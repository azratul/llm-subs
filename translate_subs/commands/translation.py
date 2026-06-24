"""Single-file and batch translation command callbacks."""

from __future__ import annotations

import time
from pathlib import Path

import typer
from rich.progress import (
    BarColumn,
    MofNCompleteColumn,
    Progress,
    SpinnerColumn,
    TextColumn,
    TimeRemainingColumn,
)
from rich.table import Table

from translate_subs.pipeline import DEFAULT_BATCH_GLOBS


def _fmt_duration(seconds: float) -> str:
    s = int(seconds)
    if s >= 3600:
        return f"{s // 3600}h{(s % 3600) // 60:02d}m{s % 60:02d}s"
    if s >= 60:
        return f"{s // 60}m{s % 60:02d}s"
    return f"{s}s"


def _make_episode_callback(console, label: str = ""):
    """Return an on_episode callback that tracks timing and prints ETA hints."""
    _EMA_ALPHA = 0.3
    start = [time.perf_counter()]  # mutable cell; [0] = time when current episode began
    durations: list[float] = []
    ema: list[float] = []  # [0] holds the current EMA value once initialised

    def on_episode(index: int, total: int, path: Path) -> None:
        now = time.perf_counter()
        hint = ""
        if index > 1:
            elapsed = now - start[0]
            durations.append(elapsed)
            if not ema:
                ema.append(elapsed)
            else:
                ema[0] = _EMA_ALPHA * elapsed + (1 - _EMA_ALPHA) * ema[0]
            remaining = total - index + 1
            eta = _fmt_duration(remaining * ema[0])
            hint = f"  [dim](prev {_fmt_duration(elapsed)}, ETA ~{eta})[/dim]"
        start[0] = now
        prefix = f"[Analyze {index}/{total}]" if label else f"[{index}/{total}]"
        console.print(f"[cyan]\\{prefix}[/cyan] {path.name}{hint}")

    def total_elapsed() -> float:
        return time.perf_counter() - start[0] + sum(durations)

    return on_episode, total_elapsed


def _runtime():
    from translate_subs import cli

    return cli


