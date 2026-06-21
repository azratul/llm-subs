"""Validation of the translation mapping and of the output file."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

import pysubs2

from translate_subs.domain.models import TranslatableUnit

# pysubs2 represents the basic italic/bold that survive .srt as {\i1}/{\b0} override
# blocks in event.text. Those are allowed; anything else in a block is leftover markup.
_OVERRIDE_BLOCK_RE = re.compile(r"\{([^}]*)\}")
_BASIC_TAGS_RE = re.compile(r"^(\\[ib][01])+$")

# Individual override commands inside a leading block, e.g. {\an8\pos(1,2)} -> \an8, \pos(1,2).
# Checked by substring so the fidelity check tolerates pysubs2 re-bracketing/merging on round-trip.
_TAG_TOKEN_RE = re.compile(r"\\[^\\{}]+")


def _lead_tokens(lead_tags: str) -> list[str]:
    return _TAG_TOKEN_RE.findall(lead_tags)


def _has_nonbasic_markup(text: str) -> bool:
    return any(not _BASIC_TAGS_RE.match(block) for block in _OVERRIDE_BLOCK_RE.findall(text))


@dataclass
class ValidationResult:
    ok: bool
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


def validate_translations(
    units: list[TranslatableUnit], translations: dict[str, str]
) -> ValidationResult:
    errors: list[str] = []
    unit_ids = {u.id for u in units}
    trans_ids = set(translations)

    missing = sorted(unit_ids - trans_ids)
    if missing:
        errors.append(f"{len(missing)} IDs without translation (e.g. {missing[:5]})")

    unknown = sorted(trans_ids - unit_ids)
    if unknown:
        errors.append(f"{len(unknown)} unknown IDs in translation (e.g. {unknown[:5]})")

    empty = sorted(uid for uid, text in translations.items() if not text.strip())
    if empty:
        errors.append(f"{len(empty)} empty translations (e.g. {empty[:5]})")

    return ValidationResult(ok=not errors, errors=errors)


def validate_file(path: str | Path) -> ValidationResult:
    """Standalone structural check of a subtitle file (no source needed)."""
    errors: list[str] = []
    warnings: list[str] = []

    try:
        subs = pysubs2.load(str(path))
    except Exception as exc:  # noqa: BLE001 - report any parse failure
        return ValidationResult(ok=False, errors=[f"not parseable: {exc}"])

    events = list(subs.events)
    if not events:
        return ValidationResult(ok=False, errors=["no events found"])

    # Override blocks are leftover markup in a flat format like .srt, but legitimate
    # positioning/colour in .ass/.ssa, so only flag them for the flat formats.
    check_markup = Path(path).suffix.lower() not in (".ass", ".ssa")

    empty = 0
    bad_timing = 0  # start after end, or negative start: genuinely broken
    zero_duration = 0  # start == end: often inherited from the source, only a warning
    with_tags = 0
    for e in events:
        if not e.plaintext.strip():
            empty += 1
        if e.start < 0 or e.end < e.start:
            bad_timing += 1
        elif e.end == e.start:
            zero_duration += 1
        if check_markup and _has_nonbasic_markup(e.text):
            with_tags += 1

    if bad_timing:
        errors.append(f"{bad_timing} events with invalid timing (start>end or negative)")
    if with_tags:
        errors.append(f"{with_tags} events still contain non-basic {{...}} markup")
    if empty:
        warnings.append(f"{empty} empty events")
    if zero_duration:
        warnings.append(f"{zero_duration} zero-duration events (likely from the source)")

    return ValidationResult(ok=not errors, errors=errors, warnings=warnings)


def validate_output(
    srt_path: str | Path,
    units: list[TranslatableUnit],
    *,
    check_fidelity: bool = False,
) -> ValidationResult:
    """Reopen the resulting file and check minimal structural integrity.

    `check_fidelity` (for the `.ass` translate path, where the output events come from the same
    units) additionally verifies that each event kept its source style and its whole-line leading
    override block (`{\\an8\\pos(..)}`), so a silently dropped position/colour/alignment is caught.
    It is off by default because `review` validates a *translated* file against *source* units,
    where style/lead-tag differences between the two files are legitimate.
    """
    errors: list[str] = []
    warnings: list[str] = []

    try:
        out = pysubs2.load(str(srt_path))
    except Exception as exc:  # noqa: BLE001 - report any parse failure
        return ValidationResult(ok=False, errors=[f"output is not parseable: {exc}"])

    events = list(out.events)
    if len(events) != len(units):
        errors.append(
            f"output has {len(events)} events, expected {len(units)} "
            "(comments/drawings may have been dropped)."
        )

    # .ass/.ssa store time in centiseconds, so a millisecond-precision source (e.g. an
    # .srt sidecar) is rounded to the nearest 10ms on write. That rounding is inherent to
    # the format and far below one video frame, so allow it instead of flagging a mismatch.
    tolerance = 10 if Path(srt_path).suffix.lower() in (".ass", ".ssa") else 0
    mismatched = [
        unit.id
        for unit, event in zip(units, events, strict=False)
        if abs(unit.start - event.start) > tolerance or abs(unit.end - event.end) > tolerance
    ]
    if mismatched:
        errors.append(f"{len(mismatched)} timestamp mismatches by position (e.g. {mismatched[:5]})")

    empty = sum(1 for e in events if not e.plaintext.strip())
    if empty:
        warnings.append(f"{empty} events ended up empty in the output")

    if (
        check_fidelity
        and len(events) == len(units)
        and Path(srt_path).suffix.lower()
        in (
            ".ass",
            ".ssa",
        )
    ):
        style_lost = [
            unit.id for unit, event in zip(units, events, strict=False) if event.style != unit.style
        ]
        if style_lost:
            errors.append(f"{len(style_lost)} events lost their style (e.g. {style_lost[:5]})")

        tags_lost = [
            unit.id
            for unit, event in zip(units, events, strict=False)
            if (tokens := _lead_tokens(unit.lead_tags))
            and any(token not in (event.text or "") for token in tokens)
        ]
        if tags_lost:
            errors.append(
                f"{len(tags_lost)} events lost their leading override tags (e.g. {tags_lost[:5]})"
            )

    return ValidationResult(ok=not errors, errors=errors, warnings=warnings)
