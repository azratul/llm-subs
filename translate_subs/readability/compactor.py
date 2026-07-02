"""LLM compaction pass for subtitles that exceed the readability limits."""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass
from functools import partial

from translate_subs.ai.claude_cli import extract_json
from translate_subs.ai.provider import ProviderError, retry_provider_call
from translate_subs.readability.metrics import LineMetrics, ReadabilityLimits

Runner = Callable[[str], str]


@dataclass
class FlaggedLine:
    id: str
    event_index: int
    text: str
    metrics: LineMetrics
    reasons: list[str]


def build_compaction_prompt(flagged: list[FlaggedLine], limits: ReadabilityLimits) -> str:
    items = []
    for line in flagged:
        budget = line.metrics.char_budget(limits)
        shown = line.text.replace("\n", " / ")
        items.append(
            f"[{line.id}] ({line.metrics.duration_ms / 1000:.1f}s, "
            f"max {budget} chars, issues: {', '.join(line.reasons)})\n  {shown}"
        )
    body = "\n".join(items)
    return (
        "Compact these subtitle lines so they read comfortably on screen, keeping the "
        "original meaning, tone and language. Constraints per line:\n"
        f"- at most {limits.max_lines} line(s), separated by a single '\\n';\n"
        f"- at most {limits.max_chars_per_line} characters per line;\n"
        "- total characters within the per-line 'max chars' budget shown "
        f"(≤ {limits.max_chars_per_second} chars/second).\n"
        "Shorten wording; do not drop essential information. A '/' in the input marks an "
        "existing line break.\n\n"
        "Reply with ONLY a JSON object mapping each id to its compacted text (use '\\n' "
        "for a line break), no prose, no code fences.\n\n"
        "LINES:\n"
        f"{body}\n"
    )


def parse_compactions(raw: str, requested: set[str]) -> dict[str, str]:
    try:
        data = json.loads(extract_json(raw))
    except json.JSONDecodeError as exc:
        raise ProviderError(
            f"Compaction reply was not valid JSON: {exc}",
            retryable=True,
            category="content",
        ) from exc
    if not isinstance(data, dict):
        raise ProviderError(
            "Compaction reply must be a JSON object of id -> text.",
            retryable=True,
            category="content",
        )
    extra = set(map(str, data)) - requested
    if extra:
        raise ProviderError(
            f"Compaction returned unknown ids: {sorted(extra)}.",
            retryable=True,
            category="content",
        )
    missing = requested - set(map(str, data))
    if missing:
        raise ProviderError(
            f"Compaction omitted ids: {sorted(missing)}.",
            retryable=True,
            category="content",
        )
    non_text = sorted(str(key) for key, value in data.items() if not isinstance(value, str))
    if non_text:
        raise ProviderError(
            f"Compaction returned non-string text for ids: {non_text}.",
            retryable=True,
            category="content",
        )
    result = {str(k): value for k, value in data.items()}
    empty = sorted(key for key, value in result.items() if not value.strip())
    if empty:
        raise ProviderError(
            f"Compaction returned empty text for ids: {empty}.",
            retryable=True,
            category="content",
        )
    return result


# Flagged lines per compaction request, mirroring the translation/review block size: a whole
# episode's worth of over-long lines in one prompt risks truncation and degrades the rewrite.
COMPACTION_BLOCK_SIZE = 40


def compact_lines(
    flagged: list[FlaggedLine],
    *,
    limits: ReadabilityLimits,
    runner: Runner,
    max_retries: int = 2,
    block_size: int = COMPACTION_BLOCK_SIZE,
) -> dict[str, str]:
    if not flagged:
        return {}
    result: dict[str, str] = {}
    for start in range(0, len(flagged), block_size):
        chunk = flagged[start : start + block_size]
        prompt = build_compaction_prompt(chunk, limits)
        requested = {line.id for line in chunk}
        result.update(
            retry_provider_call(
                partial(lambda p, ids: parse_compactions(runner(p), ids), prompt, requested),
                max_retries=max_retries,
                label="Compaction",
            )
        )
    return result
