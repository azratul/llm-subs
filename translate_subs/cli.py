"""Stable Typer application facade and shared command runtime."""

from __future__ import annotations

import subprocess
from importlib.metadata import PackageNotFoundError
from importlib.metadata import version as _pkg_version

import typer
from rich.console import Console

from translate_subs import pipeline as _pipeline
from translate_subs.ai.provider import ProviderError
from translate_subs.commands.project import (
    analyze,
    compact_memory_command,
    config,
    resolve_conflicts_command,
    update_memory_command,
)
from translate_subs.commands.quality import review, tighten
from translate_subs.commands.system import doctor, probe, validate
from translate_subs.commands.translation import batch, translate
from translate_subs.io.media_probe import MediaToolError
from translate_subs.io.source_resolver import SourceError
from translate_subs.memory.merge import Conflict, ConflictPolicy, MergeReport
from translate_subs.settings import load_settings

DEFAULT_BATCH_GLOBS = _pipeline.DEFAULT_BATCH_GLOBS
ConflictChoice = _pipeline.ConflictChoice
PipelineError = _pipeline.PipelineError
analyze_subtitle = _pipeline.analyze_subtitle
batch_analyze = _pipeline.batch_analyze
batch_translate = _pipeline.batch_translate
compact_memory = _pipeline.compact_memory
project_dir = _pipeline.project_dir
resolve_conflicts = _pipeline.resolve_conflicts
review_translation = _pipeline.review_translation
tighten_subtitle = _pipeline.tighten_subtitle
translate_subtitle = _pipeline.translate_subtitle
update_memory = _pipeline.update_memory
validate_subtitle = _pipeline.validate_subtitle

_EXPECTED_ERRORS = (
    PipelineError,
    ProviderError,
    SourceError,
    MediaToolError,
    subprocess.SubprocessError,
    OSError,
    ValueError,
)
_PROJECT_DEFAULTED = (
    "provider",
    "model",
    "target",
    "lang",
    "format",
    "reasoning",
    "analyze_provider",
    "analyze_model",
    "analyze_reasoning",
)

app = typer.Typer(
    add_completion=False,
    help="Contextual subtitle translator (any language to any language); "
    "output is .ass by default, .srt with --format srt.",
)
console = Console()


def _conflict_resolver(conflict: Conflict) -> bool:
    """Interactive prompt for --on-conflict=ask; True overwrites the stored value."""
    return typer.confirm(
        f"Conflict on {conflict.kind} '{conflict.key}': "
        f"stored '{conflict.existing}' vs suggested '{conflict.suggested}'. Overwrite?",
        default=False,
    )


def _interactive_conflict_choice(conflict: dict) -> ConflictChoice:
    """Prompt for one flagged conflict; returns 'keep' | 'use' | 'skip'."""
    console.print(
        f"\n[yellow]Conflict ({conflict.get('kind')})[/yellow] on "
        f"[bold]{conflict.get('key')}[/bold]:"
    )
    console.print(f"  stored:    {conflict.get('existing')}")
    console.print(f"  suggested: {conflict.get('suggested')}")
    choice = typer.prompt("  [k]eep stored / [u]se suggested / [s]kip", default="k").strip().lower()
    mapping: dict[str, ConflictChoice] = {"k": "keep", "u": "use", "s": "skip"}
    return mapping.get(choice, "skip")


def _report_merge(report: MergeReport) -> None:
    if report.applied:
        console.print(f"Memory updates: [green]{len(report.applied)}[/green]")
        for line in report.applied:
            console.print(f"  + {line}")
    for conflict in report.conflicts:
        console.print(
            f"[yellow]conflict ({conflict.kind}):[/yellow] {conflict.key} "
            f"kept '{conflict.existing}' (suggested '{conflict.suggested}')"
        )


def _project_overrides(
    ctx: typer.Context,
    project: str | None,
    names: tuple[str, ...] = _PROJECT_DEFAULTED,
) -> dict[str, str]:
    """Return project defaults only for options not explicitly supplied on the command line."""
    if not project:
        return {}
    settings = load_settings(project_dir(project))
    overrides: dict[str, str] = {}
    for name in names:
        source = ctx.get_parameter_source(name)
        if source is None or source.name == "DEFAULT":
            value = getattr(settings, name)
            if value is not None:
                overrides[name] = value
    return overrides


def _resolve_policy(on_conflict: str | None, non_interactive: bool) -> ConflictPolicy:
    if on_conflict is not None:
        if on_conflict not in ("ask", "keep", "overwrite", "flag"):
            console.print(f"[red]Error:[/red] invalid --on-conflict '{on_conflict}'.")
            raise typer.Exit(code=2)
        return on_conflict  # type: ignore[return-value]
    return "flag" if non_interactive else "ask"


def _version_callback(value: bool) -> None:
    if value:
        try:
            console.print(_pkg_version("llm-subs"))
        except PackageNotFoundError:
            console.print("0.0.0+source")
        raise typer.Exit()


@app.callback()
def _main(
    version: bool = typer.Option(
        False,
        "--version",
        callback=_version_callback,
        is_eager=True,
        help="Show the installed version and exit.",
    ),
) -> None:
    """Contextual subtitle translator."""


# Registration order is user-visible in `--help`; keep it aligned with the documented workflow.
app.command()(probe)
app.command()(translate)
app.command()(batch)
app.command()(config)
app.command()(analyze)
app.command(name="update-memory")(update_memory_command)
app.command(name="compact-memory")(compact_memory_command)
app.command(name="resolve-conflicts")(resolve_conflicts_command)
app.command()(review)
app.command()(tighten)
app.command()(doctor)
app.command()(validate)


if __name__ == "__main__":
    app()
