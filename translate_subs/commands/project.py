"""Project settings, analysis and memory command callbacks."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import typer
from pydantic import ValidationError
from rich.table import Table

from translate_subs.memory.compact import AliasMatch
from translate_subs.settings import ProjectSettings, load_settings, save_settings

_CONFLICT_HELP = "On contradicting suggestions: ask | keep | overwrite | flag."
_AI_PROVIDER_HELP = "claude | codex | antigravity | opencode | ollama | litellm"
# Options that fall through to project settings.json when not given on the command line.
_AUX_DEFAULTED = (
    "provider",
    "model",
    "target",
    "lang",
    "reasoning",
    "analyze_provider",
    "analyze_model",
    "analyze_reasoning",
)


def _runtime() -> Any:
    # Shared command runtime; imported lazily to break the cli <-> commands cycle. Typed Any because
    # it is a dynamically-accessed module facade, not a nominal type.
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
    analyze_provider: str | None = typer.Option(
        None,
        "--analyze-provider",
        help="Provider for batch --pre-analyze (defaults to --provider if unset).",
    ),
    analyze_model: str | None = typer.Option(
        None,
        "--analyze-model",
        help="Model id for batch --pre-analyze (defaults to --model if unset).",
    ),
    analyze_reasoning: str | None = typer.Option(
        None,
        "--analyze-reasoning",
        help="Reasoning effort for batch --pre-analyze (defaults to --reasoning if unset).",
    ),
    unset: list[str] = typer.Option(
        [], "--unset", help="Field name(s) to clear back to the built-in default (repeatable)."
    ),
) -> None:
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
        "analyze_provider": analyze_provider,
        "analyze_model": analyze_model,
        "analyze_reasoning": analyze_reasoning,
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
    for key in (
        "provider",
        "model",
        "target",
        "lang",
        "format",
        "reasoning",
        "analyze_provider",
        "analyze_model",
        "analyze_reasoning",
    ):
        table.add_row(key, str(getattr(settings, key) or "—"))
    runtime.console.print(table)
    runtime.console.print(f"[green]{project_path / 'settings.json'}[/green]")


def analyze(
    ctx: typer.Context,
    input: Path = typer.Argument(..., help="Subtitle (.ass/.srt/...) or video file."),
    target: str = typer.Option(
        "es-latam", help="Target language/variant, e.g. es-latam, en, fr-FR, ja."
    ),
    track: int | None = typer.Option(None, help="Embedded track index (when several exist)."),
    lang: str = typer.Option("en", help="Preferred source language when picking a track."),
    encoding: str | None = typer.Option(
        None,
        "--encoding",
        help="Source text encoding (e.g. cp1252, shift-jis, utf-16). Auto-detected when omitted.",
    ),
    project: str | None = typer.Option(None, help="Project/series name."),
    provider: str = typer.Option("claude", help=_AI_PROVIDER_HELP),
    model: str | None = typer.Option(None, "--model", help="Model id for the chosen CLI provider."),
    reasoning: str | None = typer.Option(None, "--reasoning", help="Codex reasoning effort."),
    retries: int = typer.Option(2, "--retries", min=0, help="Retries after an agent/JSON failure."),
    on_conflict: str | None = typer.Option(None, "--on-conflict", help=_CONFLICT_HELP),
    non_interactive: bool = typer.Option(
        False, "--non-interactive", "--yes", "-y", help="Do not prompt; resolve by heuristic/flags."
    ),
) -> None:
    """Analyze the episode (writes episode.context.json) and update series memory."""
    runtime = _runtime()
    policy = runtime._resolve_policy(on_conflict, non_interactive)
    overrides = runtime._project_overrides(ctx, project, _AUX_DEFAULTED)
    target = overrides.get("target", target)
    provider = overrides.get("analyze_provider") or overrides.get("provider", provider)
    model = overrides.get("analyze_model") or overrides.get("model", model)
    reasoning = overrides.get("analyze_reasoning") or overrides.get("reasoning", reasoning)
    lang = overrides.get("lang", lang)
    runtime._warn_weak_backend(provider)
    try:
        with runtime.console.status("Analyzing…", spinner="dots"):
            result = runtime.analyze_subtitle(
                input,
                target=target,
                track_index=track,
                lang=lang,
                encoding=encoding,
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
) -> None:
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
    provider: str | None = typer.Option(
        None,
        help=f"Enable LLM alias detection with this provider ({_AI_PROVIDER_HELP}). "
        "Without this flag only deterministic pruning runs.",
    ),
    model: str | None = typer.Option(None, "--model", help="Model id for the provider."),
    reasoning: str | None = typer.Option(None, "--reasoning", help="codex reasoning effort."),
    non_interactive: bool = typer.Option(
        False,
        "--non-interactive",
        "--yes",
        "-y",
        help="Auto-apply all detected aliases without prompting.",
    ),
) -> None:
    """Prune redundant series memory; with --provider also detects character aliases via LLM."""
    runtime = _runtime()
    if provider:  # the LLM runs only when a provider is given
        runtime._warn_weak_backend(provider)

    def alias_confirm(match: AliasMatch) -> str:
        if non_interactive:
            return "apply"
        runtime.console.print(
            f"\n[yellow]Alias detected:[/yellow] "
            f"[bold]{match.alias}[/bold] → [bold]{match.canonical}[/bold]"
        )
        runtime.console.print(f"  Reason: {match.reason}")
        choice = typer.prompt("  [a]pply merge / [s]kip", default="a").strip().lower()
        return "apply" if choice.startswith("a") else "skip"

    try:
        result = runtime.compact_memory(
            project,
            target,
            provider=provider,
            model=model,
            reasoning=reasoning,
            alias_confirm=alias_confirm if provider else None,
        )
    except runtime._EXPECTED_ERRORS as exc:
        runtime.console.print(f"[red]Error:[/red] {exc}")
        raise typer.Exit(code=1)

    report = result.report
    runtime.console.print(
        f"Glossary: removed [green]{report.removed_identity_terms}[/green] identity "
        f"and [green]{report.removed_duplicate_terms}[/green] duplicate term(s)."
    )
    runtime.console.print(
        f"Characters: merged [green]{report.merged_characters}[/green] exact duplicates, "
        f"removed [green]{report.removed_empty_characters}[/green] empty."
    )
    if report.merged_aliases:
        runtime.console.print(f"Aliases merged: [green]{len(report.merged_aliases)}[/green]")
        for match in report.merged_aliases:
            runtime.console.print(f"  {match.alias} → {match.canonical}")
    runtime.console.print(f"Memory: [green]{result.project_dir}[/green]")


def project_status_command(
    project: str = typer.Argument(..., help="Project/series name."),
    target: str = typer.Option("es-latam", help="Target language/variant whose state to show."),
    json_out: bool = typer.Option(False, "--json", help="Emit the status as JSON."),
) -> None:
    """Show a project's stored state for a target: memory, analyzed episodes, checkpoints, outputs.

    Reads only what is on disk (no LLM call, no source access). Output staleness is not recomputed
    here — run `batch` to re-check each output against its source.
    """
    runtime = _runtime()
    try:
        result = runtime.project_status(project, target)
    except runtime._EXPECTED_ERRORS as exc:
        if json_out:
            import json

            typer.echo(json.dumps({"error": str(exc)}, ensure_ascii=False, indent=2))
        else:
            runtime.console.print(f"[red]Error:[/red] {exc}")
        raise typer.Exit(code=1)

    if json_out:
        import json

        typer.echo(
            json.dumps(
                {
                    "project": project,
                    "project_dir": str(result.project_dir),
                    "target": result.target,
                    "glossary_terms": result.glossary_terms,
                    "characters": result.characters,
                    "conflicts": result.conflicts,
                    "episodes": [
                        {
                            "name": ep.name,
                            "analyzed": ep.analyzed,
                            "has_checkpoint_file": ep.has_checkpoint_file,
                            "outputs": ep.outputs,
                        }
                        for ep in result.episodes
                    ],
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        return

    analyzed = sum(1 for ep in result.episodes if ep.analyzed)
    with_ckpt = sum(1 for ep in result.episodes if ep.has_checkpoint_file)
    runtime.console.print(
        f"[bold]{project}[/bold] · target [bold]{result.target}[/bold]\n"
        f"Glossary terms: [green]{result.glossary_terms}[/green]  "
        f"Characters: [green]{result.characters}[/green]  "
        f"Conflicts: [yellow]{result.conflicts}[/yellow]\n"
        f"Episodes: [green]{len(result.episodes)}[/green] "
        f"(analyzed [green]{analyzed}[/green], with checkpoint file [cyan]{with_ckpt}[/cyan])"
    )
    if result.episodes:
        table = Table(title=f"{project} — {result.target}")
        for column in ("episode", "analyzed", "checkpoint file", "outputs"):
            table.add_column(column)
        for ep in result.episodes:
            table.add_row(
                ep.name,
                "[green]yes[/green]" if ep.analyzed else "[dim]no[/dim]",
                "[cyan]yes[/cyan]" if ep.has_checkpoint_file else "[dim]no[/dim]",
                "\n".join(ep.outputs) or "[dim]—[/dim]",
            )
        runtime.console.print(table)
    runtime.console.print(f"Memory: [green]{result.project_dir}[/green]")


def resolve_conflicts_command(
    project: str = typer.Argument(..., help="Project/series name."),
    target: str = typer.Option(
        "es-latam", help="Target language/variant whose conflicts to resolve."
    ),
) -> None:
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
