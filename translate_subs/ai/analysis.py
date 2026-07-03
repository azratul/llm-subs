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
from collections.abc import Callable, Iterable
from typing import Literal

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


def output_source_digest(units: list[TranslatableUnit], *, extra_lines: Iterable[str] = ()) -> str:
    """Fingerprint of everything a source bakes into the rendered output, for the output manifest.

    Broader than `source_digest`: it also covers timing, style and the whole-line leading override
    block. A re-timed or re-styled source doesn't change the *translation* (so it must not flag the
    context sheet stale), but it does make the existing output no longer match the source — the
    subtitle would be desynchronised while still looking up to date. Including timing/style here
    lets `batch` flag such an output as stale so `--force` can re-render it (reusing the cached
    translations, which are keyed on text/context, not timing).

    `extra_lines` fingerprints source content that reaches the output but is not a translatable
    unit: for `.ass` the caller passes a serialization of the non-translatable events (drawings,
    comments, empty cues) that are copied through verbatim, so editing one flags the output stale
    too. `.srt` prunes those events, so it passes none — with no extras the hash is byte-identical
    to the units-only digest, keeping existing manifests valid.
    """
    parts = [
        f"{unit.id}\t{unit.start}\t{unit.end}\t{unit.style}\t{unit.lead_tags}\t"
        f"{unit.speaker or ''}\t{unit.text}"
        for unit in units
    ]
    # "~" namespaces preserved-event lines so they can never collide with a unit line above.
    parts.extend(f"~{line}" for line in extra_lines)
    return hashlib.sha256("\n".join(parts).encode("utf-8")).hexdigest()[:16]


CONTEXT_SCHEMA_VERSION: Literal[1] = 1

# Bump when the analysis prompt changes: folded into the context's provenance so `--pre-analyze`
# re-analyzes an episode whose cached context predates the change instead of trusting it.
ANALYSIS_PROMPT_VERSION = 1


def analysis_signature_for(provider: str, model: str | None) -> str:
    """Provenance of an analysis (prompt version + provider/model).

    Stored on the context so `skip_if_current` can tell an up-to-date context from one produced by
    a now-superseded prompt or a different backend — a change the source fingerprint can't see.
    """
    return f"{ANALYSIS_PROMPT_VERSION}|{provider}|{model or ''}"


class EpisodeCharacter(BaseModel):
    name: str
    gender: str = "unknown"  # "male" | "female" | "unknown"
    role: str | None = None
    speech_style: str | None = None
    relationships: dict[str, str] = Field(default_factory=dict)


class EpisodeContext(BaseModel):
    # Deliberately liberal (no extra="forbid"): this validates the raw model reply, so an
    # unrequested extra key from a chatty model is ignored rather than failing the analysis.
    # Default lets legacy files without the field still load as v1; a future format bump can
    # detect and migrate older files instead of trusting them blindly.
    schema_version: Literal[1] = CONTEXT_SCHEMA_VERSION
    episode_summary: str = ""
    characters: list[EpisodeCharacter] = Field(default_factory=list)
    glossary: dict[str, str] = Field(default_factory=dict)
    translation_rules: list[str] = Field(default_factory=list)
    # Fingerprint of the source it was analyzed from; None for legacy/older context files.
    source_hash: str | None = None
    # Provenance of the analysis (prompt version + provider/model). None on legacy files, which are
    # trusted as current rather than forced to re-analyze.
    analysis_signature: str | None = None


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
        "- characters: name (use the most complete form available — family + given "
        "for Japanese names; if a character already appears in the prior-known list "
        "under a shorter form, use that same canonical name), gender "
        "(male/female/unknown — use 'unknown' when the text is insufficient), role, "
        "speech_style, and relationships (map of other character -> relationship "
        f"description in {target}).\n"
        "- glossary: recurring proper terms (organizations, places, techniques, "
        "powers, titles, set phrases) mapped to their target-language rendering.\n"
        "- translation_rules: short directives specific to this episode "
        "(formality, honorifics, tone, names to keep).\n\n"
        "Write all prose fields (episode_summary, speech_style, all relationship "
        f"descriptions) in {target}.\n\n"
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
            category="content",
        ) from exc
    try:
        return EpisodeContext.model_validate(data)
    except Exception as exc:  # pydantic ValidationError
        raise ProviderError(
            f"Analysis reply did not match the schema: {exc}",
            retryable=True,
            category="content",
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