def translate(
    ctx: typer.Context,
    input: Path = typer.Argument(..., help="Subtitle (.ass/.srt/...) or video file."),
    target: str = typer.Option(
        "es-latam", help="Target language/variant, e.g. es-latam, en, fr-FR, ja."
    ),
    provider: str = typer.Option(
        "claude",
        help="claude | codex | antigravity | opencode | ollama | litellm | file-handoff | identity "
        "(identity is a passthrough copy for testing, not a translation).",
    ),
    model: str | None = typer.Option(
        None, "--model", help="Model id for the chosen CLI provider (else its default)."
    ),
    reasoning: str | None = typer.Option(
        None,
        "--reasoning",
        help="codex reasoning effort: minimal|low|medium|high|xhigh (default low).",
    ),
    retries: int = typer.Option(
        2, "--retries", min=0, help="Retries per block after an agent/JSON failure."
    ),
    track: int | None = typer.Option(None, help="Embedded track index (when several exist)."),
    lang: str = typer.Option("en", help="Preferred source language when picking a track."),
    out_dir: Path | None = typer.Option(
        None, "--out-dir", help="Output directory (defaults next to the original)."
    ),
    output: Path | None = typer.Option(
        None, "--output", help="Force a specific output path (suffix coerced to --format)."
    ),
    format: str = typer.Option(
        "ass",
        "--format",
        help="Output format: ass (default, keeps positioning) | srt (flat, merges overlaps).",
    ),
    project: str | None = typer.Option(None, help="Project/series name."),
    force: bool = typer.Option(
        False, "--force", "-f", help="Overwrite the output file if it already exists."
    ),
    strict_lang: bool = typer.Option(
        False,
        "--strict-lang",
        help="Fail instead of falling back to a different-language subtitle.",
    ),
    fail_on_untranslated: bool = typer.Option(
        False,
        "--fail-on-untranslated",
        help="Exit non-zero if any line kept the source text (provider returned empty).",
    ),
    no_resume: bool = typer.Option(
        False,
        "--no-resume",
        help="Ignore any saved block checkpoint and re-translate every block from scratch.",
    ),
    non_interactive: bool = typer.Option(
        False,
        "--non-interactive",
        "--yes",
        "-y",
        help="Do not prompt; resolve by heuristic/flags.",
    ),
):
    """Translate a subtitle and export <base>.<lang>.<format> (lang from --target)."""
    runtime = _runtime()
    try:
        overrides = runtime._project_overrides(ctx, project)
    except runtime._EXPECTED_ERRORS as exc:
        runtime.console.print(f"[red]Error:[/red] {exc}")
        raise typer.Exit(code=1)
    target = overrides.get("target", target)
    provider = overrides.get("provider", provider)
    model = overrides.get("model", model)
    reasoning = overrides.get("reasoning", reasoning)
    lang = overrides.get("lang", lang)
    format = overrides.get("format", format)

    def run(on_progress=None):
        return runtime.translate_subtitle(
            input,
            target=target,
            provider=provider,
            model=model,
            reasoning=reasoning,
            max_retries=retries,
            track_index=track,
            lang=lang,
            out_dir=out_dir,
            output=output,
            fmt=format,
            project=project,
            interactive=not non_interactive,
            force=force,
            strict_lang=strict_lang,
            resume=not no_resume,
            on_progress=on_progress,
        )

    try:
        if runtime.console.is_terminal:
            with Progress(
                SpinnerColumn(),
                TextColumn("[progress.description]{task.description}"),
                BarColumn(),
                MofNCompleteColumn(),
                TimeRemainingColumn(),
                console=runtime.console,
                transient=True,
            ) as progress:
                task = progress.add_task("Setting up…", total=None)

                def on_progress(event) -> None:
                    progress.update(
                        task,
                        completed=event.done,
                        total=event.total,
                        description=f"Block {event.block_id}"
                        + (" (cached)" if event.reused else ""),
                    )

                result = run(on_progress)
        else:
            result = run()
    except runtime._EXPECTED_ERRORS as exc:
        runtime.console.print(f"[red]Error:[/red] {exc}")
        raise typer.Exit(code=1)

    source = result.source
    if source.was_extracted and source.track is not None:
        runtime.console.print(
            f"Extracted embedded track: #{source.track.rel_index} "
            f"({source.track.codec}, {source.track.language or '?'})"
        )
    elif source.subtitle_path != source.origin:
        runtime.console.print(f"Using sidecar: [green]{source.subtitle_path.name}[/green]")
    if source.lang_fallback:
        runtime.console.print(
            f"[yellow]Warning:[/yellow] no '{lang}' subtitle found; using "
            f"'{source.selected_lang}' instead. Pass --strict-lang to refuse this."
        )
    runtime.console.print(
        f"Translatable units: [bold]{result.n_units}[/bold] in {result.n_jobs} block(s)."
    )
    if result.memory_used:
        runtime.console.print("Using series memory (glossary/characters/style guide).")
    if result.context_used:
        runtime.console.print("Using episode.context.json for glossary/rules.")
    if result.context_stale:
        runtime.console.print(
            "[yellow]Warning:[/yellow] episode.context.json was analyzed from a different "
            "version of this subtitle; re-run `analyze` to refresh it."
        )
    runtime.console.print(f"Output: [green]{result.output_path}[/green]")

    if result.untranslated_ids:
        ids = result.untranslated_ids
        preview = ", ".join(ids[:5]) + (" …" if len(ids) > 5 else "")
        runtime.console.print(
            f"[yellow]warning:[/yellow] {len(ids)} line(s) kept the source text "
            f"(provider returned empty): {preview}"
        )

    validation = result.output_validation
    for warning in validation.warnings:
        runtime.console.print(f"[yellow]warning:[/yellow] {warning}")
    if not validation.ok:
        for error in validation.errors:
            runtime.console.print(f"[red]validation:[/red] {error}")
        raise typer.Exit(code=1)
    runtime.console.print("[green]Validation OK.[/green]")

    if fail_on_untranslated and result.untranslated_ids:
        runtime.console.print(
            f"[red]Failing:[/red] --fail-on-untranslated set and "
            f"{len(result.untranslated_ids)} line(s) were not translated."
        )
        raise typer.Exit(code=1)


