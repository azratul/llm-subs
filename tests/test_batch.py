"""Batch discovery/translation/analysis: skip/stale/failed accounting and CLI wiring."""

from __future__ import annotations

from pathlib import Path

import pysubs2
import pytest
from typer.testing import CliRunner

from tests.helpers import fake_translate_result, one_line_srt
from translate_subs import config, pipeline
from translate_subs.cli import app

# --- batch / directory translation ---------------------------------------------------


def test_discover_inputs_filters_pattern_and_skips_outputs(tmp_path):
    one_line_srt(tmp_path / "ep01.en.srt")
    one_line_srt(tmp_path / "ep02.en.srt")
    one_line_srt(tmp_path / "ep01.es-latam.srt")  # a previous output — must not be picked up
    (tmp_path / "note.txt").write_text("x", encoding="utf-8")

    found = pipeline.discover_inputs(tmp_path, globs=("*.srt",), target="es-latam")
    names = [p.name for p in found]
    assert names == ["ep01.en.srt", "ep02.en.srt"]


def test_discover_inputs_recursive(tmp_path):
    (tmp_path / "S01").mkdir()
    one_line_srt(tmp_path / "S01" / "ep01.en.srt")
    one_line_srt(tmp_path / "top.en.srt")

    flat = pipeline.discover_inputs(tmp_path, globs=("*.srt",), recursive=False)
    assert [p.name for p in flat] == ["top.en.srt"]
    deep = pipeline.discover_inputs(tmp_path, globs=("*.srt",), recursive=True)
    assert sorted(p.name for p in deep) == ["ep01.en.srt", "top.en.srt"]


def test_discover_inputs_rejects_non_directory(tmp_path):
    f = tmp_path / "x.srt"
    one_line_srt(f)
    with pytest.raises(pipeline.PipelineError, match="Not a directory"):
        pipeline.discover_inputs(f)


def test_batch_translate_skips_done_and_continues_past_failures(tmp_path, monkeypatch):
    from translate_subs.io.source_resolver import SourceError

    monkeypatch.setattr(config, "PROJECTS_DIR", tmp_path / "projects")
    for n in ("ep01.en.srt", "ep02.en.srt", "ep03.en.srt"):
        one_line_srt(tmp_path / n)
    # Pre-create ep02's output so it is skipped without --force.
    one_line_srt(tmp_path / "ep02.es-latam.srt")

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


def test_batch_reports_stale_when_source_changed(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "PROJECTS_DIR", tmp_path / "projects")
    src = tmp_path / "ep01.en.srt"
    one_line_srt(src)
    common = dict(
        globs=("*.srt",),
        provider="identity",
        target="es-latam",
        fmt="srt",
        interactive=False,
        project="P",
    )

    first = pipeline.batch_translate(tmp_path, **common)
    assert first.n_translated == 1 and first.n_stale == 0
    output = tmp_path / "ep01.es-latam.srt"
    written = output.read_text("utf-8")

    # Edit the source after translating: the existing output is now stale.
    subs = pysubs2.SSAFile()
    subs.events.append(pysubs2.SSAEvent(start=0, end=2000, text="A different line entirely."))
    subs.save(str(src), format_="srt")

    second = pipeline.batch_translate(tmp_path, **common)
    item = {i.input_path.name: i for i in second.items}["ep01.en.srt"]
    assert item.status == "stale"
    assert second.n_stale == 1 and second.n_translated == 0 and second.n_failed == 0
    assert output.read_text("utf-8") == written  # stale output is warned about, never overwritten


def test_batch_dry_run_plans_without_writing(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "PROJECTS_DIR", tmp_path / "projects")
    one_line_srt(tmp_path / "ep01.en.srt")
    one_line_srt(tmp_path / "ep02.en.srt")

    result = pipeline.batch_translate(
        tmp_path,
        globs=("*.srt",),
        provider="identity",
        target="es-latam",
        fmt="srt",
        interactive=False,
        project="P",
        dry_run=True,
    )

    assert [i.status for i in result.items] == ["planned", "planned"]
    assert result.n_planned == 2 and result.n_translated == 0 and result.n_failed == 0
    first = result.items[0]
    assert first.output_path == tmp_path / "ep01.es-latam.srt"
    assert first.n_units == 1 and first.n_jobs == 1
    # The preview must leave no trace: no output next to the media, no project state on disk.
    assert not (tmp_path / "ep01.es-latam.srt").exists()
    assert not (tmp_path / "ep02.es-latam.srt").exists()
    assert not (tmp_path / "projects").exists()


