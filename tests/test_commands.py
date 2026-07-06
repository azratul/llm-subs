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


def test_purge_cache_command(tmp_path, monkeypatch):
    from translate_subs import config

    work = tmp_path / "work"
    (work / "Show").mkdir(parents=True)
    (work / "Show" / "track.ass").write_text("x" * 2048, encoding="utf-8")
    (work / "loose.srt").write_text("y", encoding="utf-8")
    monkeypatch.setattr(config, "WORK_DIR", work)

    result = runner.invoke(app, ["purge-cache", "--yes"])
    assert result.exit_code == 0
    assert "Freed" in result.stdout
    # The cache directory itself remains; its contents are gone.
    assert work.exists()
    assert not list(work.iterdir())


def test_purge_cache_command_empty(tmp_path, monkeypatch):
    from translate_subs import config

    monkeypatch.setattr(config, "WORK_DIR", tmp_path / "missing")
    result = runner.invoke(app, ["purge-cache", "--yes"])
    assert result.exit_code == 0
    assert "already empty" in result.stdout


def test_doctor_command_exit_codes(monkeypatch):
    monkeypatch.setattr(
        system_cmd, "run_diagnostics", lambda provider=None, model=None: [Check("x", "ok", "")]
    )
    assert runner.invoke(app, ["doctor"]).exit_code == 0

    monkeypatch.setattr(
        system_cmd,
        "run_diagnostics",
        lambda provider=None, model=None: [Check("x", "fail", "boom")],
    )
    assert runner.invoke(app, ["doctor"]).exit_code == 1


def test_doctor_fix_repairs_then_checks(monkeypatch):
    monkeypatch.setattr(
        system_cmd, "run_diagnostics", lambda provider=None, model=None: [Check("x", "ok", "")]
    )
    monkeypatch.setattr(system_cmd, "fix_permissions", lambda: (3, []))
    out = runner.invoke(app, ["doctor", "--fix"])
    assert out.exit_code == 0
    assert "Fixed 3 state/cache entries" in out.stdout

    # Without --fix nothing is repaired.
    called: list[int] = []
    monkeypatch.setattr(system_cmd, "fix_permissions", lambda: called.append(1) or (0, []))
    assert runner.invoke(app, ["doctor"]).exit_code == 0
    assert not called


def test_doctor_fix_json_reports_repairs_and_errors(monkeypatch):
    import json

    monkeypatch.setattr(
        system_cmd, "run_diagnostics", lambda provider=None, model=None: [Check("x", "ok", "")]
    )
    monkeypatch.setattr(
        system_cmd, "fix_permissions", lambda: (2, ["/state/stuck.json: Operation not permitted"])
    )
    out = runner.invoke(app, ["doctor", "--fix", "--json"])
    assert out.exit_code == 0
    payload = json.loads(out.stdout)
    assert payload["fixed_permissions"] == 2
    assert payload["fix_errors"] == ["/state/stuck.json: Operation not permitted"]

    # Without --fix the JSON document keeps its original shape.
    plain = json.loads(runner.invoke(app, ["doctor", "--json"]).stdout)
    assert "fixed_permissions" not in plain


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
    monkeypatch.setattr(cli, "compact_memory", lambda project, target, **kw: result)
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


# --- project: projects / purge-project ----------------------------------------------


def test_projects_and_purge_project_commands(tmp_path, monkeypatch):
    import json

    from translate_subs import config

    monkeypatch.setattr(config, "PROJECTS_DIR", tmp_path)
    state = tmp_path / "Serie" / "es-latam"
    state.mkdir(parents=True)
    (state / "glossary.json").write_text("{}", encoding="utf-8")

    listed = runner.invoke(app, ["projects"])
    assert listed.exit_code == 0
    assert "Serie" in listed.stdout and "es-latam" in listed.stdout

    as_json = runner.invoke(app, ["projects", "--json"])
    payload = json.loads(as_json.stdout)
    assert payload[0]["name"] == "Serie" and payload[0]["files"] == 1

    # Declining the confirmation aborts without touching anything.
    aborted = runner.invoke(app, ["purge-project", "Serie"], input="n\n")
    assert aborted.exit_code == 1 and "Aborted" in aborted.stdout
    assert state.exists()

    purged = runner.invoke(app, ["purge-project", "Serie", "--yes"])
    assert purged.exit_code == 0 and "Freed" in purged.stdout
    assert not (tmp_path / "Serie").exists()

    # An unknown project is an error (likely a typo), not a silent no-op.
    missing = runner.invoke(app, ["purge-project", "Serie", "--yes"])
    assert missing.exit_code == 1

    empty = runner.invoke(app, ["projects"])
    assert empty.exit_code == 0 and "No stored projects" in empty.stdout
