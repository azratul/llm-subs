from __future__ import annotations

import os
from pathlib import Path

import pysubs2
import pytest
from typer.testing import CliRunner

from tests.helpers import fake_translate_result, one_line_srt
from translate_subs import cli as cli_module
from translate_subs import config, pipeline
from translate_subs.cli import app
from translate_subs.io import media_probe
from translate_subs.io.media_probe import MediaToolError, SubtitleTrack, ensure_binary
from translate_subs.io.source_resolver import _find_sidecar, normalize_lang, select_track
from translate_subs.memory.store import ProjectMemory, atomic_write_text

# --- atomic writes -------------------------------------------------------------------


def test_atomic_write_replaces_and_leaves_no_temp(tmp_path):
    target = tmp_path / "memory.json"
    atomic_write_text(target, "first")
    atomic_write_text(target, "second")
    assert target.read_text() == "second"
    # No leftover .tmp files from the temp-then-replace dance.
    assert [p.name for p in tmp_path.iterdir()] == ["memory.json"]


def test_project_memory_save_is_atomic(tmp_path):
    pm = ProjectMemory(tmp_path / "P")
    pm.glossary["a"] = "b"
    pm.save()
    assert (tmp_path / "P" / "glossary.json").exists()
    assert not list((tmp_path / "P").glob("*.tmp"))


@pytest.mark.skipif(os.name != "posix", reason="POSIX file permissions only")
def test_atomic_write_private_is_owner_only(tmp_path):
    import stat

    p = tmp_path / "secret.json"
    atomic_write_text(p, "data", private=True)
    assert stat.S_IMODE(p.stat().st_mode) == 0o600


@pytest.mark.skipif(os.name != "posix", reason="POSIX file permissions only")
def test_project_memory_files_are_owner_only(tmp_path):
    # Series memory may contain subtitle text and lives in the private data root: 0600, not umask.
    import stat

    pm = ProjectMemory(tmp_path / "P")
    pm.glossary["a"] = "b"
    pm.save()
    for name in ("memory.json", "glossary.json", "style_guide.json"):
        mode = stat.S_IMODE((tmp_path / "P" / name).stat().st_mode)
        assert mode == 0o600, f"{name} is {oct(mode)}, expected 0o600"


@pytest.mark.skipif(os.name != "posix", reason="POSIX file permissions only")
def test_atomic_write_private_tightens_containing_dir(tmp_path):
    # A private file's directory holds subtitle-derived state, so it must not be traversable
    # by other users; writing one into a fresh directory tightens that directory to 0700.
    import stat

    p = tmp_path / "state" / "episode" / "context.json"
    atomic_write_text(p, "data", private=True)
    assert stat.S_IMODE(p.parent.stat().st_mode) == 0o700


@pytest.mark.skipif(os.name != "posix", reason="POSIX file permissions only")
def test_ensure_private_dir_tightens_created_dirs_only(tmp_path):
    import stat

    from translate_subs.fsutil import ensure_private_dir

    root = tmp_path / "preexisting"
    root.mkdir(mode=0o755)
    ensure_private_dir(root / "a" / "b")
    # The pre-existing ancestor keeps its mode; every directory this call created is owner-only.
    assert stat.S_IMODE(root.stat().st_mode) == 0o755
    assert stat.S_IMODE((root / "a").stat().st_mode) == 0o700
    assert stat.S_IMODE((root / "a" / "b").stat().st_mode) == 0o700


@pytest.mark.skipif(os.name != "posix", reason="POSIX file permissions only")
def test_doctor_flags_group_readable_state(tmp_path, monkeypatch):
    from translate_subs import diagnostics

    projects = tmp_path / "projects"
    work = tmp_path / "cache"
    # The real flow tightens these roots (via `_writable_dir`) before the audit runs.
    projects.mkdir(mode=0o700)
    work.mkdir(mode=0o700)
    monkeypatch.setattr(diagnostics.config, "PROJECTS_DIR", projects)
    monkeypatch.setattr(diagnostics.config, "WORK_DIR", work)

    clean = diagnostics._permissions_check()
    assert clean.status == "ok"

    legacy = projects / "Serie" / "es-latam" / "memory.json"
    legacy.parent.mkdir(parents=True)
    legacy.write_text("subtitle text", encoding="utf-8")
    legacy.chmod(0o644)  # a file written by an older release
    flagged = diagnostics._permissions_check()
    assert flagged.status == "warn"
    assert str(legacy) in flagged.detail
    assert "doctor --fix" in flagged.detail


@pytest.mark.skipif(os.name != "posix", reason="POSIX file permissions only")
def test_doctor_fix_permissions_tightens_state_to_owner_only(tmp_path, monkeypatch):
    import stat

    from translate_subs import diagnostics

    projects = tmp_path / "projects"
    work = tmp_path / "cache"
    projects.mkdir(mode=0o700)
    work.mkdir(mode=0o700)
    monkeypatch.setattr(diagnostics.config, "PROJECTS_DIR", projects)
    monkeypatch.setattr(diagnostics.config, "WORK_DIR", work)

    legacy_dir = projects / "Serie" / "es-latam"
    legacy_dir.mkdir(parents=True)
    legacy_dir.chmod(0o755)
    legacy = legacy_dir / "memory.json"
    legacy.write_text("subtitle text", encoding="utf-8")
    legacy.chmod(0o644)
    # A symlink to a file outside the state dirs must not have its target chmod-ed.
    outside = tmp_path / "outside.txt"
    outside.write_text("not our state", encoding="utf-8")
    outside.chmod(0o644)
    (work / "link").symlink_to(outside)

    fixed, errors = diagnostics.fix_permissions()

    assert errors == []
    assert fixed >= 2
    assert stat.S_IMODE(legacy.lstat().st_mode) == 0o600
    assert stat.S_IMODE(legacy_dir.lstat().st_mode) == 0o700
    assert stat.S_IMODE(outside.lstat().st_mode) == 0o644
    assert diagnostics._permissions_check().status == "ok"


# --- path traversal on --project -----------------------------------------------------


@pytest.mark.parametrize("bad", ["../evil", "a/b", "..", "", "  ", ".hidden", "x\\y"])
def test_project_dir_rejects_traversal(bad, monkeypatch, tmp_path):
    monkeypatch.setattr(config, "PROJECTS_DIR", tmp_path / "projects")
    with pytest.raises(pipeline.PipelineError):
        pipeline.project_dir(bad)