def test_batch_dry_run_classifies_existing_outputs(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "PROJECTS_DIR", tmp_path / "projects")
    src = tmp_path / "ep01.en.srt"
    one_line_srt(src)
    common = dict(
        globs=("*.srt",),
        provider="identity",
        target="es-latam",
        fmt="srt",
        interactive=False,
        project="P",
    )

    pipeline.batch_translate(tmp_path, **common)  # real run: writes output + manifest
    output = tmp_path / "ep01.es-latam.srt"
    written = output.read_text("utf-8")

    current = pipeline.batch_translate(tmp_path, dry_run=True, **common)
    assert current.items[0].status == "skipped"

    # Edit the source: the dry run reports the output stale, exactly like a real run would.
    subs = pysubs2.SSAFile()
    subs.events.append(pysubs2.SSAEvent(start=0, end=2000, text="A different line entirely."))
    subs.save(str(src), format_="srt")

    stale = pipeline.batch_translate(tmp_path, dry_run=True, **common)
    assert stale.items[0].status == "stale"
    assert output.read_text("utf-8") == written  # a dry run never rewrites anything


def test_batch_cli_dry_run_previews_without_llm(tmp_path, monkeypatch):
    import json as _json

    from translate_subs import cli

    monkeypatch.setattr(config, "PROJECTS_DIR", tmp_path / "projects")
    season = tmp_path / "season"
    season.mkdir()
    one_line_srt(season / "e1.en.srt")

    def _fail_analyze(*args, **kwargs):
        raise AssertionError("--pre-analyze must be skipped under --dry-run (it calls the LLM)")

    monkeypatch.setattr(cli, "batch_analyze", _fail_analyze)

    args = [
        "batch",
        str(season),
        "--glob",
        "*.srt",
        "--provider",
        "identity",
        "--project",
        "Q",
        "--dry-run",
        "--pre-analyze",
    ]
    res = CliRunner().invoke(app, [*args, "--json"])
    assert res.exit_code == 0
    payload = _json.loads(res.stdout)
    assert payload["dry_run"] is True
    assert payload["summary"]["planned"] == 1 and payload["summary"]["translated"] == 0
    item = payload["items"][0]
    assert item["status"] == "planned" and item["units"] == 1 and item["blocks"] == 1
    assert not (season / "e1.es-latam.srt").exists()

    human = CliRunner().invoke(app, args)
    assert human.exit_code == 0
    assert "Dry run" in human.stdout and "planned" in human.stdout
    assert "--pre-analyze is skipped" in human.stdout
    assert not (season / "e1.es-latam.srt").exists()


def test_batch_out_dir_mirrors_subfolders_no_collision(tmp_path, monkeypatch):
    # Same-named episodes in different season folders must not collapse onto one flat output.
    monkeypatch.setattr(config, "PROJECTS_DIR", tmp_path / "projects")
    (tmp_path / "Season 1").mkdir()
    (tmp_path / "Season 2").mkdir()
    one_line_srt(tmp_path / "Season 1" / "Episode 01.en.srt")
    one_line_srt(tmp_path / "Season 2" / "Episode 01.en.srt")
    out_dir = tmp_path / "out"

    result = pipeline.batch_translate(
        tmp_path,
        globs=("*.srt",),
        recursive=True,
        provider="identity",
        target="es-latam",
        fmt="srt",
        interactive=False,
        project="P",
        out_dir=out_dir,
    )

    assert result.n_translated == 2 and result.n_skipped == 0
    # Each episode lands under its own mirrored season directory, not one shared filename.
    assert (out_dir / "Season 1" / "Episode 01.es-latam.srt").exists()
    assert (out_dir / "Season 2" / "Episode 01.es-latam.srt").exists()


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


def test_batch_cli_fail_on_stale(tmp_path, monkeypatch):
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
                BatchItem(tmp_path / "ep02.mkv", "stale", error="source changed"),
            ]
        ),
    )
    # A stale output is a warning by default (exit 0) but fails under --fail-on-stale.
    default = CliRunner().invoke(app, ["batch", str(tmp_path)])
    assert default.exit_code == 0
    strict = CliRunner().invoke(app, ["batch", str(tmp_path), "--fail-on-stale"])
    assert strict.exit_code == 1
    assert "stale" in strict.stdout


