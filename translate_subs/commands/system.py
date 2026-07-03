"""Environment, probing and validation command callbacks."""

from __future__ import annotations

import json
import shutil
from pathlib import Path
from typing import Any

import typer
from rich.table import Table

from translate_subs import config
from translate_subs.diagnostics import run_diagnostics
from translate_subs.io.media_probe import probe_subtitle_tracks


def _emit_json(payload: object) -> None:
    """Print a machine-readable JSON document to stdout (no rich styling)."""
    typer.echo(json.dumps(payload, ensure_ascii=False, indent=2))


def _runtime() -> Any:
    # The `cli` module is the shared command runtime (console, error tuple, workflow facades).
    # Imported lazily to break the cli <-> commands import cycle; typed as Any because it is a
    # dynamically-accessed module facade, not a nominal type.
    from translate_subs import cli

    return cli


def _dir_size(path: Path) -> tuple[int, int]:
    """Return (file_count, total_bytes) under `path`, ignoring unreadable entries."""
    files = 0
    total = 0
    for entry in path.rglob("*"):
        if entry.is_file():
            files += 1
            try:
                total += entry.stat().st_size
            except OSError:
                pass
    return files, total


def probe(media: Path = typer.Argument(..., help="Video file to inspect.")) -> None:
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
    model: str | None = typer.Option(
        None,
        "--model",
        help="With --provider ollama, also verify this model is installed on the server.",
    ),
    json_out: bool = typer.Option(False, "--json", help="Emit results as JSON instead of a table."),
) -> None:
    """Check the environment: media tools, writable data/cache dirs, optional provider."""
    runtime = _runtime()
    checks = run_diagnostics(provider, model)
    failed = any(check.status == "fail" for check in checks)
    if json_out:
        _emit_json(
            {
                "ok": not failed,
                "checks": [
                    {"name": c.name, "status": c.status, "detail": c.detail} for c in checks
                ],
            }
        )
        if failed:
            raise typer.Exit(code=1)
        return
    table = Table(title="llm-subs doctor")
    for col in ("check", "status", "detail"):
        table.add_column(col)
    marks = {"ok": "[green]ok[/green]", "warn": "[yellow]warn[/yellow]", "fail": "[red]fail[/red]"}
    for check in checks:
        table.add_row(check.name, marks[check.status], check.detail)
    runtime.console.print(table)
    if failed:
        raise typer.Exit(code=1)


def purge_cache(
    yes: bool = typer.Option(False, "--yes", "-y", help="Delete without the confirmation prompt."),
) -> None:
    """Delete cached subtitle tracks extracted from video containers.

    The cache (`$XDG_CACHE_HOME/llm-subs/work`) only holds subtitle tracks demuxed from media so a
    rerun skips re-extraction; it is always safe to clear. Per-series memory, episode context and
    reports live in the separate data root and are **not** touched by this command.
    """
    runtime = _runtime()
    work_dir = config.WORK_DIR
    if not work_dir.exists():
        runtime.console.print(f"Cache is already empty: [dim]{work_dir}[/dim]")
        return

    files, total = _dir_size(work_dir)
    if files == 0:
        runtime.console.print(f"Cache is already empty: [dim]{work_dir}[/dim]")
        return

    mib = total / (1024 * 1024)
    if not yes:
        confirmed = typer.confirm(
            f"Delete {files} cached file(s) ({mib:.1f} MiB) under {work_dir}?"
        )
        if not confirmed:
            runtime.console.print("Aborted.")
            raise typer.Exit(code=1)

    for entry in sorted(work_dir.iterdir(), reverse=True):
        if entry.is_dir():
            shutil.rmtree(entry, ignore_errors=True)
        else:
            entry.unlink(missing_ok=True)
    runtime.console.print(f"Freed [green]{mib:.1f} MiB[/green] from {files} cached file(s).")


def validate(
    subtitle: Path = typer.Argument(..., help="Subtitle file to validate."),
    json_out: bool = typer.Option(False, "--json", help="Emit the result as JSON."),
) -> None:
    """Structurally validate a subtitle file (parseable, timings, no leftover markup)."""
    runtime = _runtime()
    try:
        result = runtime.validate_subtitle(subtitle)
    except runtime._EXPECTED_ERRORS as exc:
        if json_out:
            _emit_json({"ok": False, "warnings": [], "errors": [str(exc)]})
        else:
            runtime.console.print(f"[red]Error:[/red] {exc}")
        raise typer.Exit(code=1)

    if json_out:
        _emit_json(
            {"ok": result.ok, "warnings": list(result.warnings), "errors": list(result.errors)}
        )
        if not result.ok:
            raise typer.Exit(code=1)
        return

    for warning in result.warnings:
        runtime.console.print(f"[yellow]warning:[/yellow] {warning}")
    if not result.ok:
        for error in result.errors:
            runtime.console.print(f"[red]invalid:[/red] {error}")
        raise typer.Exit(code=1)
    runtime.console.print("[green]Valid.[/green]")