def batch(
    ctx: typer.Context,
    directory: Path = typer.Argument(..., help="Directory of episodes to translate."),
    glob: list[str] = typer.Option(
        list(DEFAULT_BATCH_GLOBS),
        "--glob",
        help="Filename pattern(s) to translate (repeatable). Default: *.mkv.",
    ),
    recursive: bool = typer.Option(False, "--recursive", "-r", help="Descend into subdirectories."),
    target: str = typer.Option("es-latam", help="Target language/variant."),
    provider: str = typer.Option(
        "claude",
        help="claude | codex | antigravity | opencode | ollama | litellm | "
        "file-handoff | identity.",
    ),
    model: str | None = typer.Option(None, "--model", help="Model id for the provider."),
    reasoning: str | None = typer.Option(None, "--reasoning", help="codex reasoning effort."),
    retries: int = typer.Option(
        2, "--retries", min=0, help="Retries per block after an agent/JSON failure."
    ),
    lang: str = typer.Option("en", help="Preferred source language when picking a track."),
    out_dir: Path | None = typer.Option(
        None, "--out-dir", help="Write every output here (defaults next to each input)."
    ),
    format: str = typer.Option("ass", "--format", help="Output format: ass | srt."),
    project: str | None = typer.Option(None, help="Project/series name (shared by all episodes)."),
    force: bool = typer.Option(
        False, "--force", "-f", help="Re-translate episodes whose output already exists."
    ),
    strict_lang: bool = typer.Option(
        False, "--strict-lang", help="Fail an episode rather than use a different-language sub."
    ),
    fail_on_untranslated: bool = typer.Option(
        False,
        "--fail-on-untranslated",
        help="Exit non-zero if any episode left a line untranslated.",
    ),
    no_resume: bool = typer.Option(
        False, "--no-resume", help="Ignore saved checkpoints and re-translate every block."
    ),
    non_interactive: bool = typer.Option(
        True,
        "--non-interactive/--interactive",
        "--yes/--ask",
        "-y",
        help="Resolve track/language choices without prompting (default for batch).",
    ),
    pre_analyze: bool = typer.Option(
        False,
        "--pre-analyze",
        help="Analyze every episode first to build series memory, then translate. "
        "Produces better translations because the full character/glossary context is "
        "available before any episode is translated.",
    ),
):
    """Translate every matching file in a directory, continuing past per-episode failures."""
    runtime = _runtime()

    on_episode, translate_elapsed = _make_episode_callback(runtime.console)

    try:
        overrides = runtime._project_overrides(ctx, project)
        target = overrides.get("target", target)
        provider = overrides.get("provider", provider)
        model = overrides.get("model", model)
        reasoning = overrides.get("reasoning", reasoning)
        lang = overrides.get("lang", lang)
        format = overrides.get("format", format)

        if pre_analyze:
            runtime.console.print("[bold]Phase 1/2: Analyzing episodes…[/bold]")
            on_analyze, analyze_elapsed = _make_episode_callback(runtime.console, label="Analyze")
            analyze_provider = overrides.get("analyze_provider") or provider
            analyze_model = overrides.get("analyze_model") or model
            analyze_reasoning = overrides.get("analyze_reasoning") or reasoning
            analyze_result = runtime.batch_analyze(
                directory,
                globs=tuple(glob),
                recursive=recursive,
                on_episode=on_analyze,
                target=target,
                provider=analyze_provider,
                model=analyze_model,
                reasoning=analyze_reasoning,
                max_retries=retries,
                lang=lang,
                project=project,
                interactive=False,
                on_conflict="flag",
                skip_if_current=True,
            )
            if analyze_result.items:
                atbl = Table(title=f"{directory} — analysis")
                for col in ("episode", "status", "detail"):
                    atbl.add_column(col)
                amarks = {
                    "analyzed": "[green]analyzed[/green]",
                    "skipped": "[yellow]skipped[/yellow]",
                    "failed": "[red]failed[/red]",
                }
                for item in analyze_result.items:
                    detail = item.error or "" if item.status == "failed" else ""
                    atbl.add_row(item.input_path.name, amarks[item.status], detail)
                runtime.console.print(atbl)
            runtime.console.print(
                f"Analyzed [green]{analyze_result.n_analyzed}[/green], "
                f"skipped [yellow]{analyze_result.n_skipped}[/yellow], "
                f"failed [red]{analyze_result.n_failed}[/red].  "
                f"Total: {_fmt_duration(analyze_elapsed())}"
            )
            runtime.console.print("[bold]Phase 2/2: Translating episodes…[/bold]")

        result = runtime.batch_translate(
            directory,
            globs=tuple(glob),
            recursive=recursive,
            on_episode=on_episode,
            target=target,
            provider=provider,
            model=model,
            reasoning=reasoning,
            max_retries=retries,
            lang=lang,
            out_dir=out_dir,
            fmt=format,
            project=project,
            interactive=not non_interactive,
            force=force,
            strict_lang=strict_lang,
            resume=not no_resume,
        )
    except runtime._EXPECTED_ERRORS as exc:
        runtime.console.print(f"[red]Error:[/red] {exc}")
        raise typer.Exit(code=1)

    if not result.items:
        runtime.console.print("[yellow]No matching files found.[/yellow]")
        return

    table = Table(title=str(directory))
    for column in ("episode", "status", "detail"):
        table.add_column(column)
    marks = {
        "translated": "[green]translated[/green]",
        "skipped": "[yellow]skipped[/yellow]",
        "failed": "[red]failed[/red]",
    }
    untranslated_total = 0
    for item in result.items:
        if item.status == "translated":
            detail = str(item.output_path)
            if item.untranslated_ids:
                untranslated_total += len(item.untranslated_ids)
                detail += f"  ([yellow]{len(item.untranslated_ids)} untranslated[/yellow])"
        elif item.status == "skipped":
            detail = "output exists (use --force)"
        else:
            detail = item.error or "error"
        table.add_row(item.input_path.name, marks[item.status], detail)
    runtime.console.print(table)
    runtime.console.print(
        f"Translated [green]{result.n_translated}[/green], "
        f"skipped [yellow]{result.n_skipped}[/yellow], "
        f"failed [red]{result.n_failed}[/red].  "
        f"Total: {_fmt_duration(translate_elapsed())}"
    )

    if result.n_failed:
        raise typer.Exit(code=1)
    if fail_on_untranslated and untranslated_total:
        runtime.console.print(
            f"[red]Failing:[/red] --fail-on-untranslated set and {untranslated_total} "
            f"line(s) across the batch were not translated."
        )
        raise typer.Exit(code=1)
