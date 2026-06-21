from __future__ import annotations

from pathlib import Path

import pysubs2
import pytest
from typer.testing import CliRunner

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


# --- path traversal on --project -----------------------------------------------------


@pytest.mark.parametrize("bad", ["../evil", "a/b", "..", "", "  ", ".hidden", "x\\y"])
def test_project_dir_rejects_traversal(bad, monkeypatch, tmp_path):
    monkeypatch.setattr(config, "PROJECTS_DIR", tmp_path / "projects")
    with pytest.raises(pipeline.PipelineError):
        pipeline.project_dir(bad)


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


# --- ffmpeg/ffprobe preflight --------------------------------------------------------


def test_ensure_binary_raises_when_missing(monkeypatch):
    monkeypatch.setattr(media_probe.shutil, "which", lambda _: None)
    with pytest.raises(MediaToolError, match="not found on PATH"):
        ensure_binary("ffprobe")


def test_ensure_binary_passes_when_present(monkeypatch):
    monkeypatch.setattr(media_probe.shutil, "which", lambda _: "/usr/bin/ffprobe")
    ensure_binary("ffprobe")  # no raise


# --- CLI: --version and --force ------------------------------------------------------


def test_cli_version_flag():
    result = CliRunner().invoke(app, ["--version"])
    assert result.exit_code == 0
    assert result.stdout.strip()  # prints a version string


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


def _fake_translate_result(tmp_path, untranslated):
    from types import SimpleNamespace

    from translate_subs.pipeline import TranslateResult

    out = tmp_path / "ep.es.ass"
    out.write_text("", encoding="utf-8")
    source = SimpleNamespace(
        was_extracted=False,
        track=None,
        subtitle_path=out,
        origin=out,
        lang_fallback=False,
        selected_lang="en",
    )
    validation = SimpleNamespace(ok=True, warnings=[], errors=[])
    return TranslateResult(
        source=source,
        output_path=out,
        n_units=1,
        n_jobs=1,
        output_validation=validation,
        context_used=False,
        memory_used=False,
        untranslated_ids=list(untranslated),
    )


def test_translate_fail_on_untranslated_exits_nonzero(tmp_path, monkeypatch):
    from translate_subs import cli

    monkeypatch.setattr(
        cli,
        "translate_subtitle",
        lambda *a, **k: _fake_translate_result(tmp_path, ["0007"]),
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
        lambda *a, **k: _fake_translate_result(tmp_path, []),
    )
    src = tmp_path / "ep.en.srt"
    src.write_text("", encoding="utf-8")

    result = CliRunner().invoke(app, ["translate", str(src), "--fail-on-untranslated"])
    assert result.exit_code == 0


# --- block checkpoint / resume -------------------------------------------------------


def _job(block_id, lines, *, target="es", rules=None):
    from translate_subs.ai.job_protocol import JobLine, TranslationJobIn

    return TranslationJobIn(
        block_id=block_id,
        target=target,
        rules=rules or [],
        translate=[JobLine(id=i, text=t) for i, t in lines],
    )


def test_block_hash_is_stable_and_content_sensitive():
    from translate_subs.ai.checkpoint import block_hash

    a = _job("0001", [("0001", "hi"), ("0002", "bye")])
    same = _job("9999", [("0001", "hi"), ("0002", "bye")])  # block_id is not part of the hash
    diff_text = _job("0001", [("0001", "hi"), ("0002", "ciao")])
    diff_rules = _job("0001", [("0001", "hi"), ("0002", "bye")], rules=["formal"])
    diff_target = _job("0001", [("0001", "hi"), ("0002", "bye")], target="fr")

    assert block_hash(a) == block_hash(same)
    assert block_hash(a) != block_hash(diff_text)
    assert block_hash(a) != block_hash(diff_rules)
    assert block_hash(a) != block_hash(diff_target)


