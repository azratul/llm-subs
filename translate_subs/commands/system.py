"""Environment, probing and validation command callbacks."""

from __future__ import annotations

from pathlib import Path

import typer
from rich.table import Table

from translate_subs.diagnostics import run_diagnostics
from translate_subs.io.media_probe import probe_subtitle_tracks


def _runtime():
    from translate_subs import cli

    return cli


def probe(media: Path = typer.Argument(..., help="Video file to inspect.")):
    """List the embedded subtitle tracks of a container."""
    runtime = _runtime()
    try:
        tracks = probe_subtitle_tracks(media)
    except runtime._EXPECTED_ERRORS as exc:
        runtime.console.print(f"[red]Error:[/red] {exc}")
        raise typer.Exit(code=1)
    if not tracks:
        runtime.console.print("[yellow]No subtitle tracks.[/yellow]")
        raise typer.Exit()

    table = Table(title=str(media))
    for col in ("#", "stream", "codec", "lang", "title", "default", "forced", "text"):
        table.add_column(col)
    for track in tracks:
        table.add_row(
            str(track.rel_index),
            str(track.stream_index),
            track.codec,
            track.language or "-",
            track.title or "-",
            "yes" if track.default else "",
            "yes" if track.forced else "",
            "yes" if track.is_text else "no",
        )
    runtime.console.print(table)


def doctor(
    provider: str | None = typer.Option(
        None,
        help="Also check this provider's backend (claude|codex|...|ollama|litellm).",
    ),
):
    """Check the environment: media tools, writable data/cache dirs, optional provider."""
    runtime = _runtime()
    checks = run_diagnostics(provider)
    table = Table(title="llm-subs doctor")
    for col in ("check", "status", "detail"):
        table.add_column(col)
    marks = {"ok": "[green]ok[/green]", "warn": "[yellow]warn[/yellow]", "fail": "[red]fail[/red]"}
    for check in checks:
        table.add_row(check.name, marks[check.status], check.detail)
    runtime.console.print(table)
    if any(check.status == "fail" for check in checks):
        raise typer.Exit(code=1)


def validate(
    subtitle: Path = typer.Argument(..., help="Subtitle file to validate."),
):
    """Structurally validate a subtitle file (parseable, timings, no leftover markup)."""
    runtime = _runtime()
    try:
        result = runtime.validate_subtitle(subtitle)
    except runtime._EXPECTED_ERRORS as exc:
        runtime.console.print(f"[red]Error:[/red] {exc}")
        raise typer.Exit(code=1)

    for warning in result.warnings:
        runtime.console.print(f"[yellow]warning:[/yellow] {warning}")
    if not result.ok:
        for error in result.errors:
            runtime.console.print(f"[red]invalid:[/red] {error}")
        raise typer.Exit(code=1)
    runtime.console.print("[green]Valid.[/green]")
