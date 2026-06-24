"""Turn series memory (+ the episode context) into translation rules.

Series-level decisions take precedence over per-episode ones on overlap. `translation_rules`
returns the full set; `build_memory_rules` + `rules_for_text` instead keep only the entries
referenced by a given block of text, so prompts stay bounded by episode content rather than
growing with the whole series history.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass

from translate_subs.ai.analysis import EpisodeContext
from translate_subs.memory.models import StyleGuide
from translate_subs.memory.store import ProjectMemory


def style_guide_rules(sg: StyleGuide) -> list[str]:
    rules: list[str] = []
    if sg.locale:
        rules.append(f"Target locale/variant: {sg.locale}.")
    if sg.honorifics == "keep":
        rules.append("Keep honorifics if the source uses them.")
    if sg.names == "keep_original":
        rules.append("Keep proper names unchanged.")
    rules.append(f"Tone: {sg.tone}.")
    if sg.formality_policy == "natural":
        rules.append(
            "Use the register and politeness level natural for the target language, "
            "according to the relationships."
        )
    return rules


def translation_rules(pm: ProjectMemory, ctx: EpisodeContext | None) -> list[str]:
    rules = style_guide_rules(pm.style_guide)

    glossary = dict(ctx.glossary) if ctx else {}
    glossary.update(pm.glossary)  # series wins
    if glossary:
        terms = "; ".join(f"{src} -> {dst}" for src, dst in glossary.items())
        rules.append(f"Use these fixed glossary renderings: {terms}.")

    genders: dict[str, str] = {}
    if ctx:
        for ch in ctx.characters:
            if ch.gender in ("male", "female"):
                genders[ch.name] = ch.gender
    for cm in pm.memory.characters:  # series wins
        if cm.gender in ("male", "female"):
            genders[cm.name] = cm.gender
    if genders:
        listing = "; ".join(f"{name}: {gender}" for name, gender in genders.items())
        rules.append(f"Grammatical gender by character: {listing}.")

    speech_styles: dict[str, str] = {}
    if ctx:
        for ch in ctx.characters:
            if ch.speech_style and ch.speech_style.strip():
                speech_styles[ch.name] = ch.speech_style.strip()
    for cm in pm.memory.characters:  # series wins
        if cm.speech_style and cm.speech_style.strip():
            speech_styles[cm.name] = cm.speech_style.strip()
    if speech_styles:
        listing = "; ".join(f"{name}: {style}" for name, style in speech_styles.items())
        rules.append(f"Speech style by character: {listing}.")

    relationships = []
    for cm in pm.memory.characters:
        for other, rel in cm.relationships.items():
            relationships.append(f"{cm.name}-{other}: {rel}")
    if relationships:
        rules.append("Relationships: " + "; ".join(relationships) + ".")

    if ctx:
        rules.extend(ctx.translation_rules)
        if ctx.episode_summary.strip():
            rules.append(f"Episode context: {ctx.episode_summary.strip()}")

    return rules


@dataclass
class MemoryRules:
    """Series memory split for per-block relevance filtering."""

    base: list[str]  # style guide + episode directives + summary: always sent
    glossary: dict[str, str]
    genders: dict[str, str]
    relationships: list[tuple[str, str, str]]  # (name, other, relationship)
    speech_styles: dict[str, str]  # character -> speech style/register hint


def build_memory_rules(pm: ProjectMemory, ctx: EpisodeContext | None) -> MemoryRules:
    base = style_guide_rules(pm.style_guide)
    if ctx:
        base = base + list(ctx.translation_rules)
        # A short episode synopsis grounds pronoun/gender/tone choices; sent with every block
        # (it is only 2-4 sentences), so the model has the situation even far from the analysis.
        if ctx.episode_summary.strip():
            base = base + [f"Episode context: {ctx.episode_summary.strip()}"]

    glossary = dict(ctx.glossary) if ctx else {}
    glossary.update(pm.glossary)  # series wins
    # identity mappings carry no instruction
    glossary = {src: dst for src, dst in glossary.items() if src != dst}

    genders: dict[str, str] = {}
    if ctx:
        for ch in ctx.characters:
            if ch.gender in ("male", "female"):
                genders[ch.name] = ch.gender
    for cm in pm.memory.characters:  # series wins
        if cm.gender in ("male", "female"):
            genders[cm.name] = cm.gender

    speech_styles: dict[str, str] = {}
    if ctx:
        for ch in ctx.characters:
            if ch.speech_style and ch.speech_style.strip():
                speech_styles[ch.name] = ch.speech_style.strip()
    for cm in pm.memory.characters:  # series wins
        if cm.speech_style and cm.speech_style.strip():
            speech_styles[cm.name] = cm.speech_style.strip()

    # Deduplicate bidirectional pairs (A→B and B→A are stored separately but carry the same
    # semantic information). Keep the most informative description (longest wins).
    pair_rels: dict[frozenset, tuple[str, str, str]] = {}
    for ch in pm.memory.characters:
        for other, rel in ch.relationships.items():
            pair: frozenset = frozenset({ch.name.casefold(), other.casefold()})
            existing = pair_rels.get(pair)
            if existing is None or len(rel) > len(existing[2]):
                pair_rels[pair] = (ch.name, other, rel)
    relationships = list(pair_rels.values())

    return MemoryRules(
        base=base,
        glossary=glossary,
        genders=genders,
        relationships=relationships,
        speech_styles=speech_styles,
    )


# Maximum relationship pairs injected per block. Speaker-involved pairs are prioritised;
# beyond this limit, lower-priority pairs are dropped. This keeps prompts bounded regardless
# of how large the series memory grows across many episodes.
_MAX_RELATIONSHIPS_PER_BLOCK = 20


def rules_for_text(mr: MemoryRules, text: str, speakers: Iterable[str]) -> list[str]:
    """`mr.base` plus only the glossary/gender/relationship entries referenced by `text`."""
    hay = text.casefold()
    present_speakers = {s.casefold() for s in speakers if s}

    def present(name: str) -> bool:
        key = name.casefold()
        return key in present_speakers or key in hay

    rules = list(mr.base)

    glossary = {src: dst for src, dst in mr.glossary.items() if src.casefold() in hay}
    if glossary:
        terms = "; ".join(f"{src} -> {dst}" for src, dst in glossary.items())
        rules.append(f"Use these fixed glossary renderings: {terms}.")

    genders = {name: g for name, g in mr.genders.items() if present(name)}
    if genders:
        listing = "; ".join(f"{name}: {g}" for name, g in genders.items())
        rules.append(f"Grammatical gender by character: {listing}.")

    speech = {name: s for name, s in mr.speech_styles.items() if present(name)}
    if speech:
        listing = "; ".join(f"{name}: {s}" for name, s in speech.items())
        rules.append(f"Speech style by character: {listing}.")

    def _rel_priority(e: tuple[str, str, str]) -> int:
        a_spk = e[0].casefold() in present_speakers
        b_spk = e[1].casefold() in present_speakers
        return 0 if (a_spk or b_spk) else 1

    # Speaker-involved pairs first (priority 0), then text-mentioned (priority 1); cap total.
    relevant = sorted(
        [(a, b, r) for a, b, r in mr.relationships if present(a) or present(b)],
        key=_rel_priority,
    )[:_MAX_RELATIONSHIPS_PER_BLOCK]
    if relevant:
        rules.append("Relationships: " + "; ".join(f"{a}-{b}: {r}" for a, b, r in relevant) + ".")

    return rules
