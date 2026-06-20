"""Project settings, analysis and memory command callbacks."""

from __future__ import annotations

from pathlib import Path

import typer
from pydantic import ValidationError
from rich.table import Table

from translate_subs.settings import ProjectSettings, load_settings, save_settings

_CONFLICT_HELP = "On contradicting suggestions: ask | keep | overwrite | flag."
_AI_PROVIDER_HELP = "claude | codex | gemini | opencode"


def _runtime():
    from translate_subs import cli

    return cli


def config(
    project: str = typer.Argument(..., help="Project/series name."),
    provider: str | None = typer.Option(None, help="Default provider for this project."),
    model: str | None = typer.Option(None, "--model", help="Default model id."),
    target: str | None = typer.Option(None, help="Default target language/variant."),
    lang: str | None = typer.Option(None, help="Default source language."),
    format: str | None = typer.Option(None, "--format", help="Default output format: ass | srt."),
    reasoning: str | None = typer.Option(
        None, "--reasoning", help="Default codex reasoning effort."
    ),
    unset: list[str] = typer.Option(
        [], "--unset", help="Field name(s) to clear back to the built-in default (repeatable)."
    ),
):
    """Show or set per-project default options (settings.json).

    With no flags it prints the current settings; flags set defaults that `translate` and `batch`
    use when you don't pass the matching flag explicitly.
    """
    runtime = _runtime()
    updates = {
        "provider": provider,
        "model": model,
        "target": target,
        "lang": lang,
        "format": format,
        "reasoning": reasoning,
    }
    for key in unset:
        if key not in ProjectSettings.model_fields:
            runtime.console.print(f"[red]Error:[/red] unknown field '{key}'.")
            raise typer.Exit(code=2)
    try:
        project_path = runtime.project_dir(project)
        merged = load_settings(project_path).model_dump()
        merged.update({key: value for key, value in updates.items() if value is not None})
        merged.update(dict.fromkeys(unset))
        changed = any(value is not None for value in updates.values()) or bool(unset)
        settings = ProjectSettings(**merged)
        if changed:
            save_settings(project_path, settings)
    except ValidationError as exc:
        runtime.console.print(f"[red]Error:[/red] {exc.errors()[0]['msg']}")
        raise typer.Exit(code=2)
    except runtime._EXPECTED_ERRORS as exc:
        runtime.console.print(f"[red]Error:[/red] {exc}")
        raise typer.Exit(code=1)

    table = Table(title=f"{project} defaults")
    table.add_column("key")
    table.add_column("value")
    for key in ("provider", "model", "target", "lang", "format", "reasoning"):
        table.add_row(key, str(getattr(settings, key) or "—"))
    runtime.console.print(table)
    runtime.console.print(f"[green]{project_path / 'settings.json'}[/green]")


def analyze(
    input: Path = typer.Argument(..., help="Subtitle (.ass/.srt/...) or video file."),
    target: str = typer.Option(
        "es-latam", help="Target language/variant, e.g. es-latam, en, fr-FR, ja."
    ),
    track: int | None = typer.Option(None, help="Embedded track index (when several exist)."),
    lang: str = typer.Option("en", help="Preferred source language when picking a track."),
    project: str | None = typer.Option(None, help="Project/series name."),
    provider: str = typer.Option("claude", help=_AI_PROVIDER_HELP),
    model: str | None = typer.Option(None, "--model", help="Model id for the chosen CLI provider."),
    reasoning: str | None = typer.Option(None, "--reasoning", help="Codex reasoning effort."),
    retries: int = typer.Option(2, "--retries", min=0, help="Retries after an agent/JSON failure."),
    on_conflict: str | None = typer.Option(None, "--on-conflict", help=_CONFLICT_HELP),
    non_interactive: bool = typer.Option(
        False, "--non-interactive", "--yes", "-y", help="Do not prompt; resolve by heuristic/flags."
    ),
):
    """Analyze the episode (writes episode.context.json) and update series memory."""
    runtime = _runtime()
    policy = runtime._resolve_policy(on_conflict, non_interactive)
    try:
        result = runtime.analyze_subtitle(
            input,
            target=target,
            track_index=track,
            lang=lang,
            project=project,
            interactive=not non_interactive,
            on_conflict=policy,
            conflict_resolver=None if non_interactive else runtime._conflict_resolver,
            provider=provider,
            model=model,
            reasoning=reasoning,
            max_retries=retries,
        )
    except runtime._EXPECTED_ERRORS as exc:
        runtime.console.print(f"[red]Error:[/red] {exc}")
        raise typer.Exit(code=1)

    context = result.context
    runtime.console.print(
        f"Analyzed [bold]{result.n_units}[/bold] lines: "
        f"{len(context.characters)} character(s), {len(context.glossary)} glossary term(s)."
    )
    if result.truncated_lines:
        runtime.console.print(
            f"[yellow]Note:[/yellow] only the first {result.n_units - result.truncated_lines} "
            f"lines were analyzed; {result.truncated_lines} trailing line(s) were truncated."
        )
    runtime.console.print(f"Context: [green]{result.context_path}[/green]")
    runtime._report_merge(result.merge)