def test_batch_translate_isolates_content_error_but_aborts_systemic(tmp_path):
    from translate_subs.ai.provider import ProviderError
    from translate_subs.workflows.translation import batch_translate

    eps = [tmp_path / "a.mkv", tmp_path / "b.mkv"]

    def discover(*_a, **_k):
        return eps

    def content_fail(_path, **_k):  # unparseable reply for THIS episode
        raise ProviderError("bad json", retryable=True, category="content")

    res = batch_translate(tmp_path, discover_inputs_fn=discover, translate_fn=content_fail)
    assert res.n_failed == 2 and res.n_translated == 0  # each failed, the season continued

    # Every systemic cause (and an unclassified one) aborts the whole run instead of continuing.
    for category, retryable in [
        ("quota", True),
        ("auth", False),
        ("service", True),
        ("unknown", True),
    ]:

        def systemic_fail(_path, _cat=category, _r=retryable, **_k):
            raise ProviderError("systemic", retryable=_r, category=_cat)

        with pytest.raises(ProviderError):
            batch_translate(tmp_path, discover_inputs_fn=discover, translate_fn=systemic_fail)


def test_batch_analyze_isolates_content_error_but_aborts_systemic(tmp_path):
    from translate_subs.ai.provider import ProviderError
    from translate_subs.workflows.translation import batch_analyze

    eps = [tmp_path / "a.mkv", tmp_path / "b.mkv"]

    def discover(*_a, **_k):
        return eps

    def content_fail(_path, **_k):
        raise ProviderError("bad json", retryable=True, category="content")

    res = batch_analyze(tmp_path, discover_inputs_fn=discover, analyze_fn=content_fail)
    assert res.n_failed == 2 and res.n_analyzed == 0

    def auth_fail(_path, **_k):
        raise ProviderError("unauthorized", retryable=False, category="auth")

    with pytest.raises(ProviderError):
        batch_analyze(tmp_path, discover_inputs_fn=discover, analyze_fn=auth_fail)


def test_cli_json_flags(tmp_path, monkeypatch):
    import json as _json

    from translate_subs.commands import system as system_cmd
    from translate_subs.diagnostics import Check

    monkeypatch.setattr(config, "PROJECTS_DIR", tmp_path / "projects")

    # doctor --json (mocked checks for determinism)
    monkeypatch.setattr(
        system_cmd, "run_diagnostics", lambda provider=None, model=None: [Check("media", "ok", "d")]
    )
    res = CliRunner().invoke(app, ["doctor", "--json"])
    assert res.exit_code == 0
    payload = _json.loads(res.stdout)
    assert payload["ok"] is True and payload["checks"][0]["name"] == "media"

    subs = pysubs2.SSAFile()
    subs.events.append(pysubs2.SSAEvent(start=0, end=1000, text="Hi"))

    # validate --json
    ok_file = tmp_path / "ok.srt"
    subs.save(str(ok_file), format_="srt")
    res = CliRunner().invoke(app, ["validate", str(ok_file), "--json"])
    assert res.exit_code == 0 and _json.loads(res.stdout)["ok"] is True

    # project-status --json
    src = tmp_path / "ep.en.srt"
    subs.save(str(src), format_="srt")
    pipeline.translate_subtitle(src, provider="identity", interactive=False, fmt="srt", project="P")
    res = CliRunner().invoke(app, ["project-status", "P", "--json"])
    payload = _json.loads(res.stdout)
    assert payload["project"] == "P" and payload["episodes"][0]["outputs"]

    # batch --json
    season = tmp_path / "season"
    season.mkdir()
    subs.save(str(season / "e1.en.srt"), format_="srt")
    res = CliRunner().invoke(
        app,
        [
            "batch",
            str(season),
            "--glob",
            "*.srt",
            "--provider",
            "identity",
            "--project",
            "Q",
            "--json",
        ],
    )
    payload = _json.loads(res.stdout)
    assert payload["summary"]["translated"] == 1 and payload["items"][0]["status"] == "translated"


def test_batch_json_emits_json_on_error(tmp_path):
    import json as _json

    # A top-level failure (here: not a directory) must still be valid JSON on stdout, not a Rich
    # error line that breaks a parsing consumer.
    res = CliRunner().invoke(
        app, ["batch", str(tmp_path / "does-not-exist"), "--json", "--provider", "identity"]
    )
    assert res.exit_code == 1
    assert "error" in _json.loads(res.stdout)


def test_project_status_cli_smoke(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "PROJECTS_DIR", tmp_path / "projects")
    src = tmp_path / "ep.en.srt"
    subs = pysubs2.SSAFile()
    subs.events.append(pysubs2.SSAEvent(start=0, end=1500, text="Hello."))
    subs.save(str(src), format_="srt")
    pipeline.translate_subtitle(src, provider="identity", project="P", interactive=False, fmt="srt")

    ok = CliRunner().invoke(app, ["project-status", "P"])
    assert ok.exit_code == 0
    assert "Glossary" in ok.stdout

    missing = CliRunner().invoke(app, ["project-status", "Nope"])
    assert missing.exit_code == 1


