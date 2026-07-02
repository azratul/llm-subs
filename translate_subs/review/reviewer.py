"""LLM review pass and the safe-fix policy.

The model judges what deterministic checks cannot (gender, pronouns, tú/usted,
literalness, naturalness, loss of meaning) and proposes fixes. Whether a fix may be
auto-applied is decided here, never by trusting the model blindly.
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable
from difflib import SequenceMatcher
from functools import partial

from translate_subs.ai.claude_cli import extract_json
from translate_subs.ai.provider import ProviderError, retry_provider_call
from translate_subs.review.models import Finding, ReviewLine

Runner = Callable[[str], str]

# Kinds eligible for automatic correction. Everything else is left for a human.
SAFE_KINDS = {"glossary", "proper_name", "honorific", "empty_line", "missing_id", "gender"}

# Term-substitution fixes: they should change one term, not rewrite the line, so they are held to
# the single-span guard below; the rest (gender/empty_line/missing_id) may touch the whole line.
TERM_FIX_KINDS = {"glossary", "proper_name", "honorific"}

_WORD_OR_SPACE = re.compile(r"\S+|\s+")
# A glossary term / proper name is short; a single changed run longer than this is a reword (the
# model expanded the term or rewrote a phrase), not a term swap, so it is rejected. This also bounds
# the space-less CJK case, where the whole line is one token: a small change applies, a large
# rewrite is rejected. Distinguishing a small CJK term-swap from a small CJK reword is not possible
# without word segmentation — an accepted residual weakness, mitigated by the `apply_safe_policy`
# guard that the suggestion must still carry the known glossary rendering / character name.
_MAX_SPAN_CHARS = 16


def is_single_span_edit(current: str, suggested: str) -> bool:
    """True when `suggested` is a term-swap of `current`: exactly one short, contiguous change.

    A glossary/name/honorific correction should replace one term, not reword the line. Applying the
    whole suggested line would trust the model not to have rewritten the surrounding text; this
    guard rejects a suggestion (leaving it for a human) unless exactly one contiguous run of
    tokens differs and it is short (a term, not a phrase). Because the caller only applies fixes
    whose `current` still matches the on-disk line, replacing the whole line then equals replacing
    just that run.
    """
    a = _WORD_OR_SPACE.findall(current)
    b = _WORD_OR_SPACE.findall(suggested)
    changed = [
        op for op in SequenceMatcher(a=a, b=b, autojunk=False).get_opcodes() if op[0] != "equal"
    ]
    if len(changed) != 1:
        return False
    _, i1, i2, j1, j2 = changed[0]
    return max(len("".join(a[i1:i2])), len("".join(b[j1:j2]))) <= _MAX_SPAN_CHARS


def build_review_prompt(
    lines: list[ReviewLine],
    *,
    glossary: dict[str, str],
    genders: dict[str, str],
    target: str,
    source_lang: str = "source",
) -> str:
    src_label = source_lang.upper()
    body = "\n".join(
        f"[{line.id}] {line.speaker or '?'}\n"
        f"  {src_label}: {line.source}\n  {target}: {line.target}"
        for line in lines
    )
    glossary_block = "; ".join(f"{k} -> {v}" for k, v in glossary.items()) if glossary else "(none)"
    gender_block = "; ".join(f"{k}: {v}" for k, v in genders.items()) if genders else "(none)"
    return (
        f"You are reviewing a {target} subtitle translation. For each line you are "
        f"given the {src_label} source and its translation.\n\n"
        f"Confirmed character genders: {gender_block}\n"
        f"Series glossary: {glossary_block}\n\n"
        "Report problems using exactly one of these `kind` tokens:\n"
        "- gender (wrong grammatical gender)\n"
        "- pronoun (wrong pronoun)\n"
        "- formality (inconsistent register/politeness for the target language)\n"
        "- proper_name (mistranslated proper name)\n"
        "- glossary (glossary rendering not respected)\n"
        "- honorific (broken honorific)\n"
        "- literal (overly literal phrasing)\n"
        "- unnatural (unnatural phrasing)\n"
        "- meaning (loss of meaning)\n"
        "Also report GLOBAL inconsistencies across the episode (a character's gender "
        "changing, a term translated several ways, inconsistent names).\n\n"
        "For each problem give: scope ('line' or 'global'), id (the line id, or null "
        "for global), kind (one token from the list), message, current (the current "
        "translation), suggested (the corrected line, or null if it needs a human), and "
        "auto_safe (true ONLY for objective fixes: kind glossary, proper_name, honorific, "
        "or gender when the character's gender is confirmed above). Jokes, double "
        "meanings, ambiguous gender, cultural adaptation and tone are never auto_safe.\n\n"
        "Reply with ONLY a JSON array of such objects, no prose, no code fences.\n\n"
        "LINES:\n"
        f"{body}\n"
    )


def _as_bool(value: object) -> bool:
    """Parse a model's auto_safe flag conservatively.

    Models sometimes return the JSON string "false" instead of the boolean false; `bool("false")`
    is True in Python, which would wrongly mark a finding as auto-safe. Treat only a real True or
    the string "true" as truthy, so an explicit "false" (or anything ambiguous) never auto-applies.
    """
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().casefold() == "true"
    return False


def parse_findings(raw: str) -> list[Finding]:
    text = extract_json(raw)
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ProviderError(
            f"Review reply was not valid JSON: {exc}",
            retryable=True,
            category="content",
        ) from exc
    if isinstance(data, dict):
        data = data.get("findings", [])
    if not isinstance(data, list):
        raise ProviderError(
            "Review reply must be a JSON array of findings.",
            retryable=True,
            category="content",
        )

    findings: list[Finding] = []
    for item in data:
        if not isinstance(item, dict):
            continue
        findings.append(
            Finding(
                scope=item.get("scope", "line"),
                id=item.get("id"),
                kind=str(item.get("kind", "other")),
                message=str(item.get("message", "")),
                current=item.get("current"),
                suggested=item.get("suggested"),
                auto=_as_bool(item.get("auto_safe", False)),
            )
        )
    return findings


def apply_safe_policy(
    findings: list[Finding],
    lines: list[ReviewLine],
    confirmed_genders: dict[str, str],
    glossary: dict[str, str] | None = None,
    names: list[str] | None = None,
) -> None:
    """Demote `auto` for anything that is not a vetted safe correction (in place)."""
    glossary = glossary or {}
    renderings = [v for v in glossary.values() if v.strip()]
    known_names = [n for n in (names or []) if n.strip()]
    speaker_by_id = {line.id: (line.speaker or "") for line in lines}
    for f in findings:
        has_nonempty_fix = f.suggested is not None and bool(f.suggested.strip())
        if not (
            f.auto and f.has_fix and has_nonempty_fix and f.scope == "line" and f.kind in SAFE_KINDS
        ):
            f.auto = False
            continue
        suggested = (f.suggested or "").casefold()
        if f.kind == "gender":
            speaker = speaker_by_id.get(f.id or "", "")
            if confirmed_genders.get(speaker) not in ("male", "female"):
                f.auto = False
        elif f.kind == "glossary":
            # A glossary fix must actually carry a glossary rendering; otherwise it is an
            # arbitrary rewrite mislabeled as `glossary`. Verify deterministically rather than
            # trusting the model's `auto_safe`.
            if not any(rendering.casefold() in suggested for rendering in renderings):
                f.auto = False
        elif f.kind == "proper_name":
            # A proper-name fix must introduce a known character name from series memory; with no
            # name to check against, we can't verify it deterministically, so we leave it for a
            # human instead of trusting the model's label.
            if not any(name.casefold() in suggested for name in known_names):
                f.auto = False


# Lines per review request. A whole long episode in one prompt risks truncation/timeouts and
# degrades attention; the model still sees an episode-spanning glossary/gender sheet in every block.
REVIEW_BLOCK_SIZE = 40


def review_lines(
    lines: list[ReviewLine],
    *,
    glossary: dict[str, str],
    genders: dict[str, str],
    target: str,
    source_lang: str = "source",
    names: list[str] | None = None,
    runner: Runner,
    max_retries: int = 2,
    block_size: int = REVIEW_BLOCK_SIZE,
) -> list[Finding]:
    if not lines:
        return []
    findings: list[Finding] = []
    for start in range(0, len(lines), block_size):
        chunk = lines[start : start + block_size]
        prompt = build_review_prompt(
            chunk, glossary=glossary, genders=genders, target=target, source_lang=source_lang
        )
        chunk_findings = retry_provider_call(
            partial(lambda p: parse_findings(runner(p)), prompt),
            max_retries=max_retries,
            label="Review",
        )
        apply_safe_policy(chunk_findings, chunk, genders, glossary, names)
        findings.extend(chunk_findings)
    return findings