def update_memory_command(
    input: Path = typer.Argument(..., help="Subtitle/video whose episode.context.json exists."),
    target: str = typer.Option("es-latam", help="Target language/variant of the memory to update."),
    track: int | None = typer.Option(None, help="Embedded track index (when several exist)."),
    lang: str = typer.Option("en", help="Preferred source language when picking a track."),
    project: str | None = typer.Option(None, help="Project/series name."),
    on_conflict: str | None = typer.Option(None, "--on-conflict", help=_CONFLICT_HELP),
    non_interactive: bool = typer.Option(
        False, "--non-interactive", "--yes", "-y", help="Do not prompt; resolve by heuristic/flags."
    ),
):
    """Re-merge an existing episode.context.json into series memory (no LLM call)."""
    runtime = _runtime()
    policy = runtime._resolve_policy(on_conflict, non_interactive)
    try:
        result = runtime.update_memory(
            input,
            target=target,
            track_index=track,
            lang=lang,
            project=project,
            interactive=not non_interactive,
            on_conflict=policy,
            conflict_resolver=None if non_interactive else runtime._conflict_resolver,
        )
    except runtime._EXPECTED_ERRORS as exc:
        runtime.console.print(f"[red]Error:[/red] {exc}")
        raise typer.Exit(code=1)

    runtime.console.print(f"Memory: [green]{result.project_dir}[/green]")
    runtime._report_merge(result.merge)


def compact_memory_command(
    project: str = typer.Argument(..., help="Project/series name."),
    target: str = typer.Option("es-latam", help="Target language/variant of the memory to prune."),
):
    """Prune redundant series memory (identity glossary terms, duplicate/empty characters)."""
    runtime = _runtime()
    try:
        result = runtime.compact_memory(project, target)
    except runtime._EXPECTED_ERRORS as exc:
        runtime.console.print(f"[red]Error:[/red] {exc}")
        raise typer.Exit(code=1)

    report = result.report
    runtime.console.print(
        f"Glossary: removed [green]{report.removed_identity_terms}[/green] identity "
        f"and [green]{report.removed_duplicate_terms}[/green] duplicate term(s)."
    )
    runtime.console.print(
        f"Characters: merged [green]{report.merged_characters}[/green], "
        f"removed [green]{report.removed_empty_characters}[/green] empty."
    )
    runtime.console.print(f"Memory: [green]{result.project_dir}[/green]")


def resolve_conflicts_command(
    project: str = typer.Argument(..., help="Project/series name."),
    target: str = typer.Option(
        "es-latam", help="Target language/variant whose conflicts to resolve."
    ),
):
    """Walk flagged memory conflicts and resolve each (keep stored / use suggested / skip)."""
    runtime = _runtime()
    try:
        result = runtime.resolve_conflicts(project, runtime._interactive_conflict_choice, target)
    except runtime._EXPECTED_ERRORS as exc:
        runtime.console.print(f"[red]Error:[/red] {exc}")
        raise typer.Exit(code=1)

    if result.resolved == 0 and result.remaining == 0:
        runtime.console.print("No conflicts to resolve.")
        return
    runtime.console.print(
        f"Resolved [green]{result.resolved}[/green]; "
        f"[yellow]{result.remaining}[/yellow] left for later."
    )
    runtime.console.print(f"Memory: [green]{result.project_dir}[/green]")