def test_malformed_settings_reports_friendly_error_no_traceback(monkeypatch, tmp_path):
    # A hand-broken settings.json must produce a short error, not a raw traceback, on the commands
    # that resolve project defaults before their own try block (analyze/review/tighten/batch).
    monkeypatch.setattr(config, "PROJECTS_DIR", tmp_path / "projects")
    proj = tmp_path / "projects" / "P"
    proj.mkdir(parents=True)
    (proj / "settings.json").write_text('{"provider": "not-a-real-provider"}', encoding="utf-8")

    src = pysubs2.SSAFile()
    src.events.append(pysubs2.SSAEvent(start=0, end=2000, text="Hello."))
    source = tmp_path / "ep.en.srt"
    src.save(str(source), format_="srt")

    result = CliRunner().invoke(app, ["analyze", str(source), "--project", "P"])
    assert result.exit_code == 1
    assert "Error:" in result.output
    assert "Traceback" not in result.output


def test_project_dir_accepts_normal_name_with_spaces(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "PROJECTS_DIR", tmp_path / "projects")
    assert pipeline.project_dir("Kimagure Orange Road").name == "Kimagure Orange Road"


# --- language normalization + track selection ----------------------------------------


def _track(idx, lang, *, codec="subrip", title=None, default=False, forced=False):
    return SubtitleTrack(idx, idx, codec, lang, title, default, forced)


def test_normalize_lang_collapses_codes_and_names():
    assert normalize_lang("eng") == normalize_lang("English") == normalize_lang("en-US") == "en"
    assert normalize_lang("es-latam") == "es"
    assert normalize_lang(None) is None


def test_normalize_lang_maps_latino_aliases_to_spanish():
    # Deliberate domain override (see source_resolver `_LANG_ALIASES`): in the anime/fansub domain
    # `lat`/`Latino` mean Latin American Spanish, not Latin the language. Freeze that contract so a
    # future "ISO purity" cleanup can't silently regress real Latino source matching.
    assert normalize_lang("LAT") == "es"
    assert normalize_lang("lat") == "es"
    assert normalize_lang("Latino") == "es"
    assert normalize_lang("latam") == "es"


def test_find_sidecar_selects_latino_as_spanish(tmp_path):
    # A `.lat.srt` sidecar (common Latino fan naming) resolves as Spanish when `es` is requested.
    (tmp_path / "movie.mkv").write_bytes(b"")
    (tmp_path / "movie.lat.srt").write_text("x")
    found = _find_sidecar(tmp_path / "movie.mkv", "es")
    assert found is not None and found.name == "movie.lat.srt"


def test_select_track_exact_match_not_substring():
    # "en" must not accidentally match a label that merely contains those letters.
    tracks = [_track(0, "Brazilian"), _track(1, "eng")]
    assert select_track(tracks, lang="en", track_index=None, interactive=False).rel_index == 1


def test_select_track_prefers_full_over_forced_and_plain_over_sdh():
    tracks = [
        _track(0, "eng", forced=True),
        _track(1, "eng", title="English SDH"),
        _track(2, "eng"),
    ]
    assert select_track(tracks, lang="en", track_index=None, interactive=False).rel_index == 2


def test_find_sidecar_prefers_requested_language(tmp_path):
    (tmp_path / "ep.mkv").write_bytes(b"")
    (tmp_path / "ep.es.srt").write_text("x")
    (tmp_path / "ep.en.srt").write_text("x")
    assert _find_sidecar(tmp_path / "ep.mkv", "en").name == "ep.en.srt"
    assert _find_sidecar(tmp_path / "ep.mkv", "es").name == "ep.es.srt"


def test_find_sidecar_detects_arbitrary_iso_language(tmp_path):
    # Any ISO 639-1 language is recognized as a sidecar suffix, not only a hardcoded few.
    (tmp_path / "ep.mkv").write_bytes(b"")
    (tmp_path / "ep.ru.srt").write_text("x")
    assert _find_sidecar(tmp_path / "ep.mkv", "ru").name == "ep.ru.srt"
    # With no language preference it still picks up the Russian sidecar.
    assert _find_sidecar(tmp_path / "ep.mkv").name == "ep.ru.srt"


def test_find_sidecar_detects_multi_subtag_language(tmp_path):
    # A regional sidecar (movie.es-latam.srt) must be recognized, not just bare codes.
    from translate_subs.io.source_resolver import _sidecar_lang

    (tmp_path / "movie.mkv").write_bytes(b"")
    (tmp_path / "movie.es-latam.srt").write_text("x")
    found = _find_sidecar(tmp_path / "movie.mkv", "es")
    assert found is not None and found.name == "movie.es-latam.srt"
    assert _sidecar_lang(tmp_path / "movie.es-latam.srt") == "es"
    assert _sidecar_lang(tmp_path / "movie.pt-BR.srt") == "pt"
    assert _sidecar_lang(tmp_path / "movie.notlang.srt") is None


# --- ffmpeg/ffprobe preflight --------------------------------------------------------


def test_ensure_binary_raises_when_missing(monkeypatch):
    monkeypatch.setattr(media_probe.shutil, "which", lambda _: None)
    with pytest.raises(MediaToolError, match="not found on PATH"):
        ensure_binary("ffprobe")


def test_ensure_binary_passes_when_present(monkeypatch):
    monkeypatch.setattr(media_probe.shutil, "which", lambda _: "/usr/bin/ffprobe")
    ensure_binary("ffprobe")  # no raise


# --- CLI: --version and --force ------------------------------------------------------


def test_cli_version_flag_uses_distribution_name(monkeypatch):
    requested = []
    monkeypatch.setattr(
        cli_module,
        "_pkg_version",
        lambda distribution: requested.append(distribution) or "9.8.7",
    )

    result = CliRunner().invoke(app, ["--version"])
    assert result.exit_code == 0
    assert result.stdout.strip() == "9.8.7"
    assert requested == ["llm-subs"]


