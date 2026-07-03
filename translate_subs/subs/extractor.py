"""Extract translatable units from an SSAFile.

Separates visible text from ASS tags and assigns a stable per-line ID.
"""

from __future__ import annotations

import re

import pysubs2

from translate_subs.domain.models import TranslatableUnit

# {\p1} or higher marks a vector drawing, not text.
_DRAWING_RE = re.compile(r"\\p[1-9]")

# The run of override blocks at the very start of an event (e.g. {\an8}{\pos(..)}).
# These apply to the whole line and don't depend on its text, so they can be restored
# after translation. Inline tags further inside the text are tied to the original
# wording and are dropped. Karaoke (\k) is per-syllable, so such leads are not restored.
_LEADING_TAGS_RE = re.compile(r"^(?:\{[^}]*\})+")
_KARAOKE_RE = re.compile(r"\\[kK]")


def is_translatable(event: pysubs2.SSAEvent) -> bool:
    if event.is_comment:
        return False
    if _DRAWING_RE.search(event.text or ""):
        return False
    return bool(event.plaintext.strip())


def leading_tags(event: pysubs2.SSAEvent) -> str:
    match = _LEADING_TAGS_RE.match(event.text or "")
    if match is None:
        return ""
    block = match.group(0)
    return "" if _KARAOKE_RE.search(block) else block


def _style_signature(style: pysubs2.SSAStyle) -> str:
    """Rendering-relevant fields of an ASS style definition.

    A change to any of these (font, size, colours, alignment, margins…) alters the rendered
    `.ass` even when no line's text or timing changes, so they belong in the staleness digest.
    """
    fields = (
        style.fontname,
        style.fontsize,
        style.primarycolor,
        style.secondarycolor,
        style.tertiarycolor,
        style.outlinecolor,
        style.backcolor,
        style.bold,
        style.italic,
        style.underline,
        style.strikeout,
        style.scalex,
        style.scaley,
        style.spacing,
        style.angle,
        style.borderstyle,
        style.outline,
        style.shadow,
        style.alignment,
        style.marginl,
        style.marginr,
        style.marginv,
        style.alphalevel,
        style.encoding,
        style.drawing,
    )
    return "\t".join(str(field) for field in fields)


def ass_fidelity_lines(subs: pysubs2.SSAFile) -> list[str]:
    """Serialize the ASS source content that reaches the output beyond the translatable units.

    Covers what `.ass` preserves but the units don't capture: `[Script Info]` headers (PlayResX/
    PlayResY rescale every coordinate, so they change the render without touching a line), the
    Aegisub project garbage, embedded attachments (fonts/graphics), the `[V4+ Styles]` definitions
    and every event's layout metadata (layer, actor/name, margins, effect), plus the verbatim text
    of non-translatable events (drawings, comments). Folded into the output-staleness digest so a
    re-style, a resolution change, or a drawing edit flags the `.ass` output stale even though no
    translatable line changed. `.srt` is flat and prunes these, so it does not use this.
    """
    lines = [f"I\t{key}\t{value}" for key, value in sorted(subs.info.items())]
    lines.extend(f"A\t{key}\t{value}" for key, value in sorted(subs.aegisub_project.items()))
    # Attachments ([Fonts]/[Graphics]) are dicts of name -> encoded content lines; distinct
    # prefixes so a font and a graphic sharing a filename can't collide into one digest line.
    lines.extend(
        f"F\t{name}\t{'|'.join(content)}" for name, content in sorted(subs.fonts_opaque.items())
    )
    lines.extend(
        f"G\t{name}\t{'|'.join(content)}" for name, content in sorted(subs.graphics_opaque.items())
    )
    lines.extend(f"S\t{name}\t{_style_signature(style)}" for name, style in subs.styles.items())
    for index, event in enumerate(subs.events):
        # Translatable text is already in the units; only preserved events contribute their text.
        text = "" if is_translatable(event) else (event.text or "")
        lines.append(
            f"E\t{index}\t{event.layer}\t{event.start}\t{event.end}\t{event.style}\t"
            f"{event.name}\t{event.marginl}\t{event.marginr}\t{event.marginv}\t"
            f"{event.effect}\t{event.type}\t{text}"
        )
    return lines


def extract_units(subs: pysubs2.SSAFile) -> list[TranslatableUnit]:
    units: list[TranslatableUnit] = []
    n = 1
    for index, event in enumerate(subs.events):
        if not is_translatable(event):
            continue
        units.append(
            TranslatableUnit(
                id=f"{n:04d}",
                event_index=index,
                start=event.start,
                end=event.end,
                style=event.style,
                speaker=event.name or None,
                text=event.plaintext,
                lead_tags=leading_tags(event),
            )
        )
        n += 1
    return units
