"""Merge an episode's findings into per-series memory, conflict-aware.

A new suggestion that contradicts a stored discrete decision (glossary rendering,
confirmed gender) is never silently overwritten. The `ConflictPolicy` decides what
happens: `keep` (silent), `flag` (keep + record), `overwrite` (apply), or `ask`
(delegate to a resolver callback).

Safe, non-contradicting updates always apply: new terms, new characters, filling an
`unknown` gender or an empty speech style. Relationships are free-text descriptions, not
discrete decisions, so they never raise conflicts; the most informative one is kept.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Literal

from pydantic import BaseModel

from translate_subs.ai.analysis import EpisodeCharacter, EpisodeContext
from translate_subs.memory.models import CharacterMemory, SeriesMemory, normalize_gender

ConflictPolicy = Literal["ask", "keep", "overwrite", "flag"]


class Conflict(BaseModel):
    kind: str  # "glossary" | "gender"
    key: str
    existing: str
    suggested: str


class MergeReport(BaseModel):
    applied: list[str] = []
    conflicts: list[Conflict] = []


ConflictResolver = Callable[[Conflict], bool]  # True -> overwrite


def _norm(s: str) -> str:
    """Canonical form for comparing glossary renderings.

    Collapses whitespace, case, and trailing punctuation so trivial wording differences
    (e.g. a stray full stop) are not mistaken for a contradicting decision.
    """
    return " ".join(s.split()).casefold().rstrip(".!?¡¿ ")


def _decide(
    conflict: Conflict,
    policy: ConflictPolicy,
    resolver: ConflictResolver | None,
    report: MergeReport,
) -> bool:
    """Return True if the suggestion should overwrite the stored decision."""
    if policy == "overwrite":
        return True
    if policy == "keep":
        return False
    if policy == "ask" and resolver is not None:
        if resolver(conflict):
            return True
        report.conflicts.append(conflict)
        return False
    # "flag" (and "ask" without a resolver): keep existing, record the conflict.
    report.conflicts.append(conflict)
    return False


def _merge_glossary_terms(
    glossary: dict[str, str],
    ctx: EpisodeContext,
    policy: ConflictPolicy,
    resolver: ConflictResolver | None,
    report: MergeReport,
) -> None:
    for term, rendering in ctx.glossary.items():
        current = glossary.get(term)
        if current is None:
            glossary[term] = rendering
            report.applied.append(f"glossary: {term} -> {rendering}")
        elif _norm(current) != _norm(rendering):
            c = Conflict(kind="glossary", key=term, existing=current, suggested=rendering)
            if _decide(c, policy, resolver, report):
                glossary[term] = rendering
                report.applied.append(f"glossary (overwrite): {term} -> {rendering}")


def _merge_gender(
    existing: CharacterMemory,
    ch: EpisodeCharacter,
    policy: ConflictPolicy,
    resolver: ConflictResolver | None,
    report: MergeReport,
) -> None:
    new_gender = normalize_gender(ch.gender)
    if new_gender not in ("male", "female"):
        return
    if existing.gender == "unknown":
        existing.gender = new_gender
        report.applied.append(f"gender: {ch.name} -> {new_gender}")
    elif existing.gender != new_gender:
        c = Conflict(kind="gender", key=ch.name, existing=existing.gender, suggested=new_gender)
        if _decide(c, policy, resolver, report):
            existing.gender = new_gender
            report.applied.append(f"gender (overwrite): {ch.name} -> {ch.gender}")


def _merge_character(
    memory: SeriesMemory,
    ch: EpisodeCharacter,
    policy: ConflictPolicy,
    resolver: ConflictResolver | None,
    report: MergeReport,
) -> None:
    existing = memory.find(ch.name)
    if existing is None:
        memory.characters.append(
            CharacterMemory(
                name=ch.name,
                gender=normalize_gender(ch.gender),
                speech_style=ch.speech_style,
                relationships=dict(ch.relationships),
            )
        )
        report.applied.append(f"character: {ch.name}")
        return

    _merge_gender(existing, ch, policy, resolver, report)

    if ch.speech_style and not existing.speech_style:
        existing.speech_style = ch.speech_style
        report.applied.append(f"speech_style: {ch.name}")

    for other, rel in ch.relationships.items():
        current_rel = existing.relationships.get(other)
        # Relationships are free-text descriptions, not discrete decisions, so an
        # exact-string mismatch is just a paraphrase, never a real contradiction.
        # Flagging every wording difference floods conflicts.json (one entry per
        # episode per pair); instead keep the most informative (longest) description.
        if rel and len(rel) > len(current_rel or ""):
            existing.relationships[other] = rel
            report.applied.append(f"relationship: {ch.name} -> {other}")


def merge_episode_context(
    memory: SeriesMemory,
    glossary: dict[str, str],
    ctx: EpisodeContext,
    *,
    policy: ConflictPolicy = "flag",
    resolver: ConflictResolver | None = None,
) -> MergeReport:
    """Merge `ctx` into `memory`/`glossary` in place; return what happened."""
    report = MergeReport()
    _merge_glossary_terms(glossary, ctx, policy, resolver, report)
    for ch in ctx.characters:
        _merge_character(memory, ch, policy, resolver, report)
    return report