# --- batch --no-resume wiring (regression) -------------------------------------------


def test_batch_forwards_no_resume_to_translate(tmp_path, monkeypatch):
    # batch_translate forwards its kwargs to translate_subtitle; assert --no-resume reaches it.
    captured: dict = {}

    def fake_translate(path, **kwargs):
        captured.update(kwargs)
        return fake_translate_result(tmp_path, [])

    monkeypatch.setattr(pipeline, "translate_subtitle", fake_translate)
    one_line_srt(tmp_path / "ep01.en.srt")

    result = CliRunner().invoke(
        app, ["batch", str(tmp_path), "--glob", "*.srt", "--no-resume", "--provider", "identity"]
    )
    assert result.exit_code == 0
    assert captured.get("resume") is False

    captured.clear()
    CliRunner().invoke(app, ["batch", str(tmp_path), "--glob", "*.srt", "--provider", "identity"])
    assert captured.get("resume") is True  # default keeps the checkpoint


# --- batch skip is a typed error, not a message match (#10) --------------------------


def test_batch_skip_uses_typed_error_not_message_match(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "PROJECTS_DIR", tmp_path / "projects")
    one_line_srt(tmp_path / "ep01.en.srt")

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


# --- batch_analyze and --pre-analyze ------------------------------------------------


def test_batch_analyze_analyzes_all_and_continues_past_failures(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "PROJECTS_DIR", tmp_path / "projects")
    for n in ("ep01.en.srt", "ep02.en.srt", "ep03.en.srt"):
        one_line_srt(tmp_path / n)

    calls: list[str] = []

    def fake_analyze(path, **kwargs):
        name = Path(path).name
        calls.append(name)
        if name == "ep03.en.srt":
            raise pipeline.PipelineError("no track")

    monkeypatch.setattr(pipeline, "analyze_subtitle", fake_analyze)

    seen: list[str] = []
    result = pipeline.batch_analyze(
        tmp_path,
        globs=("*.srt",),
        on_episode=lambda i, n, p: seen.append(p.name),
        target="es-latam",
        provider="claude",
        project="P",
        interactive=False,
    )

    assert result.n_analyzed == 2
    assert result.n_failed == 1
    assert calls == seen == ["ep01.en.srt", "ep02.en.srt", "ep03.en.srt"]
    failed = next(i for i in result.items if i.status == "failed")
    assert "no track" in failed.error


def test_batch_cli_pre_analyze_runs_analyze_then_translate(tmp_path, monkeypatch):
    from translate_subs import cli
    from translate_subs.pipeline import AnalyzeBatchResult, BatchItem, BatchResult
    from translate_subs.workflows.models import AnalyzeBatchItem

    analyze_calls: list[str] = []
    translate_calls: list[str] = []

    def fake_batch_analyze(directory, *, on_episode=None, **kwargs):
        r = AnalyzeBatchResult()
        for p in sorted(tmp_path.glob("*.srt")):
            if on_episode:
                on_episode(1, 1, p)
            analyze_calls.append(p.name)
            r.items.append(AnalyzeBatchItem(p, "analyzed"))
        return r

    def fake_batch_translate(directory, *, on_episode=None, **kwargs):
        r = BatchResult()
        for p in sorted(tmp_path.glob("*.srt")):
            if on_episode:
                on_episode(1, 1, p)
            translate_calls.append(p.name)
            r.items.append(BatchItem(p, "translated", output_path=p))
        return r

    monkeypatch.setattr(cli, "batch_analyze", fake_batch_analyze)
    monkeypatch.setattr(cli, "batch_translate", fake_batch_translate)
    one_line_srt(tmp_path / "ep01.en.srt")

    from typer.testing import CliRunner

    runner = CliRunner()
    out = runner.invoke(cli.app, ["batch", str(tmp_path), "--pre-analyze"])
    assert out.exit_code == 0
    assert analyze_calls == ["ep01.en.srt"]
    assert translate_calls == ["ep01.en.srt"]
    assert "Phase 1/2" in out.stdout
    assert "Phase 2/2" in out.stdout


