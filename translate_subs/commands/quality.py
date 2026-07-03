"""Review and readability command callbacks."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any

import typer
from rich.console import Console
from rich.status import Status
from rich.table import Table

from translate_subs.readability.metrics import ReadabilityLimits

_ConfirmGate = Callable[[list[tuple[str, str, str]]], bool]


def _print_diff_table(console: Console, fixes: list[tuple[str, str, str]], title: str) -> None:
    table = Table(title=title, show_header=True, header_style="bold", expand=False)
    table.add_column("ID", style="dim", no_wrap=True, width=6)
    table.add_column("Before", style="red")
    table.add_column("After", style="green")
    for fix_id, old, new in fixes:
        table.add_row(fix_id, old, new)
    console.print(table)


def _make_apply_confirm(
    console: Console, status: Status, non_interactive: bool, title: str, noun: str
) -> _ConfirmGate | None:
    """A confirm gate for --apply: show the diff and ask before overwriting whole lines.

    Each change replaces a whole line, so a silent write is risky; the default (interactive) run
    previews the diff and asks. `--non-interactive`/`--yes` opts out of the prompt and applies.
    The live spinner is stopped first, since it runs while the workflow (and this gate) executes
    and would otherwise redraw over the prompt.
    """
    if non_interactive:
        return None

    def _confirm(changes: list[tuple[str, str, str]]) -> bool:
        status.stop()
        _print_diff_table(console, changes, title)
        return typer.confirm(f"Apply {len(changes)} {noun} to the subtitle file?", default=False)

    return _confirm


_AI_PROVIDER_HELP = "claude | codex | antigravity | opencode | ollama | litellm"
# Options that fall through to project settings.json when not given on the command line.
_AUX_DEFAULTED = ("provider", "model", "target", "lang", "reasoning")
_TIGHTEN_DEFAULTED = ("provider", "model", "target", "reasoning")


def _runtime() -> Any:
    # Shared command runtime; imported lazily to break the cli <-> commands cycle. Typed Any because
    # it is a dynamically-accessed module facade, not a nominal type.
    from translate_subs import cli

    return cli


def review(
    ctx: typer.Context,
    source: Path = typer.Argument(..., help="Original source (subtitle/video)."),
    translated: Path = typer.Argument(..., help="Translated .ass/.srt to review."),
    target: str = typer.Option(
        "es-latam", help="Target language/variant, e.g. es-latam, en, fr-FR, ja."
    ),
    track: int | None = typer.Option(None, help="Embedded track index (when several exist)."),
    lang: str = typer.Option("en", help="Preferred source language when picking a track."),
    encoding: str | None = typer.Option(
        None,
        "--encoding",
        help="Text encoding of the source file (the translated file is auto-detected as UTF-8).",
    ),
    project: str | None = typer.Option(None, help="Project/series name."),
    max_chars: int = typer.Option(42, help="Max characters per visual line."),
    provider: str = typer.Option("claude", help=_AI_PROVIDER_HELP),
    model: str | None = typer.Option(None, "--model", help="Model id for the chosen CLI provider."),
    reasoning: str | None = typer.Option(None, "--reasoning", help="Codex reasoning effort."),
    retries: int = typer.Option(2, "--retries", min=0, help="Retries after an agent/JSON failure."),
    no_llm: bool = typer.Option(False, "--no-llm", help="Deterministic checks only."),
    apply: bool = typer.Option(
        False,
        "--apply",
        help="Apply only the safe auto-fixes to the translated subtitle.",
    ),
    non_interactive: bool = typer.Option(
        False, "--non-interactive", "--yes", "-y", help="Do not prompt; resolve by heuristic/flags."
    ),
) -> None:
    """Review a translation and write episode.review.md (optionally apply safe fixes)."""
    runtime = _runtime()
    overrides = runtime._project_overrides(ctx, project, _AUX_DEFAULTED)
    target = overrides.get("target", target)
    provider = overrides.get("provider", provider)
    model = overrides.get("model", model)
    reasoning = overrides.get("reasoning", reasoning)
    lang = overrides.get("lang", lang)
    if not no_llm:
        runtime._warn_weak_backend(provider)
    status = runtime.console.status("Reviewing…", spinner="dots")
    status.start()
    try:
        result = runtime.review_translation(
            source,
            translated,
            target=target,
            track_index=track,
            lang=lang,
            encoding=encoding,
            project=project,
            interactive=not non_interactive,
            max_chars=max_chars,
            use_llm=not no_llm,
            apply=apply,
            confirm=_make_apply_confirm(
                runtime.console, status, non_interactive, "Suggested fixes", "safe fix(es)"
            ),
            provider=provider,
            model=model,
            reasoning=reasoning,
            max_retries=retries,
        )
    except runtime._EXPECTED_ERRORS as exc:
        runtime.console.print(f"[red]Error:[/red] {exc}")
        raise typer.Exit(code=1)
    finally:
        status.stop()

    report = result.report
    runtime.console.print(
        f"Reviewed [bold]{result.n_lines}[/bold] lines: "
        f"{len(report.warnings())} warning(s), {len(report.fixes())} suggested fix(es)."
    )
    if result.context_stale:
        runtime.console.print(
            "[yellow]Warning:[/yellow] episode.context.json was analyzed from a different "
            "version of this source; re-run `analyze` to refresh it."
        )
    if apply and not result.mapping_aligned:
        runtime.console.print(
            "[yellow]Skipped --apply:[/yellow] the translated file does not map 1:1 to the "
            "source (merged .srt). Review and apply fixes on the .ass output instead."
        )
    elif apply:
        if result.n_applied:
            runtime.console.print(f"Applied [green]{result.n_applied}[/green] safe fix(es).")
            # In an interactive run the diff was already shown by the confirm prompt.
            if non_interactive and result.applied_fixes:
                _print_diff_table(runtime.console, result.applied_fixes, "Applied fixes")
        else:
            runtime.console.print("[dim]No safe fixes applied.[/dim]")
    elif result.planned_fixes:
        runtime.console.print(
            f"[dim]{len(result.planned_fixes)} safe fix(es) available — use --apply to write.[/dim]"
        )
        _print_diff_table(runtime.console, result.planned_fixes, "Suggested fixes")
    runtime.console.print(f"Report: [green]{result.report_path}[/green]")


def tighten(
    ctx: typer.Context,
    translated: Path = typer.Argument(..., help="Translated .ass/.srt to check for readability."),
    target: str = typer.Option(
        "es-latam", help="Target language/variant whose memory directory holds the report."
    ),
    project: str | None = typer.Option(None, help="Project/series name."),
    source: Path | None = typer.Option(
        None,
        "--source",
        help="Original input the translation came from; keys the report to the same episode "
        "directory as translate/review when the translated file lives in --out-dir.",
    ),
    encoding: str | None = typer.Option(
        None,
        "--encoding",
        help="Text encoding of the translated file (auto-detected when omitted).",
    ),
    max_chars_per_line: int = typer.Option(42, help="Max characters per visual line."),
    max_lines: int = typer.Option(2, help="Max lines per subtitle."),
    max_cps: float = typer.Option(18.0, help="Max characters per second."),
    provider: str = typer.Option("claude", help=_AI_PROVIDER_HELP),
    model: str | None = typer.Option(None, "--model", help="Model id for the chosen CLI provider."),
    reasoning: str | None = typer.Option(None, "--reasoning", help="Codex reasoning effort."),
    retries: int = typer.Option(2, "--retries", min=0, help="Retries after an agent/JSON failure."),
    no_llm: bool = typer.Option(False, "--no-llm", help="Only measure/flag, no compaction."),
    apply: bool = typer.Option(
        False,
        "--apply",
        help="Write the compacted lines back to the subtitle file.",
    ),
    non_interactive: bool = typer.Option(
        False, "--non-interactive", "--yes", "-y", help="Apply without the confirmation prompt."
    ),
) -> None:
    """Flag subtitles that break readability limits and compact them via the LLM."""
    runtime = _runtime()
    overrides = runtime._project_overrides(ctx, project, _TIGHTEN_DEFAULTED)
    target = overrides.get("target", target)
    provider = overrides.get("provider", provider)
    model = overrides.get("model", model)
    reasoning = overrides.get("reasoning", reasoning)
    limits = ReadabilityLimits(
        max_chars_per_line=max_chars_per_line,
        max_lines=max_lines,
        max_chars_per_second=max_cps,
    )
    if not no_llm:
        runtime._warn_weak_backend(provider)
    status = runtime.console.status("Checking readability…", spinner="dots")
    status.start()
    try:
        result = runtime.tighten_subtitle(
            translated,
            target=target,
            project=project,
            source=source,
            limits=limits,
            encoding=encoding,
            use_llm=not no_llm,
            apply=apply,
            confirm=_make_apply_confirm(
                runtime.console, status, non_interactive, "Suggested compactions", "compaction(s)"
            ),
            provider=provider,
            model=model,
            reasoning=reasoning,
            max_retries=retries,
        )
    except runtime._EXPECTED_ERRORS as exc:
        runtime.console.print(f"[red]Error:[/red] {exc}")
        raise typer.Exit(code=1)
    finally:
        status.stop()

    runtime.console.print(
        f"Checked [bold]{result.n_subs}[/bold] subtitles: "
        f"{result.n_flagged} over limit, {result.n_compacted} compacted."
    )
    if apply:
        if result.n_applied:
            runtime.console.print(f"Applied [green]{result.n_applied}[/green] compaction(s).")
            # In an interactive run the diff was already shown by the confirm prompt.
            if non_interactive and result.applied_compactions:
                _print_diff_table(
                    runtime.console, result.applied_compactions, "Applied compactions"
                )
        else:
            runtime.console.print("[dim]No compactions applied.[/dim]")
    if result.n_residual:
        runtime.console.print(
            f"[yellow]{result.n_residual} still over limit after compaction.[/yellow]"
        )
    runtime.console.print(f"Report: [green]{result.report_path}[/green]")