def test_config_prefers_canonical_home_variable(monkeypatch, tmp_path):
    canonical = tmp_path / "canonical"
    legacy = tmp_path / "legacy"
    monkeypatch.setenv("LLM_SUBS_HOME", str(canonical))
    monkeypatch.setenv("TRANSLATE_SUBS_HOME", str(legacy))

    assert config._data_root() == canonical

    monkeypatch.delenv("LLM_SUBS_HOME")
    assert config._data_root() == legacy


def test_config_reuses_legacy_xdg_directory(monkeypatch, tmp_path):
    monkeypatch.delenv("LLM_SUBS_HOME", raising=False)
    monkeypatch.delenv("TRANSLATE_SUBS_HOME", raising=False)
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path))
    legacy = tmp_path / "translate-subs"

    assert config._data_root() == tmp_path / "llm-subs"

    legacy.mkdir()

    assert config._data_root() == legacy

    canonical = tmp_path / "llm-subs"
    canonical.mkdir()
    assert config._data_root() == canonical


def test_translate_force_required_to_overwrite(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "PROJECTS_DIR", tmp_path / "projects")
    src = pysubs2.SSAFile()
    src.events.append(pysubs2.SSAEvent(start=0, end=2000, text="Hello."))
    source = tmp_path / "ep.en.srt"
    src.save(str(source), format_="srt")

    first = pipeline.translate_subtitle(source, provider="identity", interactive=False, project="P")
    assert first.output_path.exists()

    with pytest.raises(pipeline.PipelineError, match="already exists"):
        pipeline.translate_subtitle(source, provider="identity", interactive=False, project="P")

    again = pipeline.translate_subtitle(
        source, provider="identity", interactive=False, project="P", force=True
    )
    assert again.output_path.exists()


# --- target sanitisation / path-escape ------------------------------------------------


def test_lang_code_strips_path_characters():
    from translate_subs.naming import lang_code

    # Variants keep their region/script so they don't collide on one filename.
    assert lang_code("es-latam") == "es-latam"
    assert lang_code("pt-BR") == "pt-br"
    assert lang_code("es") == "es"
    # A hostile target can't inject path components into `<base>.<lang>.<fmt>`.
    assert "/" not in lang_code("../../tmp/x")
    assert lang_code("../../tmp/x") == "tmpx"


def test_validate_target_accepts_tags_and_rejects_paths():
    from translate_subs.naming import validate_target

    for good in ("es-latam", "pt-BR", "zh-Hans", "fr"):
        assert validate_target(good) == good
    # Normalises whitespace and underscores.
    assert validate_target(" es ") == "es"
    assert validate_target("es_latam") == "es-latam"
    # Malformed: leading/trailing/consecutive hyphens.
    for bad in ("-es", "es-", "es--latam", "../../etc", "es/latam", "", "..", r"a\b", "a b"):
        with pytest.raises(ValueError, match="target"):
            validate_target(bad)


def test_merge_alias_case_insensitive_removal():
    from translate_subs.memory.compact import merge_alias
    from translate_subs.memory.models import CharacterMemory, SeriesMemory
    from translate_subs.memory.store import ProjectMemory

    alice = CharacterMemory(name="Alice Chambers")
    alias = CharacterMemory(name="ALICE")
    bystander = CharacterMemory(name="Bob", relationships={"ALICE": "rivals"})
    mem = ProjectMemory(
        project_dir=Path("/tmp"),
        memory=SeriesMemory(characters=[alice, alias, bystander]),
    )

    result = merge_alias(mem, "Alice Chambers", "alice")
    assert result is True
    names = [ch.name for ch in mem.memory.characters]
    assert "ALICE" not in names, "alias should be removed regardless of casing"
    assert "Alice Chambers" in names
    # Relationship key must use canonical.name casing, not the caller's canonical_name arg.
    assert "Alice Chambers" in bystander.relationships
    assert "ALICE" not in bystander.relationships

    # canonical is alias (same object via casefold) — must not remove the character.
    mem2 = ProjectMemory(
        project_dir=Path("/tmp"),
        memory=SeriesMemory(characters=[CharacterMemory(name="Alice")]),
    )
    assert merge_alias(mem2, "Alice", "alice") is False
    assert len(mem2.memory.characters) == 1


def test_detect_character_aliases_needs_two_characters():
    from translate_subs.memory.compact import detect_character_aliases
    from translate_subs.memory.models import CharacterMemory

    # Fewer than two characters can't have a duplicate: return early without calling the runner.
    called = []

    def runner(_prompt: str) -> str:
        called.append(1)
        return "{}"

    assert detect_character_aliases(runner, [CharacterMemory(name="Solo")]) == []
    assert called == []


def test_detect_character_aliases_filters_to_known_names():
    from translate_subs.memory.compact import detect_character_aliases
    from translate_subs.memory.models import CharacterMemory

    chars = [CharacterMemory(name="Alice Chambers"), CharacterMemory(name="Alice")]

    def runner(_prompt: str) -> str:
        # One valid pair (both names known) and one bogus pair (unknown name) that must be dropped.
        return (
            '{"duplicates": ['
            '{"canonical": "Alice Chambers", "alias": "Alice", "reason": "same first name"},'
            '{"canonical": "Alice Chambers", "alias": "Ghost", "reason": "not in memory"}]}'
        )

    matches = detect_character_aliases(runner, chars)
    assert len(matches) == 1
    assert matches[0].canonical == "Alice Chambers" and matches[0].alias == "Alice"


def test_detect_character_aliases_wraps_malformed_reply():
    from translate_subs.ai.provider import ProviderError
    from translate_subs.memory.compact import detect_character_aliases
    from translate_subs.memory.models import CharacterMemory

    chars = [CharacterMemory(name="A"), CharacterMemory(name="B")]

    # `duplicates` is not a list -> a retryable ProviderError, not a raw TypeError.
    with pytest.raises(ProviderError, match="valid JSON"):
        detect_character_aliases(lambda _p: '{"duplicates": "nope"}', chars)


def test_translate_rejects_path_like_target(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "PROJECTS_DIR", tmp_path / "projects")
    src = pysubs2.SSAFile()
    src.events.append(pysubs2.SSAEvent(start=0, end=2000, text="Hello."))
    source = tmp_path / "ep.en.srt"
    src.save(str(source), format_="srt")

    with pytest.raises(pipeline.PipelineError, match="target"):
        pipeline.translate_subtitle(
            source, target="../../escape", provider="identity", interactive=False, project="P"
        )


