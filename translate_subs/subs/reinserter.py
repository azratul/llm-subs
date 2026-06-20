"""Reinsert translations into the SSAFile and prepare it for export.

The visible text is replaced via `event.plaintext`, which drops inline ASS override
tags; the whole-line leading block (e.g. `{\\an8\\pos(..)}`) captured at extraction is
then restored ahead of it. Style-level attributes (the style name, hence its
alignment/colour/font) are kept too, so an .ass export still places e.g. a top-aligned
translator note above the dialogue. An .srt export has no positioning, so the restored
tags are stripped by the writer and `flatten_overlaps` merges simultaneous cues instead.
"""

from __future__ import annotations

import re

import pysubs2

from translate_subs.domain.models import TranslatableUnit
from translate_subs.subs.extractor import leading_tags

# An ASS override block injected by the model: braces wrapping a backslash command
# (e.g. {\b1}, {\an8}, {\pos(..)}). A literal brace in dialogue like "{cool}" has no
# backslash and is left untouched. The legitimate whole-line lead block is restored
# separately, so stripping these from the model text never drops real positioning.
_INJECTED_TAG_RE = re.compile(r"\{[^{}]*\\[^{}]*\}")


def sanitize_model_text(text: str) -> str:
    """Drop ASS override blocks a model may have returned inside the visible text."""
    return _INJECTED_TAG_RE.sub("", text)


def replace_visible_text(
    event: pysubs2.SSAEvent,
    text: str,
    *,
    lead_tags: str | None = None,
) -> None:
    """Replace an event's visible text without dropping whole-line ASS override tags."""
    preserved = leading_tags(event) if lead_tags is None else lead_tags
    event.plaintext = sanitize_model_text(text)
    if preserved:
        event.text = preserved + event.text


def apply_translations(
    subs: pysubs2.SSAFile,
    units: list[TranslatableUnit],
    translations: dict[str, str],
) -> None:
    by_id = {unit.id: unit for unit in units}
    for unit_id, text in translations.items():
        unit = by_id.get(unit_id)
        if unit is None:
            continue
        event = subs.events[unit.event_index]
        # The plaintext setter escapes line breaks back to '\N'; restore the leading
        # whole-line tags captured from the source after replacing the visible text.
        replace_visible_text(event, text, lead_tags=unit.lead_tags)


def prune_to_units(subs: pysubs2.SSAFile, units: list[TranslatableUnit]) -> None:
    """Keep only translated events so empty cues or drawings never reach the output."""
    keep = {unit.event_index for unit in units}
    subs.events = [event for index, event in enumerate(subs.events) if index in keep]


def _alignment_rank(subs: pysubs2.SSAFile, event: pysubs2.SSAEvent) -> int:
    """0 = top, 1 = middle, 2 = bottom, from the event's style (ASS \\an numbering)."""
    style = subs.styles.get(event.style)
    align = int(getattr(style, "alignment", 2)) if style is not None else 2
    if align in (7, 8, 9):
        return 0
    if align in (4, 5, 6):
        return 1
    return 2


def flatten_overlaps(subs: pysubs2.SSAFile) -> None:
    """Rewrite events so none overlap in time (for .srt, which has no positioning).

    The timeline is split at every cue boundary; each resulting interval becomes a
    single cue stacking the text of all cues active during it (top-aligned first, so a
    translator note sits above the line it annotates). Adjacent intervals with identical
    text are re-joined to avoid needless splits, so files without overlaps are unchanged.
    """
    events = [e for e in subs.events if e.plaintext.strip()]
    timed = [e for e in events if e.end > e.start]
    degenerate = [e for e in events if e.end <= e.start]

    boundaries = sorted({e.start for e in timed} | {e.end for e in timed})
    segments: list[tuple[int, int, str]] = []
    for start, end in zip(boundaries, boundaries[1:], strict=False):
        active = [e for e in timed if e.start <= start and e.end >= end]
        if not active:
            continue
        active.sort(key=lambda e: (_alignment_rank(subs, e), e.start))
        text = "\n".join(e.plaintext for e in active)
        if segments and segments[-1][1] == start and segments[-1][2] == text:
            prev_start, _, prev_text = segments[-1]
            segments[-1] = (prev_start, end, prev_text)
        else:
            segments.append((start, end, text))

    new_events: list[pysubs2.SSAEvent] = []
    for start, end, text in segments:
        event = pysubs2.SSAEvent(start=start, end=end)
        event.plaintext = text
        new_events.append(event)
    subs.events = new_events + degenerate