def test_block_hash_includes_surrounding_context():
    from translate_subs.ai.checkpoint import block_hash
    from translate_subs.ai.job_protocol import JobLine, TranslationJobIn

    def job(before_text: str) -> TranslationJobIn:
        return TranslationJobIn(
            block_id="0002",
            target="es",
            context_before=[JobLine(id="0001", text=before_text)],
            translate=[JobLine(id="0002", text="unchanged")],
        )

    # Same block lines, different neighbour: the context steers meaning, so the hash differs and
    # the block is re-translated rather than reusing a translation made under the old context.
    assert block_hash(job("Hello there.")) != block_hash(job("Goodbye."))


def test_block_hash_includes_prompt_version(monkeypatch):
    from translate_subs.ai import checkpoint

    job = _job("0001", [("0001", "hello")])
    original = checkpoint.block_hash(job)
    monkeypatch.setattr(checkpoint, "TRANSLATION_PROMPT_VERSION", 999)
    assert checkpoint.block_hash(job) != original


def test_checkpoint_round_trips_and_signature_mismatch_loads_empty(tmp_path):
    from translate_subs.ai.checkpoint import BlockCheckpoint, _Entry

    path = tmp_path / "cp.json"
    cp = BlockCheckpoint(path, signature="claude|", entries={})
    cp.entries["abc"] = _Entry("0001", {"0001": "Hola"}, [])
    cp.save()

    same = BlockCheckpoint.load(path, "claude|")
    assert same.entries["abc"].translations == {"0001": "Hola"}

    # A different provider/model signature must not reuse the cached blocks.
    other = BlockCheckpoint.load(path, "ollama|qwen3:4b")
    assert other.entries == {}


def test_checkpoint_corrupt_file_loads_empty(tmp_path):
    from translate_subs.ai.checkpoint import BlockCheckpoint

    path = tmp_path / "cp.json"
    path.write_text("{ not json", encoding="utf-8")
    assert BlockCheckpoint.load(path, "claude|").entries == {}


def test_checkpoint_wrong_value_types_loads_empty(tmp_path):
    from translate_subs.ai.checkpoint import CHECKPOINT_VERSION, BlockCheckpoint

    path = tmp_path / "cp.json"
    path.write_text(
        (
            '{"version":'
            f"{CHECKPOINT_VERSION}"
            ',"signature":"claude|","blocks":{"abc":{"block_id":"0001",'
            '"translations":{"0001":123},"untranslated":[]}}}'
        ),
        encoding="utf-8",
    )
    assert BlockCheckpoint.load(path, "claude|").entries == {}


class _FlakyProvider:
    """Test double: uppercases text; optionally raises on one block; records calls."""

    def __init__(self, fail_on_block=None):
        from translate_subs.ai.provider import ProviderError

        self._error = ProviderError
        self.fail_on_block = fail_on_block
        self.calls: list[str] = []
        self.untranslated_ids: list[str] = []

    def translate(self, jobs):
        self.untranslated_ids = []
        out: dict[str, str] = {}
        for job in jobs:
            self.calls.append(job.block_id)
            if job.block_id == self.fail_on_block:
                raise self._error("boom")
            for line in job.translate:
                out[line.id] = line.text.upper()
        return out


def test_translate_with_checkpoint_reuses_cached_block(tmp_path):
    from translate_subs.ai.checkpoint import (
        BlockCheckpoint,
        block_hash,
        translate_with_checkpoint,
    )

    jobs = [_job("0001", [("0001", "a")]), _job("0002", [("0002", "b")])]
    cp = BlockCheckpoint(tmp_path / "cp.json", signature="claude|")
    prov = _FlakyProvider()

    # Pre-seed block 1 as already translated; only block 2 should hit the provider.
    from translate_subs.ai.checkpoint import _Entry

    cp.entries[block_hash(jobs[0])] = _Entry("0001", {"0001": "PRE"}, [])
    events: list = []
    translations, untranslated = translate_with_checkpoint(
        prov, jobs, checkpoint=cp, on_progress=events.append
    )

    assert prov.calls == ["0002"]  # block 1 was reused, not re-translated
    assert translations == {"0001": "PRE", "0002": "B"}
    assert untranslated == []
    assert [e.reused for e in events] == [True, False]
    assert events[-1].total == 2