# --- review --apply guard on a non-1:1 (merged .srt) target -------------------------


def test_review_apply_skipped_when_target_not_aligned(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "PROJECTS_DIR", tmp_path / "projects")
    src = pysubs2.SSAFile()
    src.events.append(pysubs2.SSAEvent(start=0, end=2000, text="One."))
    src.events.append(pysubs2.SSAEvent(start=2000, end=4000, text="Two."))
    source = tmp_path / "ep.en.srt"
    src.save(str(source), format_="srt")

    # Translated file with fewer events than source units (as a merged .srt would have).
    tgt = pysubs2.SSAFile()
    tgt.events.append(pysubs2.SSAEvent(start=0, end=4000, text="Uno.\nDos."))
    translated = tmp_path / "ep.es.srt"
    tgt.save(str(translated), format_="srt")

    result = pipeline.review_translation(
        source, translated, project="P", interactive=False, use_llm=False, apply=True
    )
    assert result.mapping_aligned is False
    assert result.n_applied == 0


# --- analyze transcript cap ----------------------------------------------------------


def test_build_transcript_caps_lines():
    from translate_subs.ai.analysis import TRANSCRIPT_LIMIT, build_transcript
    from translate_subs.domain.models import TranslatableUnit

    units = [
        TranslatableUnit(
            id=f"{i:04d}", event_index=i, start=i, end=i + 1, style="Default", text="x"
        )
        for i in range(TRANSCRIPT_LIMIT + 5)
    ]
    assert build_transcript(units).count("\n") + 1 == TRANSCRIPT_LIMIT


# --- language fallback flag / --strict-lang ------------------------------------------


def test_resolve_source_flags_language_fallback(tmp_path):
    from translate_subs.io.source_resolver import SourceError, resolve_source

    (tmp_path / "ep.mkv").write_bytes(b"")
    (tmp_path / "ep.es.srt").write_text("x")  # only Spanish available

    resolved = resolve_source(tmp_path / "ep.mkv", work_dir=tmp_path, lang="en")
    assert resolved.lang_fallback is True
    assert resolved.selected_lang == "es"

    with pytest.raises(SourceError, match="No 'en' subtitle"):
        resolve_source(tmp_path / "ep.mkv", work_dir=tmp_path, lang="en", strict_lang=True)


def test_resolve_source_no_fallback_when_language_matches(tmp_path):
    from translate_subs.io.source_resolver import resolve_source

    (tmp_path / "ep.mkv").write_bytes(b"")
    (tmp_path / "ep.en.srt").write_text("x")
    resolved = resolve_source(tmp_path / "ep.mkv", work_dir=tmp_path, lang="en")
    assert resolved.lang_fallback is False


# --- transactional output ------------------------------------------------------------


def test_translate_leaves_no_file_when_validation_fails(tmp_path, monkeypatch):
    from translate_subs.subs.validator import ValidationResult

    monkeypatch.setattr(config, "PROJECTS_DIR", tmp_path / "projects")
    monkeypatch.setattr(
        pipeline,
        "validate_output",
        lambda *a, **k: ValidationResult(ok=False, errors=["forced failure"]),
    )
    src = pysubs2.SSAFile()
    src.events.append(pysubs2.SSAEvent(start=0, end=2000, text="Hello."))
    source = tmp_path / "ep.en.srt"
    src.save(str(source), format_="srt")

    with pytest.raises(pipeline.PipelineError, match="failed validation"):
        pipeline.translate_subtitle(source, provider="identity", interactive=False, project="P")

    # Neither the final output nor a temp file is left behind.
    assert not (tmp_path / "ep.es.ass").exists()
    assert not list(tmp_path.glob(".ep*"))


def test_atomic_save_writes_and_leaves_no_temp(tmp_path):
    subs = pysubs2.SSAFile()
    subs.events.append(pysubs2.SSAEvent(start=0, end=1000, text="hi"))
    out = tmp_path / "x.ass"
    pipeline._atomic_save(subs, out, fmt="ass")
    assert out.exists()
    assert [p.name for p in tmp_path.iterdir()] == ["x.ass"]  # no leftover temp


def test_atomic_save_keeps_old_file_when_validation_fails(tmp_path):
    from translate_subs.subs.validator import ValidationResult

    out = tmp_path / "x.ass"
    out.write_text("ORIGINAL")
    subs = pysubs2.SSAFile()
    subs.events.append(pysubs2.SSAEvent(start=0, end=1000, text="hi"))

    with pytest.raises(pipeline.PipelineError, match="failed validation"):
        pipeline._atomic_save(
            subs,
            out,
            fmt="ass",
            validate=lambda p: ValidationResult(ok=False, errors=["nope"]),
        )
    assert out.read_text() == "ORIGINAL"  # destination untouched
    assert [p.name for p in tmp_path.iterdir()] == ["x.ass"]  # no leftover temp


# --- strict gender schema ------------------------------------------------------------


def test_character_memory_rejects_invalid_gender_and_extra_keys():
    from pydantic import ValidationError

    from translate_subs.memory.models import CharacterMemory, normalize_gender

    assert normalize_gender("male") == "male"
    assert normalize_gender("nonbinary") == "unknown"

    with pytest.raises(ValidationError):
        CharacterMemory(name="X", gender="nonbinary")
    with pytest.raises(ValidationError):
        CharacterMemory(name="X", typo_field="oops")


def test_merge_coerces_unexpected_gender_without_crashing():
    from translate_subs.ai.analysis import EpisodeCharacter, EpisodeContext
    from translate_subs.memory.merge import merge_episode_context
    from translate_subs.memory.models import SeriesMemory

    memory = SeriesMemory()
    ctx = EpisodeContext(characters=[EpisodeCharacter(name="Akira", gender="???")])
    merge_episode_context(memory, {}, ctx, policy="flag")
    assert memory.find("Akira").gender == "unknown"


# --- file-handoff rejects stale / mismatched outputs ---------------------------------


def _one_block_jobs():
    from translate_subs.ai.blocks import build_jobs
    from translate_subs.domain.models import TranslatableUnit

    units = [
        TranslatableUnit(id="0001", event_index=0, start=0, end=1, style="D", text="a"),
        TranslatableUnit(id="0002", event_index=1, start=1, end=2, style="D", text="b"),
    ]
    return build_jobs(units, target="es", rules=[], block_size=40, context=0)