def test_analyze_subtitle_forwards_strict_lang_to_source_resolution(tmp_path):
    """A wrong-language source must be rejected *before* analysis touches series memory.

    Without this, `batch --pre-analyze --strict-lang` would merge the wrong language's
    characters/glossary into the shared memory and only then fail the translate pass.
    """
    from translate_subs.io.source_resolver import SourceError
    from translate_subs.workflows import memory as memory_workflows

    received: dict = {}

    def fake_resolve(input_path, **kwargs):
        received.update(kwargs)
        raise SourceError("no 'en' subtitle track")

    with pytest.raises(SourceError):
        memory_workflows.analyze_subtitle(
            tmp_path / "ep01.mkv",
            strict_lang=True,
            resolve_source_fn=fake_resolve,
            ai_runner_factory=lambda provider, **kwargs: lambda prompt: "",
        )
    assert received["strict_lang"] is True


def test_batch_cli_pre_analyze_forwards_strict_lang(tmp_path, monkeypatch):
    from translate_subs import cli
    from translate_subs.pipeline import AnalyzeBatchResult, BatchResult

    analyze_kwargs: dict = {}

    def fake_batch_analyze(directory, *, on_episode=None, **kwargs):
        analyze_kwargs.update(kwargs)
        return AnalyzeBatchResult()

    monkeypatch.setattr(cli, "batch_analyze", fake_batch_analyze)
    monkeypatch.setattr(cli, "batch_translate", lambda directory, **kwargs: BatchResult())
    one_line_srt(tmp_path / "ep01.en.srt")

    runner = CliRunner()
    out = runner.invoke(app, ["batch", str(tmp_path), "--pre-analyze", "--strict-lang"])
    assert out.exit_code == 0
    assert analyze_kwargs["strict_lang"] is True


def test_batch_translate_aborts_on_provider_error(tmp_path, monkeypatch):
    """A ProviderError propagates out of batch_translate instead of being swallowed."""
    from translate_subs.ai.provider import ProviderError

    monkeypatch.setattr(config, "PROJECTS_DIR", tmp_path / "projects")
    for n in ("ep01.en.srt", "ep02.en.srt"):
        one_line_srt(tmp_path / n)

    def fake_translate(path, **kwargs):
        raise ProviderError("quota exceeded", retryable=False)

    monkeypatch.setattr(pipeline, "translate_subtitle", fake_translate)

    with pytest.raises(ProviderError, match="quota exceeded"):
        pipeline.batch_translate(
            tmp_path,
            globs=("*.srt",),
            provider="identity",
            target="es-latam",
            fmt="srt",
            interactive=False,
            project="P",
        )


def test_batch_analyze_aborts_on_provider_error(tmp_path, monkeypatch):
    """A ProviderError propagates out of batch_analyze instead of being swallowed."""
    from translate_subs.ai.provider import ProviderError

    monkeypatch.setattr(config, "PROJECTS_DIR", tmp_path / "projects")
    for n in ("ep01.en.srt", "ep02.en.srt"):
        one_line_srt(tmp_path / n)

    def fake_analyze(path, **kwargs):
        raise ProviderError("quota exceeded", retryable=False)

    monkeypatch.setattr(pipeline, "analyze_subtitle", fake_analyze)

    with pytest.raises(ProviderError, match="quota exceeded"):
        pipeline.batch_analyze(
            tmp_path,
            globs=("*.srt",),
            target="es-latam",
            provider="claude",
            project="P",
            interactive=False,
        )


def test_batch_analyze_skips_current_episodes(tmp_path, monkeypatch):
    """Episodes whose context.json matches the current source are skipped, not re-analyzed."""
    from translate_subs.workflows.models import AnalysisCurrentError

    monkeypatch.setattr(config, "PROJECTS_DIR", tmp_path / "projects")
    for n in ("ep01.en.srt", "ep02.en.srt"):
        one_line_srt(tmp_path / n)

    calls: list[str] = []

    def fake_analyze(path, **kwargs):
        name = Path(path).name
        calls.append(name)
        if kwargs.get("skip_if_current") and name == "ep01.en.srt":
            raise AnalysisCurrentError("already current")

    monkeypatch.setattr(pipeline, "analyze_subtitle", fake_analyze)

    result = pipeline.batch_analyze(
        tmp_path,
        globs=("*.srt",),
        target="es-latam",
        provider="claude",
        project="P",
        interactive=False,
        skip_if_current=True,
    )

    assert result.n_skipped == 1
    assert result.n_analyzed == 1
    assert result.n_failed == 0
    skipped = next(i for i in result.items if i.status == "skipped")
    assert skipped.input_path.name == "ep01.en.srt"