def test_translate_with_checkpoint_regenerates_mismatched_entry(tmp_path):
    from translate_subs.ai.checkpoint import (
        BlockCheckpoint,
        _Entry,
        block_hash,
        translate_with_checkpoint,
    )

    job = _job("0001", [("0001", "a")])
    cp = BlockCheckpoint(tmp_path / "cp.json", signature="claude|")
    cp.entries[block_hash(job)] = _Entry("0001", {"9999": "STALE"}, [])
    provider = _FlakyProvider()

    translations, _ = translate_with_checkpoint(provider, [job], checkpoint=cp)

    assert provider.calls == ["0001"]
    assert translations == {"0001": "A"}


def _multi_block_source(tmp_path, n=45):
    subs = pysubs2.SSAFile()
    for i in range(n):
        subs.events.append(pysubs2.SSAEvent(start=i * 2000, end=i * 2000 + 1500, text=f"Line {i}."))
    source = tmp_path / "ep.en.srt"
    subs.save(str(source), format_="srt")
    return source


def test_translate_resumes_after_block_failure(tmp_path, monkeypatch):
    from translate_subs.ai.provider import ProviderError

    monkeypatch.setattr(config, "PROJECTS_DIR", tmp_path / "projects")
    source = _multi_block_source(tmp_path)  # 45 lines -> 2 blocks (40 + 5)

    # First run: the provider blows up on block 2, after block 1 was checkpointed.
    monkeypatch.setattr(
        pipeline, "make_provider", lambda *a, **k: _FlakyProvider(fail_on_block="0002")
    )
    with pytest.raises(ProviderError):
        pipeline.translate_subtitle(source, provider="claude", interactive=False, project="P")

    from translate_subs.workflows.support import episode_key

    episode = episode_key(source)
    checkpoint = tmp_path / "projects" / "P" / "es" / episode / "translations.checkpoint.json"
    assert checkpoint.exists()

    # Second run: a healthy provider should only need to translate the missing block 2.
    healthy = _FlakyProvider()
    monkeypatch.setattr(pipeline, "make_provider", lambda *a, **k: healthy)
    result = pipeline.translate_subtitle(source, provider="claude", interactive=False, project="P")
    assert healthy.calls == ["0002"]  # block 1 reused from the checkpoint
    assert result.output_path.exists()
    assert result.output_validation.ok


def test_translate_no_resume_retranslates_all_blocks(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "PROJECTS_DIR", tmp_path / "projects")
    source = _multi_block_source(tmp_path)

    first = _FlakyProvider()
    monkeypatch.setattr(pipeline, "make_provider", lambda *a, **k: first)
    pipeline.translate_subtitle(source, provider="claude", interactive=False, project="P")
    assert first.calls == ["0001", "0002"]

    # --no-resume ignores the checkpoint: both blocks are translated again.
    second = _FlakyProvider()
    monkeypatch.setattr(pipeline, "make_provider", lambda *a, **k: second)
    pipeline.translate_subtitle(
        source, provider="claude", interactive=False, project="P", force=True, resume=False
    )
    assert second.calls == ["0001", "0002"]


# --- batch / directory translation ---------------------------------------------------


def _one_line_srt(path):
    subs = pysubs2.SSAFile()
    subs.events.append(pysubs2.SSAEvent(start=0, end=2000, text="Hi."))
    subs.save(str(path), format_="srt")