def test_file_handoff_rejects_wrong_block_id(tmp_path):
    from translate_subs.ai.job_protocol import TranslationJobOut
    from translate_subs.ai.provider import FileHandoffProvider, ProviderError

    jobs = _one_block_jobs()
    job = jobs[0]
    bad = TranslationJobOut(block_id="WRONG", translations={"0001": "x", "0002": "y"})
    (tmp_path / f"block_{job.block_id}.out.json").write_text(bad.model_dump_json())

    with pytest.raises(ProviderError, match="does not match"):
        FileHandoffProvider(tmp_path).translate(jobs)


def test_file_handoff_rejects_id_mismatch(tmp_path):
    from translate_subs.ai.job_protocol import TranslationJobOut
    from translate_subs.ai.provider import FileHandoffProvider, ProviderError

    jobs = _one_block_jobs()
    job = jobs[0]
    # Right block, but a stale set of ids (missing 0002, extra 9999).
    bad = TranslationJobOut(block_id=job.block_id, translations={"0001": "x", "9999": "z"})
    (tmp_path / f"block_{job.block_id}.out.json").write_text(bad.model_dump_json())

    with pytest.raises(ProviderError, match="id mismatch"):
        FileHandoffProvider(tmp_path).translate(jobs)


# --- review alignment also checks timestamps -----------------------------------------


def test_review_apply_skipped_when_timestamps_differ(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "PROJECTS_DIR", tmp_path / "projects")
    src = pysubs2.SSAFile()
    src.events.append(pysubs2.SSAEvent(start=0, end=2000, text="One."))
    src.events.append(pysubs2.SSAEvent(start=2000, end=4000, text="Two."))
    source = tmp_path / "ep.en.srt"
    src.save(str(source), format_="srt")

    # Same event count, but shifted timings -> must not be considered aligned.
    tgt = pysubs2.SSAFile()
    tgt.events.append(pysubs2.SSAEvent(start=5000, end=7000, text="Uno."))
    tgt.events.append(pysubs2.SSAEvent(start=7000, end=9000, text="Dos."))
    translated = tmp_path / "ep.es.srt"
    tgt.save(str(translated), format_="srt")

    result = pipeline.review_translation(
        source, translated, project="P", interactive=False, use_llm=False, apply=True
    )
    assert result.mapping_aligned is False
    assert result.n_applied == 0


# --- extraction cache key avoids collisions ------------------------------------------


def test_extraction_cache_key_differs_per_file(tmp_path):
    from translate_subs.io.track_extractor import _cache_key

    track = _track(0, "eng")
    a = tmp_path / "A"
    a.mkdir()
    (a / "Episode 01.mkv").write_bytes(b"aaaa")
    b = tmp_path / "B"
    b.mkdir()
    (b / "Episode 01.mkv").write_bytes(b"bbbbbbbb")
    # Same filename, different folders/sizes -> different keys (no shared destination).
    assert _cache_key(a / "Episode 01.mkv", track) != _cache_key(b / "Episode 01.mkv", track)
    assert _cache_key(a / "Episode 01.mkv", track) == _cache_key(a / "Episode 01.mkv", track)


# --- protocol hardening --------------------------------------------------------------


def test_translation_job_out_rejects_extra_keys():
    from pydantic import ValidationError

    from translate_subs.ai.job_protocol import TranslationJobOut

    with pytest.raises(ValidationError):
        TranslationJobOut.model_validate_json('{"block_id": "1", "translations": {}, "bogus": 1}')


def test_parse_reply_rejects_non_string_values():
    from translate_subs.ai.job_protocol import JobLine, TranslationJobIn
    from translate_subs.ai.provider import ProviderError, parse_translation_reply

    job = TranslationJobIn(block_id="0001", target="es", translate=[JobLine(id="0001", text="hi")])
    with pytest.raises(ProviderError, match="non-string"):
        parse_translation_reply('{"0001": ["a", "b"]}', job)


def test_retry_provider_call_backs_off_between_attempts():
    from translate_subs.ai.provider import ProviderError, retry_provider_call

    waits: list[float] = []
    attempts = {"n": 0}

    def flaky() -> str:
        attempts["n"] += 1
        if attempts["n"] < 3:
            raise ProviderError("transient", retryable=True)
        return "ok"

    result = retry_provider_call(
        flaky,
        max_retries=3,
        label="block",
        backoff_base=1.0,
        jitter_ratio=0,
        sleep=waits.append,
    )
    assert result == "ok"
    # Two failures before success → two waits with exponential growth (1s, 2s).
    assert waits == [1.0, 2.0]


def test_retry_provider_call_no_wait_after_last_attempt():
    from translate_subs.ai.provider import ProviderError, retry_provider_call

    waits: list[float] = []

    def always_fail() -> str:
        raise ProviderError("nope", retryable=True)

    with pytest.raises(ProviderError, match="after 2 attempt"):
        retry_provider_call(
            always_fail,
            max_retries=1,
            label="block",
            backoff_base=1.0,
            jitter_ratio=0,
            sleep=waits.append,
        )
    # One retry → exactly one wait; never sleeps after the final failed attempt.
    assert waits == [1.0]


def test_retry_provider_call_caps_backoff():
    from translate_subs.ai.provider import ProviderError, retry_provider_call

    waits: list[float] = []

    def always_fail() -> str:
        raise ProviderError("nope", retryable=True)

    with pytest.raises(ProviderError):
        retry_provider_call(
            always_fail,
            max_retries=5,
            label="block",
            backoff_base=10.0,
            backoff_cap=15.0,
            jitter_ratio=0,
            sleep=waits.append,
        )
    assert max(waits) <= 15.0


def test_retry_provider_call_does_not_retry_permanent_errors():
    from translate_subs.ai.provider import ProviderError, retry_provider_call

    attempts = {"n": 0}

    def permanent() -> str:
        attempts["n"] += 1
        raise ProviderError("bad credentials", retryable=False)

    with pytest.raises(ProviderError, match="bad credentials"):
        retry_provider_call(permanent, max_retries=3, label="block", backoff_base=0)
    assert attempts["n"] == 1


