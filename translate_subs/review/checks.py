"""Deterministic review checks (no LLM)."""

from __future__ import annotations

from translate_subs.readability.metrics import display_width
from translate_subs.review.models import Finding, ReviewLine

DEFAULT_MAX_CHARS = 42  # per visual line; matches readability limits (Phase 5).


def check_target_not_empty(lines: list[ReviewLine]) -> list[Finding]:
    findings = []
    for line in lines:
        if line.source.strip() and not line.target.strip():
            findings.append(
                Finding(
                    id=line.id,
                    kind="empty_line",
                    message="Source line has no translation.",
                    current=line.target,
                )
            )
    return findings


def check_glossary(lines: list[ReviewLine], glossary: dict[str, str]) -> list[Finding]:
    """Flag lines whose source uses a glossary term but the target lacks its rendering."""
    findings = []
    for line in lines:
        src = line.source.casefold()
        tgt = line.target.casefold()
        for term, rendering in glossary.items():
            if term.casefold() in src and rendering.casefold() not in tgt:
                findings.append(
                    Finding(
                        id=line.id,
                        kind="glossary",
                        message=f"Glossary term '{term}' should render as '{rendering}'.",
                        current=line.target,
                    )
                )
    return findings


def check_name_consistency(lines: list[ReviewLine], names: list[str]) -> list[Finding]:
    """Flag known proper names present in the source but missing from the target."""
    findings = []
    for line in lines:
        for name in names:
            if name and name in line.source and name not in line.target:
                findings.append(
                    Finding(
                        id=line.id,
                        kind="proper_name",
                        message=f"Proper name '{name}' should be kept unchanged.",
                        current=line.target,
                    )
                )
    return findings


def check_line_length(lines: list[ReviewLine], max_chars: int = DEFAULT_MAX_CHARS) -> list[Finding]:
    findings = []
    for line in lines:
        longest = max((display_width(seg) for seg in line.target.split("\n")), default=0)
        if longest > max_chars:
            findings.append(
                Finding(
                    id=line.id,
                    kind="length",
                    message=f"Line exceeds {max_chars} columns ({longest}).",
                    current=line.target,
                )
            )
    return findings


def run_deterministic_checks(
    lines: list[ReviewLine],
    *,
    glossary: dict[str, str],
    names: list[str],
    max_chars: int = DEFAULT_MAX_CHARS,
) -> list[Finding]:
    return [
        *check_target_not_empty(lines),
        *check_glossary(lines, glossary),
        *check_name_consistency(lines, names),
        *check_line_length(lines, max_chars),
    ]