def test_discover_inputs_filters_pattern_and_skips_outputs(tmp_path):
    _one_line_srt(tmp_path / "ep01.en.srt")
    _one_line_srt(tmp_path / "ep02.en.srt")
    _one_line_srt(tmp_path / "ep01.es.srt")  # a previous output — must not be picked up
    (tmp_path / "note.txt").write_text("x", encoding="utf-8")

    found = pipeline.discover_inputs(tmp_path, globs=("*.srt",), target="es-latam")
    names = [p.name for p in found]
    assert names == ["ep01.en.srt", "ep02.en.srt"]


def test_discover_inputs_recursive(tmp_path):
    (tmp_path / "S01").mkdir()
    _one_line_srt(tmp_path / "S01" / "ep01.en.srt")
    _one_line_srt(tmp_path / "top.en.srt")

    flat = pipeline.discover_inputs(tmp_path, globs=("*.srt",), recursive=False)
    assert [p.name for p in flat] == ["top.en.srt"]
    deep = pipeline.discover_inputs(tmp_path, globs=("*.srt",), recursive=True)
    assert sorted(p.name for p in deep) == ["ep01.en.srt", "top.en.srt"]


def test_discover_inputs_rejects_non_directory(tmp_path):
    f = tmp_path / "x.srt"
    _one_line_srt(f)
    with pytest.raises(pipeline.PipelineError, match="Not a directory"):
        pipeline.discover_inputs(f)


def test_batch_translate_skips_done_and_continues_past_failures(tmp_path, monkeypatch):
    from translate_subs.io.source_resolver import SourceError

    monkeypatch.setattr(config, "PROJECTS_DIR", tmp_path / "projects")
    for n in ("ep01.en.srt", "ep02.en.srt", "ep03.en.srt"):
        _one_line_srt(tmp_path / n)
    # Pre-create ep02's output so it is skipped without --force.
    _one_line_srt(tmp_path / "ep02.es.srt")

    real = pipeline.translate_subtitle

    def fake_translate(path, **kwargs):
        if Path(path).name == "ep03.en.srt":
            raise SourceError("no usable track")
        return real(path, **kwargs)

    monkeypatch.setattr(pipeline, "translate_subtitle", fake_translate)

    seen: list[str] = []
    result = pipeline.batch_translate(
        tmp_path,
        globs=("*.srt",),
        on_episode=lambda i, n, p: seen.append(p.name),
        provider="identity",
        target="es-latam",
        fmt="srt",
        interactive=False,
        project="P",
    )

    by_name = {i.input_path.name: i for i in result.items}
    assert by_name["ep01.en.srt"].status == "translated"
    assert by_name["ep02.en.srt"].status == "skipped"
    assert by_name["ep03.en.srt"].status == "failed"
    assert "no usable track" in by_name["ep03.en.srt"].error
    assert result.n_translated == 1 and result.n_skipped == 1 and result.n_failed == 1
    assert seen == ["ep01.en.srt", "ep02.en.srt", "ep03.en.srt"]  # progress per episode


def test_batch_cli_exits_nonzero_on_failure(tmp_path, monkeypatch):
    from translate_subs import cli
    from translate_subs.pipeline import BatchItem, BatchResult

    monkeypatch.setattr(
        cli,
        "batch_translate",
        lambda *a, **k: BatchResult(
            items=[
                BatchItem(
                    tmp_path / "ep01.mkv", "translated", output_path=tmp_path / "ep01.es.ass"
                ),
                BatchItem(tmp_path / "ep02.mkv", "failed", error="boom"),
            ]
        ),
    )
    res = CliRunner().invoke(app, ["batch", str(tmp_path)])
    assert res.exit_code == 1
    assert "failed" in res.stdout


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
        return _fake_translate_result(tmp_path, [])

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


# --- batch --no-resume wiring (regression) -------------------------------------------