def test_retry_provider_call_honours_retry_after():
    from translate_subs.ai.provider import ProviderError, retry_provider_call

    waits: list[float] = []
    attempts = {"n": 0}

    def rate_limited() -> str:
        attempts["n"] += 1
        if attempts["n"] == 1:
            raise ProviderError("rate limited", retryable=True, retry_after=7.0)
        return "ok"

    assert (
        retry_provider_call(
            rate_limited,
            max_retries=1,
            label="block",
            sleep=waits.append,
        )
        == "ok"
    )
    assert waits == [7.0]


def test_retry_provider_call_caps_retry_after():
    # Retry-After is server-controlled input: an absurd value must not park the tool for days.
    from translate_subs.ai.provider import RETRY_AFTER_CAP, ProviderError, retry_provider_call

    waits: list[float] = []
    attempts = {"n": 0}

    def rate_limited() -> str:
        attempts["n"] += 1
        if attempts["n"] == 1:
            raise ProviderError("rate limited", retryable=True, retry_after=999_999.0)
        return "ok"

    assert (
        retry_provider_call(rate_limited, max_retries=1, label="block", sleep=waits.append) == "ok"
    )
    assert waits == [RETRY_AFTER_CAP]


def test_doctor_reports_writable_dirs(monkeypatch, tmp_path):
    from translate_subs import config, diagnostics

    monkeypatch.setattr(config, "DATA_DIR", tmp_path / "data")
    monkeypatch.setattr(config, "PROJECTS_DIR", tmp_path / "data" / "projects")
    monkeypatch.setattr(config, "WORK_DIR", tmp_path / "cache" / "work")
    checks = diagnostics.run_diagnostics()
    by_name = {c.name: c for c in checks}
    assert by_name["data dir"].status == "ok"
    assert by_name["projects dir"].status == "ok"
    assert by_name["cache dir"].status == "ok"
    assert all(c.status != "fail" for c in checks)


def test_doctor_flags_missing_cli_provider(monkeypatch):
    from translate_subs import diagnostics

    monkeypatch.setattr(diagnostics.shutil, "which", lambda name: None)
    check = diagnostics._provider_check("claude")
    assert check.status == "fail"
    assert "not found" in check.detail


def test_doctor_provider_check_passthrough_needs_no_backend():
    from translate_subs import diagnostics

    assert diagnostics._provider_check("identity").status == "ok"
    assert diagnostics._provider_check("file-handoff").status == "ok"


def test_doctor_warns_antigravity_weak_isolation(monkeypatch):
    from translate_subs import diagnostics

    # CLI present, so the provider check is ok; the extra isolation warning must still be emitted.
    monkeypatch.setattr(diagnostics.shutil, "which", lambda name: "/usr/bin/agy")
    by_name = {c.name: c for c in diagnostics.run_diagnostics(provider="antigravity")}
    assert by_name["antigravity"].status == "ok"
    assert by_name["antigravity isolation"].status == "warn"
    assert "ollama" in by_name["antigravity isolation"].detail
    # Other providers get no such warning (identity needs no backend and no network).
    assert not any(
        c.name == "antigravity isolation" for c in diagnostics.run_diagnostics(provider="identity")
    )


def test_warn_weak_backend_helper(capsys):
    from translate_subs import cli

    cli._warn_weak_backend("claude", "ollama")
    assert "antigravity" not in capsys.readouterr().out
    # Any provider in the set triggers it (batch passes both translate and pre-analyze providers).
    cli._warn_weak_backend("claude", "antigravity")
    assert "antigravity" in capsys.readouterr().out


def test_analyze_warns_on_antigravity_before_running(tmp_path, monkeypatch):
    from translate_subs import cli

    monkeypatch.setattr(config, "PROJECTS_DIR", tmp_path / "projects")

    def _boom(*a, **k):
        raise cli.PipelineError("stop before hitting the backend")

    monkeypatch.setattr(cli, "analyze_subtitle", _boom)
    src = tmp_path / "ep.en.srt"
    subs = pysubs2.SSAFile()
    subs.events.append(pysubs2.SSAEvent(start=0, end=1500, text="Hello."))
    subs.save(str(src), format_="srt")
    res = CliRunner().invoke(
        app, ["analyze", str(src), "--project", "P", "--provider", "antigravity", "-y"]
    )
    # Warning emitted at the analyze call site, not only in translate/batch.
    assert "antigravity" in res.stdout


def test_doctor_reports_cli_version(monkeypatch):
    from translate_subs import diagnostics

    monkeypatch.setattr(diagnostics, "_pkg_version", lambda name: "9.9.9")
    by_name = {c.name: c for c in diagnostics.run_diagnostics()}
    assert by_name["llm-subs"].status == "ok" and by_name["llm-subs"].detail == "9.9.9"


def test_doctor_ollama_checks_model_presence(monkeypatch):
    import io
    import json

    from translate_subs import diagnostics

    payload = json.dumps({"models": [{"name": "qwen3:4b"}, {"name": "llama3:8b"}]}).encode()

    def fake_urlopen(url, timeout=5):
        return io.BytesIO(payload)

    monkeypatch.setattr(diagnostics.urllib.request, "urlopen", fake_urlopen)

    # Bare-name match against a tagged model.
    assert diagnostics._ollama_check("qwen3").status == "ok"
    assert diagnostics._ollama_check("qwen3:4b").status == "ok"
    missing = diagnostics._ollama_check("mistral")
    assert missing.status == "fail" and "not installed" in missing.detail
    # No model requested: still ok, lists what's there.
    assert diagnostics._ollama_check().status == "ok"


@pytest.mark.parametrize("body", ["[]", '{"models": null}', '{"models": [null]}', '"oops"'])
def test_doctor_ollama_tolerates_unexpected_json(monkeypatch, body):
    # A 200 with a valid-but-unexpected JSON shape must not crash doctor; it warns instead.
    import io

    from translate_subs import diagnostics

    monkeypatch.setattr(
        diagnostics.urllib.request, "urlopen", lambda url, timeout=5: io.BytesIO(body.encode())
    )
    check = diagnostics._ollama_check("qwen3")
    assert check.status in ("warn", "fail")  # never raises


