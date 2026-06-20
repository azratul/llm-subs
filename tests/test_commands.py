"""CLI wiring smoke tests for the command layer.

The refactor moved every command callback into `translate_subs/commands/`; these check that each
one reads the right result fields, prints, and exits correctly — cheap insurance against a broken
wire-up that type-checking and linting wouldn't catch. The heavy logic is tested in its own suite,
so the underlying workflow functions are stubbed here.
"""

from __future__ import annotations

import pysubs2
from typer.testing import CliRunner

from translate_subs import cli
from translate_subs.cli import app
from translate_subs.commands import system as system_cmd
from translate_subs.diagnostics import Check
from translate_subs.io.media_probe import SubtitleTrack
from translate_subs.memory.compact import CompactReport
from translate_subs.memory.merge import MergeReport
from translate_subs.review.models import ReviewReport
from translate_subs.workflows.models import (
    AnalyzeResult,
    CompactMemoryResult,
    ResolveConflictsResult,
    ReviewResult,
    TightenResult,
    UpdateMemoryResult,
)

runner = CliRunner()


# --- system: validate / doctor / probe ----------------------------------------------


def test_validate_command_accepts_valid_and_rejects_broken(tmp_path):
    good = tmp_path / "ok.srt"
    subs = pysubs2.SSAFile()
    subs.events.append(pysubs2.SSAEvent(start=0, end=2000, text="Hola."))
    subs.save(str(good), format_="srt")
    ok = runner.invoke(app, ["validate", str(good)])
    assert ok.exit_code == 0 and "Valid" in ok.stdout

    missing = runner.invoke(app, ["validate", str(tmp_path / "nope.srt")])
    assert missing.exit_code == 1


def test_doctor_command_exit_codes(monkeypatch):
    monkeypatch.setattr(system_cmd, "run_diagnostics", lambda provider=None: [Check("x", "ok", "")])
    assert runner.invoke(app, ["doctor"]).exit_code == 0

    monkeypatch.setattr(
        system_cmd, "run_diagnostics", lambda provider=None: [Check("x", "fail", "boom")]
    )
    assert runner.invoke(app, ["doctor"]).exit_code == 1


def test_probe_command_lists_tracks_and_handles_none(monkeypatch):
    monkeypatch.setattr(system_cmd, "probe_subtitle_tracks", lambda media: [])
    empty = runner.invoke(app, ["probe", "movie.mkv"])
    assert empty.exit_code == 0 and "No subtitle tracks" in empty.stdout

    track = SubtitleTrack(
        rel_index=0,
        stream_index=2,
        codec="subrip",  # is_text is derived from the codec
        language="eng",
        title="Full",
        default=True,
        forced=False,
    )
    monkeypatch.setattr(system_cmd, "probe_subtitle_tracks", lambda media: [track])
    listed = runner.invoke(app, ["probe", "movie.mkv"])
    assert listed.exit_code == 0 and "subrip" in listed.stdout


# --- quality: review / tighten -------------------------------------------------------


def test_review_command_reports_and_warns_on_stale(tmp_path, monkeypatch):
    result = ReviewResult(
        report=ReviewReport(episode="ep", findings=[]),
        report_path=tmp_path / "episode.review.md",
        translated_path=tmp_path / "ep.es.ass",
        n_lines=3,
        n_applied=0,
        mapping_aligned=True,
        context_stale=True,
    )
    monkeypatch.setattr(cli, "review_translation", lambda *a, **k: result)
    out = runner.invoke(app, ["review", "src.ass", "tgt.ass"])
    assert out.exit_code == 0
    assert "Reviewed" in out.stdout
    assert "analyzed from a different" in out.stdout  # the stale-context warning


def test_tighten_command_reports_residual(tmp_path, monkeypatch):
    result = TightenResult(
        report_path=tmp_path / "episode.readability.md",
        translated_path=tmp_path / "ep.es.srt",
        n_subs=10,
        n_flagged=2,
        n_compacted=2,
        n_applied=0,
        n_residual=1,
    )
    monkeypatch.setattr(cli, "tighten_subtitle", lambda *a, **k: result)
    out = runner.invoke(app, ["tighten", "ep.es.srt"])
    assert out.exit_code == 0
    assert "still over limit" in out.stdout


# --- project: analyze / update-memory / compact-memory / resolve-conflicts -----------


def test_analyze_command_prints_counts(tmp_path, monkeypatch):
    from translate_subs.ai.analysis import EpisodeContext

    result = AnalyzeResult(
        source=None,
        context_path=tmp_path / "episode.context.json",
        context=EpisodeContext(glossary={"a": "b"}),
        n_units=5,
        merge=MergeReport(applied=["+ glossary: a -> b"], conflicts=[]),
        truncated_lines=0,
    )
    monkeypatch.setattr(cli, "analyze_subtitle", lambda *a, **k: result)
    out = runner.invoke(app, ["analyze", "ep.en.ass", "--yes"])
    assert out.exit_code == 0
    assert "Analyzed" in out.stdout


def test_update_memory_command(tmp_path, monkeypatch):
    result = UpdateMemoryResult(
        project_dir=tmp_path / "P",
        context_path=tmp_path / "episode.context.json",
        merge=MergeReport(applied=[], conflicts=[]),
    )
    monkeypatch.setattr(cli, "update_memory", lambda *a, **k: result)
    out = runner.invoke(app, ["update-memory", "ep.en.ass", "--yes"])
    assert out.exit_code == 0 and "Memory" in out.stdout


def test_compact_memory_command(tmp_path, monkeypatch):
    result = CompactMemoryResult(
        project_dir=tmp_path / "P",
        report=CompactReport(
            removed_identity_terms=1,
            removed_duplicate_terms=0,
            merged_characters=2,
            removed_empty_characters=1,
        ),
    )
    monkeypatch.setattr(cli, "compact_memory", lambda project, target: result)
    out = runner.invoke(app, ["compact-memory", "P"])
    assert out.exit_code == 0 and "Glossary" in out.stdout


def test_resolve_conflicts_command_empty_and_resolved(tmp_path, monkeypatch):
    monkeypatch.setattr(
        cli,
        "resolve_conflicts",
        lambda project, prompt, target: ResolveConflictsResult(
            project_dir=tmp_path / "P", resolved=0, remaining=0
        ),
    )
    empty = runner.invoke(app, ["resolve-conflicts", "P"])
    assert empty.exit_code == 0 and "No conflicts" in empty.stdout

    monkeypatch.setattr(
        cli,
        "resolve_conflicts",
        lambda project, prompt, target: ResolveConflictsResult(
            project_dir=tmp_path / "P", resolved=2, remaining=1
        ),
    )
    some = runner.invoke(app, ["resolve-conflicts", "P"])
    assert some.exit_code == 0 and "Resolved" in some.stdout


def test_command_error_path_exits_one(monkeypatch):
    def boom(*a, **k):
        raise cli.PipelineError("nope")

    monkeypatch.setattr(cli, "compact_memory", boom)
    out = runner.invoke(app, ["compact-memory", "P"])
    assert out.exit_code == 1 and "Error" in out.stdout
