"""Episode context analysis (Phase 2).

Before translating, the model reads the whole episode (as a clean
`[ID] Speaker: text` transcript) and produces a context sheet: summary,
characters (gender/role/speech style/relationships), glossary and translation
rules. The sheet is saved and later folded into the translation prompts so the
result respects gender, formality (tú/usted/ustedes) and term consistency.

The core invariant holds: the raw subtitle file is never sent — only the
extracted visible text with stable IDs.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable

from pydantic import BaseModel, Field

from translate_subs.ai.claude_cli import extract_json
from translate_subs.ai.provider import ProviderError, retry_provider_call
from translate_subs.domain.models import TranslatableUnit

Runner = Callable[[str], str]


def source_digest(units: list[TranslatableUnit]) -> str:
    """Stable fingerprint of the analyzed source content (id, speaker and visible text).

    Stored in the saved context so `translate`/`review` can warn when the subtitle has changed
    since it was analyzed and the context sheet may no longer match. The speaker is part of the
    fingerprint because reassigning a line to a different character can change gender/register
    without altering the text — a change the context sheet would otherwise miss silently.
    """
    blob = "\n".join(f"{unit.id}\t{unit.speaker or ''}\t{unit.text}" for unit in units)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:16]


class EpisodeCharacter(BaseModel):
    name: str
    gender: str = "unknown"  # "male" | "female" | "unknown"
    role: str | None = None
    speech_style: str | None = None
    relationships: dict[str, str] = Field(default_factory=dict)


class EpisodeContext(BaseModel):
    episode_summary: str = ""
    characters: list[EpisodeCharacter] = Field(default_factory=list)
    glossary: dict[str, str] = Field(default_factory=dict)
    translation_rules: list[str] = Field(default_factory=list)
    # Fingerprint of the source it was analyzed from; None for legacy/older context files.
    source_hash: str | None = None


TRANSCRIPT_LIMIT = 4000  # lines; guards against pathological inputs.


def build_transcript(units: list[TranslatableUnit]) -> str:
    lines = []
    for unit in units[:TRANSCRIPT_LIMIT]:
        speaker = unit.speaker or "?"
        text = unit.text.replace("\n", " ")
        lines.append(f"[{unit.id}] {speaker}: {text}")
    return "\n".join(lines)


def build_analysis_prompt(
    units: list[TranslatableUnit], *, target: str, prior_known: str | None = None
) -> str:
    transcript = build_transcript(units)
    prior_block = ""
    if prior_known:
        prior_block = (
            f"Known from earlier episodes (stay consistent; do not contradict):\n{prior_known}\n\n"
        )
    return (
        "You are a subtitle localization analyst. Read this full episode "
        f"transcript and produce a context sheet to guide translation into {target}.\n"
        "Each line is `[ID] Speaker: visible text`. Speakers may be missing ('?').\n\n"
        f"{prior_block}"
        "Infer, from dialogue alone:\n"
        "- episode_summary: 2-4 sentences.\n"
        "- characters: name, gender (male/female/unknown — use 'unknown' when the "
        "text is insufficient), role, speech_style, and relationships "
        "(map of other character -> relationship).\n"
        "- glossary: recurring proper terms (organizations, places, techniques, "
        "powers, titles, set phrases) mapped to their target-language rendering.\n"
        "- translation_rules: short directives specific to this episode "
        "(formality, honorifics, tone, names to keep).\n\n"
        "Reply with ONLY a JSON object matching this shape, no prose, no code fences:\n"
        '{"episode_summary": "", "characters": [{"name": "", "gender": "unknown", '
        '"role": "", "speech_style": "", "relationships": {}}], '
        '"glossary": {}, "translation_rules": []}\n\n'
        "TRANSCRIPT:\n"
        f"{transcript}\n"
    )


def parse_context(raw: str) -> EpisodeContext:
    """Validate the model reply into an EpisodeContext.

    Tolerates code fences and leading/trailing prose around the JSON object.
    """
    text = extract_json(raw)
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ProviderError(
            f"Analysis reply was not valid JSON: {exc}",
            retryable=True,
        ) from exc
    try:
        return EpisodeContext.model_validate(data)
    except Exception as exc:  # pydantic ValidationError
        raise ProviderError(
            f"Analysis reply did not match the schema: {exc}",
            retryable=True,
        ) from exc


def analyze_episode(
    units: list[TranslatableUnit],
    *,
    target: str,
    runner: Runner,
    prior_known: str | None = None,
    max_retries: int = 2,
) -> EpisodeContext:
    if not units:
        raise ProviderError("No translatable lines to analyze.", retryable=False)
    prompt = build_analysis_prompt(units, target=target, prior_known=prior_known)
    return retry_provider_call(
        lambda: parse_context(runner(prompt)),
        max_retries=max_retries,
        label="Analysis",
    )