def test_translate_fail_on_untranslated_exits_nonzero(tmp_path, monkeypatch):
    from translate_subs import cli

    monkeypatch.setattr(
        cli,
        "translate_subtitle",
        lambda *a, **k: fake_translate_result(tmp_path, ["0007"]),
    )
    src = tmp_path / "ep.en.srt"
    src.write_text("", encoding="utf-8")

    failed = CliRunner().invoke(app, ["translate", str(src), "--fail-on-untranslated"])
    assert failed.exit_code == 1
    assert "not translated" in failed.stdout

    # Without the flag the same partial result is a success (file is still written).
    ok = CliRunner().invoke(app, ["translate", str(src)])
    assert ok.exit_code == 0


def test_translate_no_fail_when_all_translated(tmp_path, monkeypatch):
    from translate_subs import cli

    monkeypatch.setattr(
        cli,
        "translate_subtitle",
        lambda *a, **k: fake_translate_result(tmp_path, []),
    )
    src = tmp_path / "ep.en.srt"
    src.write_text("", encoding="utf-8")

    result = CliRunner().invoke(app, ["translate", str(src), "--fail-on-untranslated"])
    assert result.exit_code == 0


# --- per-project settings ------------------------------------------------------------


def test_settings_round_trip_and_resolve_precedence(tmp_path):
    from translate_subs.settings import (
        ProjectSettings,
        load_settings,
        resolve,
        save_settings,
    )

    assert load_settings(tmp_path) == ProjectSettings()  # missing file -> all unset
    save_settings(tmp_path, ProjectSettings(provider="ollama", model="qwen3:4b"))
    loaded = load_settings(tmp_path)
    assert loaded.provider == "ollama" and loaded.model == "qwen3:4b"

    assert resolve("claude", "provider", loaded) == "claude"  # explicit flag wins
    assert resolve(None, "provider", loaded) == "ollama"  # falls back to setting
    assert resolve(None, "target", loaded) == "es-latam"  # falls back to built-in
    assert resolve(None, "model", loaded) == "qwen3:4b"
    assert resolve(None, "reasoning", loaded) is None  # no setting, no built-in


def test_settings_reject_path_like_target(tmp_path):
    import pydantic

    from translate_subs.settings import ProjectSettings, load_settings

    # A path-like target is rejected at construction, not silently carried to translate time.
    with pytest.raises(pydantic.ValidationError):
        ProjectSettings(target="../../etc")

    # A hand-edited settings.json with the same value surfaces a friendly ValueError on load.
    (tmp_path / "settings.json").write_text('{"target": "../../etc"}', encoding="utf-8")
    with pytest.raises(ValueError, match="invalid"):
        load_settings(tmp_path)

    assert ProjectSettings(target="es-latam").target == "es-latam"  # valid tag still accepted


def test_config_command_sets_unsets_and_validates(tmp_path, monkeypatch):
    from translate_subs.settings import load_settings

    monkeypatch.setattr(config, "PROJECTS_DIR", tmp_path / "projects")

    ok = CliRunner().invoke(app, ["config", "P", "--provider", "codex", "--reasoning", "high"])
    assert ok.exit_code == 0
    saved = load_settings(tmp_path / "projects" / "P")
    assert saved.provider == "codex" and saved.reasoning == "high"

    CliRunner().invoke(app, ["config", "P", "--unset", "provider"])
    after = load_settings(tmp_path / "projects" / "P")
    assert after.provider is None and after.reasoning == "high"  # only the named field cleared

    bad = CliRunner().invoke(app, ["config", "P", "--format", "vtt"])
    assert bad.exit_code == 2
    unknown = CliRunner().invoke(app, ["config", "P", "--unset", "nope"])
    assert unknown.exit_code == 2


def test_translate_uses_project_settings_as_defaults(tmp_path, monkeypatch):
    from translate_subs import cli
    from translate_subs.settings import ProjectSettings, save_settings

    monkeypatch.setattr(config, "PROJECTS_DIR", tmp_path / "projects")
    pdir = tmp_path / "projects" / "P"
    pdir.mkdir(parents=True)
    save_settings(pdir, ProjectSettings(provider="ollama", model="qwen3:4b", target="fr"))

    captured: dict = {}

    def fake(input_path, **kwargs):
        captured.clear()
        captured.update(kwargs)
        return fake_translate_result(tmp_path, [])

    monkeypatch.setattr(cli, "translate_subtitle", fake)
    src = tmp_path / "ep.en.srt"
    src.write_text("", encoding="utf-8")

    CliRunner().invoke(app, ["translate", str(src), "--project", "P"])
    assert captured["provider"] == "ollama"
    assert captured["model"] == "qwen3:4b"
    assert captured["target"] == "fr"

    # An explicit flag overrides the project setting; unspecified ones still come from it.
    CliRunner().invoke(app, ["translate", str(src), "--project", "P", "--provider", "claude"])
    assert captured["provider"] == "claude"
    assert captured["model"] == "qwen3:4b"


def test_review_and_tighten_use_project_settings_as_defaults(tmp_path, monkeypatch):
    from translate_subs import cli
    from translate_subs.settings import ProjectSettings, save_settings

    monkeypatch.setattr(config, "PROJECTS_DIR", tmp_path / "projects")
    pdir = tmp_path / "projects" / "P"
    pdir.mkdir(parents=True)
    save_settings(pdir, ProjectSettings(provider="ollama", model="qwen3:4b", target="fr-FR"))

    captured: dict = {}

    def fake(*args, **kwargs):
        captured.clear()
        captured.update(kwargs)
        return fake_translate_result(tmp_path, [])  # shape unused by these assertions

    src = tmp_path / "ep.en.srt"
    src.write_text("", encoding="utf-8")
    tgt = tmp_path / "ep.es.srt"
    tgt.write_text("", encoding="utf-8")

    monkeypatch.setattr(cli, "review_translation", fake)
    CliRunner().invoke(app, ["review", str(src), str(tgt), "--project", "P", "--no-llm"])
    assert captured["target"] == "fr-FR"
    assert captured["provider"] == "ollama"
    assert captured["model"] == "qwen3:4b"

    monkeypatch.setattr(cli, "tighten_subtitle", fake)
    CliRunner().invoke(app, ["tighten", str(tgt), "--project", "P", "--no-llm"])
    assert captured["target"] == "fr-FR"
    assert captured["provider"] == "ollama"
    assert captured["model"] == "qwen3:4b"


