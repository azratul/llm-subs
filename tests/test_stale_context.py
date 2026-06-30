"""Stale-context detection and the analysis/memory workflows behind it."""

from __future__ import annotations

import pysubs2
import pytest

from translate_subs import config, pipeline
from translate_subs.ai.analysis import EpisodeContext, source_digest
from translate_subs.domain.models import TranslatableUnit
from translate_subs.subs import document
from translate_subs.subs.extractor import extract_units
from translate_subs.workflows.support import context_path, episode_key

_ANALYSIS_REPLY = (
    '{"episode_summary": "A test.", '
    '"characters": [{"name": "Aya", "gender": "female"}], '
    '"glossary": {"Sword": "Espada"}, "translation_rules": ["keep names"]}'
)


def _unit(i: int, text: str) -> TranslatableUnit:
    return TranslatableUnit(
        id=f"{i:04d}", event_index=i, start=i * 1000, end=i * 1000 + 500, style="Default", text=text
    )


def _save_srt(path, *texts):
    subs = pysubs2.SSAFile()
    for i, text in enumerate(texts):
        subs.events.append(pysubs2.SSAEvent(start=i * 2000, end=i * 2000 + 1500, text=text))
    subs.save(str(path), format_="srt")


def test_legacy_context_without_schema_version_loads_as_v1():
    # Files written before schema_version existed have no such key; they must still load,
    # defaulting to version 1, so the field is a safe forward-compatible addition.
    legacy = '{"episode_summary": "Old.", "glossary": {"Sword": "Espada"}}'
    ctx = EpisodeContext.model_validate_json(legacy)
    assert ctx.schema_version == 1
    assert ctx.glossary == {"Sword": "Espada"}


def test_source_digest_is_stable_and_content_sensitive():
    a = [_unit(1, "Hello"), _unit(2, "World")]
    assert source_digest(a) == source_digest([_unit(1, "Hello"), _unit(2, "World")])
    assert source_digest(a) != source_digest([_unit(1, "Hello"), _unit(2, "Changed")])


def test_source_digest_is_sensitive_to_speaker():
    # Reassigning a line to another character changes gender/register without touching the text.
    base = TranslatableUnit(
        id="0001", event_index=0, start=0, end=500, style="Default", speaker="Aya", text="Hello"
    )
    moved = base.model_copy(update={"speaker": "Ken"})
    assert source_digest([base]) != source_digest([moved])


def test_analyze_stores_and_merges_source_hash(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "PROJECTS_DIR", tmp_path / "projects")
    src = tmp_path / "ep.en.srt"
    _save_srt(src, "Aya draws her sword.")

    result = pipeline.analyze_subtitle(
        src, project="P", interactive=False, runner=lambda _prompt: _ANALYSIS_REPLY
    )
    units = extract_units(document.load(src))
    assert result.context.source_hash == source_digest(units)
    saved = EpisodeContext.model_validate_json(result.context_path.read_text("utf-8"))
    assert saved.source_hash == source_digest(units)
    # The analysis findings were merged into series memory.
    assert result.merge.applied


def test_context_not_written_if_memory_merge_fails(tmp_path, monkeypatch):
    # The context file must be persisted *after* the memory merge: it is what `skip_if_current`
    # trusts to decide an episode is already analyzed. If it were written first and the merge then
    # crashed, a later `--pre-analyze` would skip this episode and lose its findings. So a merge
    # failure must leave no context file behind.
    from translate_subs.workflows import memory as memory_workflow

    monkeypatch.setattr(config, "PROJECTS_DIR", tmp_path / "projects")
    src = tmp_path / "ep.en.srt"
    _save_srt(src, "Aya draws her sword.")

    def _boom(*_args, **_kwargs):
        raise RuntimeError("merge failed")

    monkeypatch.setattr(memory_workflow, "merge_into_memory", _boom)

    with pytest.raises(RuntimeError, match="merge failed"):
        pipeline.analyze_subtitle(
            src, project="P", interactive=False, runner=lambda _prompt: _ANALYSIS_REPLY
        )

    assert not context_path("P", "es-latam", episode_key(src)).exists()


def test_translate_flags_stale_context(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "PROJECTS_DIR", tmp_path / "projects")
    src = tmp_path / "ep.en.srt"
    _save_srt(src, "Hello.")

    ctx_file = context_path("P", "es-latam", episode_key(src))
    ctx_file.parent.mkdir(parents=True, exist_ok=True)
    # A context whose stored hash does not match this source -> stale.
    ctx_file.write_text(EpisodeContext(source_hash="0" * 16).model_dump_json(), encoding="utf-8")

    result = pipeline.translate_subtitle(
        src, provider="identity", project="P", interactive=False, fmt="srt"
    )
    assert result.context_used is True
    assert result.context_stale is True


def test_translate_not_stale_when_hash_matches(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "PROJECTS_DIR", tmp_path / "projects")
    src = tmp_path / "ep.en.srt"
    _save_srt(src, "Hello.")
    units = extract_units(document.load(src))

    ctx_file = context_path("P", "es-latam", episode_key(src))
    ctx_file.parent.mkdir(parents=True, exist_ok=True)
    ctx_file.write_text(
        EpisodeContext(source_hash=source_digest(units)).model_dump_json(), encoding="utf-8"
    )

    result = pipeline.translate_subtitle(
        src, provider="identity", project="P", interactive=False, fmt="srt"
    )
    assert result.context_used is True
    assert result.context_stale is False


def test_legacy_context_without_hash_is_never_stale(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "PROJECTS_DIR", tmp_path / "projects")
    src = tmp_path / "ep.en.srt"
    _save_srt(src, "Hello.")

    ctx_file = context_path("P", "es-latam", episode_key(src))
    ctx_file.parent.mkdir(parents=True, exist_ok=True)
    # An older context file has no source_hash; we can't tell, so we don't warn.
    ctx_file.write_text(EpisodeContext().model_dump_json(), encoding="utf-8")

    result = pipeline.translate_subtitle(
        src, provider="identity", project="P", interactive=False, fmt="srt"
    )
    assert result.context_stale is False


def test_update_and_compact_memory_workflows(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "PROJECTS_DIR", tmp_path / "projects")
    src = tmp_path / "ep.en.srt"
    _save_srt(src, "Aya draws her sword.")
    pipeline.analyze_subtitle(
        src, project="P", interactive=False, runner=lambda _prompt: _ANALYSIS_REPLY
    )

    # Re-merge the existing context without an LLM call.
    again = pipeline.update_memory(src, project="P", interactive=False)
    assert again.project_dir.exists()

    # Compaction runs and returns a report.
    compacted = pipeline.compact_memory("P")
    assert compacted.project_dir.exists()
