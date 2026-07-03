"""Deterministic source/target structure checks used before linguistic review."""

from __future__ import annotations

from collections import Counter
from typing import Any

import pysubs2

from translate_subs.domain.models import TranslatableUnit
from translate_subs.review.models import Finding, ReviewLine
from translate_subs.subs.extractor import is_translatable

ALIGN_TOLERANCE_MS = 10


def _style_signature(
    subs: pysubs2.SSAFile, event: pysubs2.SSAEvent
) -> tuple[str, dict[str, Any] | None]:
    style = subs.styles.get(event.style)
    return event.style, style.as_dict() if style is not None else None


def pair_lines(
    units: list[TranslatableUnit],
    target_subs: pysubs2.SSAFile,
    *,
    source_subs: pysubs2.SSAFile | None = None,
    compare_styles: bool = False,
    sequential: bool = False,
) -> tuple[list[ReviewLine], list[Finding]]:
    """Pair source/target events and report structural mismatches.

    ASS targets (sequential=False, default): lookup by unit.event_index so that
    non-translatable events preserved verbatim (drawings, comments) don't shift the
    pairing and cause source unit N to be compared against the wrong translated event.

    SRT targets (sequential=True): pair by position (units[i] ↔ events[i]) because
    prune_to_units and flatten_overlaps have already removed non-translatable events and
    may have re-segmented the timeline, so event_index no longer addresses a valid slot.
    """
    events = target_subs.events
    lines: list[ReviewLine] = []
    findings: list[Finding] = []

    if sequential:
        for pos, unit in enumerate(units):
            if pos >= len(events):
                findings.append(
                    Finding(
                        id=unit.id,
                        kind="missing_id",
                        message="No translated event at this position.",
                        current="",
                    )
                )
                continue
            event = events[pos]
            lines.append(
                ReviewLine(
                    id=unit.id,
                    event_index=pos,
                    speaker=unit.speaker,
                    source=unit.text,
                    target=event.plaintext,
                )
            )
            if (
                abs(unit.start - event.start) > ALIGN_TOLERANCE_MS
                or abs(unit.end - event.end) > ALIGN_TOLERANCE_MS
            ):
                findings.append(
                    Finding(
                        id=unit.id,
                        kind="timing_mismatch",
                        message=(
                            f"Timing differs: source {unit.start}-{unit.end} ms, "
                            f"target {event.start}-{event.end} ms."
                        ),
                        current=event.plaintext,
                    )
                )
        for i in range(len(units), len(events)):
            event = events[i]
            if event.plaintext.strip():
                findings.append(
                    Finding(
                        id=f"T{i + 1:04d}",
                        kind="extra_event",
                        message="Translated file has more text events than the source.",
                        current=event.plaintext,
                    )
                )
    else:
        unit_indices: set[int] = set()
        for unit in units:
            idx = unit.event_index
            if idx >= len(events):
                findings.append(
                    Finding(
                        id=unit.id,
                        kind="missing_id",
                        message="No translated event at this position.",
                        current="",
                    )
                )
                continue
            event = events[idx]
            unit_indices.add(idx)
            lines.append(
                ReviewLine(
                    id=unit.id,
                    event_index=idx,
                    speaker=unit.speaker,
                    source=unit.text,
                    target=event.plaintext,
                )
            )
            if (
                abs(unit.start - event.start) > ALIGN_TOLERANCE_MS
                or abs(unit.end - event.end) > ALIGN_TOLERANCE_MS
            ):
                findings.append(
                    Finding(
                        id=unit.id,
                        kind="timing_mismatch",
                        message=(
                            f"Timing differs: source {unit.start}-{unit.end} ms, "
                            f"target {event.start}-{event.end} ms."
                        ),
                        current=event.plaintext,
                    )
                )
            if (
                compare_styles
                and source_subs is not None
                and _style_signature(source_subs, source_subs.events[unit.event_index])
                != _style_signature(target_subs, event)
            ):
                findings.append(
                    Finding(
                        id=unit.id,
                        kind="style_mismatch",
                        message=f"Style mismatch: source={unit.style!r}, target={event.style!r}.",
                        current=event.plaintext,
                    )
                )

        # Only translatable Dialogue events with no matching source unit are unexpected;
        # drawings, comments and other non-translatables are preserved verbatim in ASS.
        for i, event in enumerate(events):
            if i not in unit_indices and is_translatable(event):
                findings.append(
                    Finding(
                        id=f"T{i + 1:04d}",
                        kind="extra_event",
                        message="Translated file has a text event with no matching source line.",
                        current=event.plaintext,
                    )
                )

    duplicate_ids = sorted(
        unit_id for unit_id, count in Counter(unit.id for unit in units).items() if count > 1
    )
    if duplicate_ids:
        findings.append(
            Finding(
                scope="global",
                kind="duplicate_id",
                message=f"Source contains duplicate stable IDs: {duplicate_ids[:5]}.",
            )
        )

    source_is_chronological = all(
        units[i].start >= units[i - 1].start for i in range(1, len(units))
    )
    out_of_order = (
        [i for i in range(1, len(events)) if events[i].start < events[i - 1].start]
        if source_is_chronological
        else []
    )
    if out_of_order:
        findings.append(
            Finding(
                scope="global",
                kind="out_of_order",
                message=(
                    "Translated events are not in chronological order "
                    f"near positions {[i + 1 for i in out_of_order[:5]]}."
                ),
            )
        )
    return lines, findings
