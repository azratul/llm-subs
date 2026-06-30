"""Compact per-series memory: drop dead glossary entries and redundant characters.

Memory only ever grows as episodes are analyzed. This prunes entries that carry no
instruction so prompts and files stay small: glossary mappings where source == target,
case-insensitive duplicate terms/characters, and characters with no usable information.

With a runner, a second LLM pass detects character aliases (e.g. "Alice" and
"Alice Chambers") that slipped past exact-name matching during merge.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass, field

from translate_subs.memory.models import CharacterMemory
from translate_subs.memory.store import ProjectMemory

Runner = Callable[[str], str]


@dataclass
class AliasMatch:
    canonical: str
    alias: str
    reason: str


@dataclass
class CompactReport:
    removed_identity_terms: int = 0
    removed_duplicate_terms: int = 0
    merged_characters: int = 0
    removed_empty_characters: int = 0
    merged_aliases: list[AliasMatch] = field(default_factory=list)


def compact_project_memory(pm: ProjectMemory) -> CompactReport:
    """Prune `pm` in place; return what was removed."""
    report = CompactReport()

    cleaned: dict[str, str] = {}
    seen: set[str] = set()
    for src, dst in pm.glossary.items():
        if src == dst:
            report.removed_identity_terms += 1
            continue
        key = src.casefold()
        if key in seen:
            report.removed_duplicate_terms += 1
            continue
        seen.add(key)
        cleaned[src] = dst
    pm.glossary = cleaned

    merged: dict[str, CharacterMemory] = {}
    order: list[str] = []
    for ch in pm.memory.characters:
        key = ch.name.casefold()
        base = merged.get(key)
        if base is None:
            merged[key] = ch
            order.append(key)
            continue
        report.merged_characters += 1
        if base.gender == "unknown" and ch.gender in ("male", "female"):
            base.gender = ch.gender
        if not base.speech_style and ch.speech_style:
            base.speech_style = ch.speech_style
        for other, rel in ch.relationships.items():
            base.relationships.setdefault(other, rel)

    kept = []
    for key in order:
        ch = merged[key]
        if ch.gender == "unknown" and not ch.speech_style and not ch.relationships:
            report.removed_empty_characters += 1
            continue
        kept.append(ch)
    pm.memory.characters = kept

    return report


def _character_profile(ch: CharacterMemory) -> str:
    parts = [f'name: "{ch.name}"', f"gender: {ch.gender}"]
    if ch.speech_style:
        parts.append(f'speech_style: "{ch.speech_style}"')
    if ch.relationships:
        rel_str = ", ".join(f'"{k}"' for k in ch.relationships)
        parts.append(f"relationships with: [{rel_str}]")
    return "{" + ", ".join(parts) + "}"


def detect_character_aliases(runner: Runner, characters: list[CharacterMemory]) -> list[AliasMatch]:
    """Ask the LLM to find character aliases in the current memory.

    Returns pairs where `alias` should be merged into `canonical`. Only high-confidence
    matches are expected; the caller confirms each one interactively before applying.
    """
    if len(characters) < 2:
        return []

    profiles = "\n".join(f"- {_character_profile(ch)}" for ch in characters)
    prompt = (
        "You are reviewing a character memory file for a TV series or film. "
        "The list below may contain duplicate entries for the same character under "
        "different name forms (e.g. given name only vs. family + given name, or a "
        "nickname vs. the full name).\n\n"
        "CHARACTER LIST:\n"
        f"{profiles}\n\n"
        "Identify pairs that are almost certainly the same character. "
        "Use gender, shared relationships, and speech style as evidence — name overlap "
        "alone is not enough. Only report pairs you are highly confident about.\n\n"
        "For each pair: choose the most complete name as `canonical` and the shorter/alternate "
        "form as `alias`.\n\n"
        'Reply with ONLY a JSON object: {"duplicates": [{"canonical": "...", "alias": "...", '
        '"reason": "one sentence"}]}. '
        'If there are no duplicates reply with {"duplicates": []}. No prose, no code fences.'
    )

    from translate_subs.ai.claude_cli import extract_json
    from translate_subs.ai.provider import ProviderError

    names = {ch.name.casefold() for ch in characters}
    try:
        raw = runner(prompt)
        data = json.loads(extract_json(raw))
        pairs = data.get("duplicates", [])
        if not isinstance(pairs, list):
            raise TypeError(f"expected a list of duplicates, got {type(pairs).__name__!r}")
        results: list[AliasMatch] = []
        for pair in pairs:
            canonical = str(pair.get("canonical", "")).strip()
            alias = str(pair.get("alias", "")).strip()
            reason = str(pair.get("reason", "")).strip()
            if (
                canonical
                and alias
                and canonical != alias
                and canonical.casefold() in names
                and alias.casefold() in names
            ):
                results.append(AliasMatch(canonical=canonical, alias=alias, reason=reason))
    except (json.JSONDecodeError, KeyError, TypeError, AttributeError) as exc:
        raise ProviderError(
            f"Alias detection reply was not valid JSON: {exc}", retryable=True
        ) from exc
    return results


def merge_alias(pm: ProjectMemory, canonical_name: str, alias_name: str) -> bool:
    """Merge `alias_name` into `canonical_name` in `pm` in place.

    Combines relationships and fills in missing gender/speech_style from the alias.
    Also rewrites all other characters' relationship keys that reference the alias.
    Returns True if both names were found and the merge happened.
    """
    canonical = pm.memory.find(canonical_name)
    alias = pm.memory.find(alias_name)
    if canonical is None or alias is None or canonical is alias:
        return False

    if canonical.gender == "unknown" and alias.gender in ("male", "female"):
        canonical.gender = alias.gender
    if not canonical.speech_style and alias.speech_style:
        canonical.speech_style = alias.speech_style
    for other, rel in alias.relationships.items():
        if other.casefold() == canonical_name.casefold():
            continue
        existing = canonical.relationships.get(other)
        if not existing or len(rel) > len(existing):
            canonical.relationships[other] = rel

    pm.memory.characters = [
        ch for ch in pm.memory.characters if ch.name.casefold() != alias_name.casefold()
    ]

    # Rewrite references to the alias in all other characters' relationship maps.
    # Keys may differ in casing from alias_name (e.g. stored as "ALICE", called with "alice").
    for ch in pm.memory.characters:
        for key in list(ch.relationships.keys()):
            if key.casefold() == alias_name.casefold():
                rel = ch.relationships.pop(key)
                if canonical.name not in ch.relationships or len(rel) > len(
                    ch.relationships[canonical.name]
                ):
                    ch.relationships[canonical.name] = rel
                break

    return True