def test_batch_forwards_no_resume_to_translate(tmp_path, monkeypatch):
    # batch_translate forwards its kwargs to translate_subtitle; assert --no-resume reaches it.
    captured: dict = {}

    def fake_translate(path, **kwargs):
        captured.update(kwargs)
        return _fake_translate_result(tmp_path, [])

    monkeypatch.setattr(pipeline, "translate_subtitle", fake_translate)
    _one_line_srt(tmp_path / "ep01.en.srt")

    result = CliRunner().invoke(
        app, ["batch", str(tmp_path), "--glob", "*.srt", "--no-resume", "--provider", "identity"]
    )
    assert result.exit_code == 0
    assert captured.get("resume") is False

    captured.clear()
    CliRunner().invoke(app, ["batch", str(tmp_path), "--glob", "*.srt", "--provider", "identity"])
    assert captured.get("resume") is True  # default keeps the checkpoint


# --- --output must not overwrite the source (#5) -------------------------------------


def test_translate_refuses_to_overwrite_source(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "PROJECTS_DIR", tmp_path / "projects")
    src = tmp_path / "ep.srt"
    _one_line_srt(src)
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
    assert es.name == "es" and fr.name == "fr"
    assert es != fr  # a French run can't inherit the Spanish glossary


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
    _one_line_srt(src)

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


def test_atomic_write_text_respects_umask(tmp_path):
    import os

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


def test_translate_output_respects_umask(tmp_path, monkeypatch):
    import os

    monkeypatch.setattr(config, "PROJECTS_DIR", tmp_path / "projects")
    src = tmp_path / "ep.en.srt"
    _one_line_srt(src)
    old = os.umask(0o022)
    try:
        result = pipeline.translate_subtitle(
            src, provider="identity", interactive=False, project="P", fmt="srt"
        )
    finally:
        os.umask(old)
    assert result.output_path.stat().st_mode & 0o777 == 0o644


# --- batch skip is a typed error, not a message match (#10) --------------------------


def test_batch_skip_uses_typed_error_not_message_match(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "PROJECTS_DIR", tmp_path / "projects")
    _one_line_srt(tmp_path / "ep01.en.srt")

    # A PipelineError that merely mentions "already exists" for an unrelated reason must be
    # recorded as failed, not silently skipped (the old substring heuristic got this wrong).
    def misleading(path, **kwargs):
        raise pipeline.PipelineError("a conflicting term already exists in the glossary")

    monkeypatch.setattr(pipeline, "translate_subtitle", misleading)
    result = pipeline.batch_translate(
        tmp_path,
        globs=("*.srt",),
        provider="identity",
        target="es-latam",
        fmt="srt",
        interactive=False,
        project="P",
    )
    assert result.n_skipped == 0 and result.n_failed == 1

    # Only the typed OutputExistsError counts as a skip.
    def already(path, **kwargs):
        raise pipeline.OutputExistsError("Output already exists: x. Use --force to overwrite.")

    monkeypatch.setattr(pipeline, "translate_subtitle", already)
    result = pipeline.batch_translate(
        tmp_path,
        globs=("*.srt",),
        provider="identity",
        target="es-latam",
        fmt="srt",
        interactive=False,
        project="P",
    )
    assert result.n_skipped == 1 and result.n_failed == 0


# --- checkpoint signature keys on the effective model, not the --model flag (#2) ------


def test_checkpoint_signature_includes_effective_model(tmp_path, monkeypatch):
    import json
    import types

    monkeypatch.setattr(config, "PROJECTS_DIR", tmp_path / "projects")
    source = tmp_path / "ep.en.srt"
    _one_line_srt(source)

    prov = _FlakyProvider()
    prov.runner = types.SimpleNamespace(model="claude-opus-4-8")  # the runner's default model
    monkeypatch.setattr(pipeline, "make_provider", lambda *a, **k: prov)

    # --model omitted; the signature must still pin the model the runner actually used.
    pipeline.translate_subtitle(
        source, provider="claude", interactive=False, project="P", fmt="srt"
    )

    from translate_subs.workflows.support import episode_key

    cp = tmp_path / "projects" / "P" / "es" / episode_key(source) / "translations.checkpoint.json"
    signature = json.loads(cp.read_text())["signature"]
    assert signature == "claude|claude-opus-4-8|"