# --- --output must not overwrite the source (#5) -------------------------------------


def test_translate_refuses_to_overwrite_source(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "PROJECTS_DIR", tmp_path / "projects")
    src = tmp_path / "ep.srt"
    one_line_srt(src)
    # --output aimed at the source file (same suffix/format) must be refused, even with force.
    with pytest.raises(pipeline.PipelineError, match="Refusing to overwrite the source"):
        pipeline.translate_subtitle(
            src,
            provider="identity",
            interactive=False,
            project="P",
            output=src,
            fmt="srt",
            force=True,
        )


# --- model-injected ASS tags are stripped (#6) ---------------------------------------


def test_sanitize_strips_injected_ass_tags_but_keeps_literal_braces():
    from translate_subs.subs.reinserter import sanitize_model_text

    assert sanitize_model_text(r"Hola {\b1}mundo{\b0}") == "Hola mundo"
    assert sanitize_model_text(r"{\an8}Cartel: PELIGRO") == "Cartel: PELIGRO"
    # A literal brace with no backslash command is dialogue, not a tag — keep it.
    assert sanitize_model_text("usa {llave} aquí") == "usa {llave} aquí"


def test_apply_translation_neutralizes_injected_tag(tmp_path):
    subs = pysubs2.SSAFile()
    subs.events.append(pysubs2.SSAEvent(start=0, end=2000, text="hi"))
    from translate_subs.domain.models import TranslatableUnit
    from translate_subs.subs.reinserter import apply_translations

    unit = TranslatableUnit(id="0001", event_index=0, start=0, end=2000, style="Default", text="hi")
    apply_translations(subs, [unit], {"0001": r"Hola {\i1}mundo"})
    assert "\\i1" not in subs.events[0].text
    assert "Hola" in subs.events[0].plaintext and "mundo" in subs.events[0].plaintext


# --- per-target memory layout + backward-compat fallback (#3) -------------------------


def test_memory_root_segments_by_target(tmp_path, monkeypatch):
    from translate_subs.workflows.support import memory_root

    monkeypatch.setattr(config, "PROJECTS_DIR", tmp_path / "projects")
    es = memory_root("Show", "es-latam")
    fr = memory_root("Show", "fr-FR")
    assert es.name == "es-latam" and fr.name == "fr-fr"
    assert es != fr  # a French run can't inherit the Spanish glossary
    # Region matters: a Latin-American and a Castilian run keep separate memory (full target,
    # not the collapsed language code, so they can't contaminate each other).
    assert memory_root("Show", "es-latam") != memory_root("Show", "es-ES")


def test_default_project_skips_season_folder(tmp_path):
    from translate_subs.workflows.support import default_project

    # A bare season folder is a poor default; use the series folder above it.
    series = tmp_path / "Cowboy Bebop"
    (series / "Season 1").mkdir(parents=True)
    assert default_project(series / "Season 1" / "ep01.mkv") == "Cowboy Bebop"
    (series / "S02").mkdir()
    assert default_project(series / "S02" / "ep01.mkv") == "Cowboy Bebop"
    (series / "Specials").mkdir()
    assert default_project(series / "Specials" / "ova.mkv") == "Cowboy Bebop"

    # A normal folder is used as-is.
    (tmp_path / "Some Movie").mkdir()
    assert default_project(tmp_path / "Some Movie" / "movie.mkv") == "Some Movie"


def test_episode_key_disambiguates_same_name_in_different_folders(tmp_path):
    from translate_subs.workflows.support import episode_key

    (tmp_path / "S1").mkdir()
    (tmp_path / "S2").mkdir()
    e1 = tmp_path / "S1" / "Episode 01.mkv"
    e2 = tmp_path / "S2" / "Episode 01.mkv"
    e1.write_bytes(b"")
    e2.write_bytes(b"")

    # Same stem, different folders -> different episode dirs (no shared context/checkpoint).
    assert episode_key(e1) != episode_key(e2)
    assert episode_key(e1).startswith("Episode 01 [")
    # Stable: the same file always maps to the same key (so resume works).
    assert episode_key(e1) == episode_key(tmp_path / "S1" / "Episode 01.mkv")


def test_translate_does_not_leak_glossary_across_targets(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "PROJECTS_DIR", tmp_path / "projects")
    src = tmp_path / "ep.en.srt"
    one_line_srt(src)

    # Seed an es-latam glossary the way analyze would, under the per-target memory root.
    from translate_subs.memory.store import ProjectMemory
    from translate_subs.workflows.support import memory_root

    es_mem = ProjectMemory.load(memory_root("P", "es-latam"))
    es_mem.glossary["Sword"] = "Espada"
    es_mem.save()

    # A French translation loads the (empty) French memory, not the Spanish one.
    fr_mem = ProjectMemory.load(memory_root("P", "fr"))
    assert "Sword" not in fr_mem.glossary
    assert (memory_root("P", "es-latam") / "glossary.json").exists()
    assert not (memory_root("P", "fr") / "glossary.json").exists()


# --- generated files respect the umask (#1) ------------------------------------------

# POSIX-only: Windows has no umask/group-other permission bits (os.chmod only toggles the
# read-only flag), so a written file always reports 0o666 there.
_posix_only = pytest.mark.skipif(os.name != "posix", reason="POSIX file permissions only")


@_posix_only
def test_atomic_write_text_respects_umask(tmp_path):
    from translate_subs.fsutil import atomic_write_text

    old = os.umask(0o022)
    try:
        target = tmp_path / "out.json"
        atomic_write_text(target, "data")
        mode = target.stat().st_mode & 0o777
    finally:
        os.umask(old)
    # mkstemp would leave 0o600; respecting the umask gives the share-friendly 0o644.
    assert mode == 0o644


@_posix_only
def test_translate_output_respects_umask(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "PROJECTS_DIR", tmp_path / "projects")
    src = tmp_path / "ep.en.srt"
    one_line_srt(src)
    old = os.umask(0o022)
    try:
        result = pipeline.translate_subtitle(
            src, provider="identity", interactive=False, project="P", fmt="srt"
        )
    finally:
        os.umask(old)
    assert result.output_path.stat().st_mode & 0o777 == 0o644
