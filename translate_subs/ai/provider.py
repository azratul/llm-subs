"""Translation providers.

The core is deterministic; translation is a decoupled step behind this interface.
Phase 1 ships two providers without real AI:

- IdentityProvider: copies the source text, to verify the round-trip.
- FileHandoffProvider: writes `*.in.json` and reads `*.out.json` filled by a person
  or an agent. This is the base protocol (no API cost).
"""

from __future__ import annotations

import json
import random
import time
from abc import ABC, abstractmethod
from collections.abc import Callable
from pathlib import Path
from typing import TypeVar

from translate_subs.ai.job_protocol import JobLine, TranslationJobIn, TranslationJobOut
from translate_subs.fsutil import atomic_write_text

TRANSLATION_PROMPT_VERSION = 1
_PERMANENT_BACKEND_MARKERS = (
    "auth error",
    "authentication",
    "unauthorized",
    "forbidden",
    "invalid api key",
    "not authenticated",
    "please log in",
    "unknown model",
    "model not found",
    "invalid option",
    "unknown option",
    "usage:",
)


def backend_error_is_retryable(message: str) -> bool:
    """Classify common permanent backend failures that retries cannot repair."""
    normalized = message.casefold()
    return not any(marker in normalized for marker in _PERMANENT_BACKEND_MARKERS)


class ProviderError(Exception):
    """Backend/protocol failure with retry metadata."""

    def __init__(
        self,
        message: str,
        *,
        retryable: bool = False,
        retry_after: float | None = None,
    ) -> None:
        self.retryable = retryable
        self.retry_after = retry_after
        super().__init__(message)


class IncompleteTranslation(ProviderError):
    """Reply is structurally valid (ids match) but some lines came back empty.

    Carries the parsed mapping so callers can fall back to the source text for the
    empty ids instead of discarding the whole block.
    """

    def __init__(self, block_id: str, translations: dict[str, str], empty_ids: list[str]) -> None:
        self.block_id = block_id
        self.translations = translations
        self.empty_ids = empty_ids
        super().__init__(
            f"Block {block_id}: empty translations for ids {empty_ids}.",
            retryable=True,
        )


_T = TypeVar("_T")


def retry_provider_call(
    fn: Callable[[], _T],
    *,
    max_retries: int,
    label: str,
    backoff_base: float = 1.0,
    backoff_cap: float = 30.0,
    jitter_ratio: float = 0.1,
    sleep: Callable[[float], None] = time.sleep,
    random_fn: Callable[[], float] = random.random,
) -> _T:
    """Run `fn`, retrying on `ProviderError` (agent/JSON failures).

    Raises a single labeled `ProviderError` once the attempts are exhausted, chaining
    the last failure. `max_retries` is retries *after* the first try (clamped at 0).

    Only retryable failures are attempted again. Between attempts it honours an explicit
    Retry-After delay when present; otherwise it waits with capped exponential backoff plus
    positive jitter. `sleep` and `random_fn` are injectable so tests stay deterministic.
    """
    attempts = max(0, max_retries) + 1
    last_error: ProviderError | None = None
    for i in range(attempts):
        try:
            return fn()
        except ProviderError as exc:
            last_error = exc
            if not exc.retryable:
                raise ProviderError(f"{label} failed: {exc}", retryable=False) from exc
            if i < attempts - 1:
                if exc.retry_after is not None:
                    delay = max(0.0, exc.retry_after)
                else:
                    base_delay = min(backoff_cap, max(0.0, backoff_base) * (2**i))
                    jitter = base_delay * max(0.0, jitter_ratio) * random_fn()
                    delay = min(backoff_cap, base_delay + jitter)
                if delay > 0:
                    sleep(delay)
    assert last_error is not None
    raise ProviderError(
        f"{label} failed after {attempts} attempt(s): {last_error}",
        retryable=last_error.retryable,
    ) from last_error


class TranslationProvider(ABC):
    @abstractmethod
    def translate(self, jobs: list[TranslationJobIn]) -> dict[str, str]:
        """Return an id -> translated text mapping for every line in the jobs."""


class IdentityProvider(TranslationProvider):
    """Passthrough: translation equals the source text (round-trip test)."""

    def translate(self, jobs: list[TranslationJobIn]) -> dict[str, str]:
        result: dict[str, str] = {}
        for job in jobs:
            for line in job.translate:
                result[line.id] = line.text
        return result


class FileHandoffProvider(TranslationProvider):
    """Write jobs to disk and read back whatever translations are returned."""

    def __init__(self, jobs_dir: str | Path) -> None:
        self.jobs_dir = Path(jobs_dir)

    def translate(self, jobs: list[TranslationJobIn]) -> dict[str, str]:
        self.jobs_dir.mkdir(parents=True, exist_ok=True)
        pending: list[str] = []
        result: dict[str, str] = {}

        for job in jobs:
            in_path = self.jobs_dir / f"block_{job.block_id}.in.json"
            out_path = self.jobs_dir / f"block_{job.block_id}.out.json"
            atomic_write_text(in_path, job.model_dump_json(indent=2), private=True)
            if not out_path.exists():
                pending.append(out_path.name)
                continue
            out = TranslationJobOut.model_validate_json(out_path.read_text("utf-8"))
            # Reject a stale or mismatched output: the file must be for this block and carry
            # exactly the ids this block asked to translate, so an old *.out.json can't slip in.
            if out.block_id != job.block_id:
                raise ProviderError(
                    f"{out_path.name}: block_id '{out.block_id}' does not match "
                    f"'{job.block_id}'. The output file is stale or misplaced."
                )
            expected = {line.id for line in job.translate}
            got = set(out.translations)
            if got != expected:
                missing = sorted(expected - got)
                unknown = sorted(got - expected)
                raise ProviderError(
                    f"{out_path.name}: id mismatch for block {job.block_id} "
                    f"(missing {missing[:3]}, unknown {unknown[:3]})."
                )
            result.update(out.translations)

        if pending:
            raise ProviderError(
                f"Missing {len(pending)} output files in {self.jobs_dir} "
                f"(e.g. {pending[:3]}). Fill the *.out.json files and rerun."
            )
        return result


def _format_lines(lines: list[JobLine]) -> str:
    # Each unit must stay on a single physical line so the `[ID] Speaker: text` framing is
    # unambiguous. A cue with an internal break carries a real newline in `text`; left raw it
    # would split into an unlabeled second physical line the model can't attribute to an id. Show
    # the break as the literal two-character token `\n` (the prompt tells the model to keep it).
    rendered = []
    for line in lines:
        text = line.text.replace("\n", "\\n")
        rendered.append(f"[{line.id}] {line.speaker or '?'}: {text}")
    return "\n".join(rendered)


def build_translation_prompt(job: TranslationJobIn) -> str:
    parts = [
        f"Translate the subtitle lines below into {job.target}.",
        "Each line is `[ID] Speaker: visible text`. Translate ONLY the lines under "
        "TRANSLATE; the CONTEXT lines are for reference and must not be returned.",
        "A line break inside a cue is shown as the literal token \\n; keep it as \\n in your "
        "output. Preserve meaning and tone. Do not add or drop lines.",
    ]
    if job.rules:
        parts.append("Rules:\n" + "\n".join(f"- {r}" for r in job.rules))
    if job.context_before:
        parts.append("CONTEXT (before):\n" + _format_lines(job.context_before))
    parts.append("TRANSLATE:\n" + _format_lines(job.translate))
    if job.context_after:
        parts.append("CONTEXT (after):\n" + _format_lines(job.context_after))
    parts.append(
        "Reply with ONLY a JSON object mapping each TRANSLATE id to its translation, "
        "no prose, no code fences. Use exactly these ids: "
        + ", ".join(line.id for line in job.translate)
    )
    return "\n\n".join(parts)


def parse_translation_reply(raw: str, job: TranslationJobIn) -> dict[str, str]:
    from translate_subs.ai.claude_cli import extract_json

    try:
        data = json.loads(extract_json(raw))
    except json.JSONDecodeError as exc:
        raise ProviderError(
            f"Block {job.block_id}: reply was not valid JSON: {exc}",
            retryable=True,
        ) from exc
    if not isinstance(data, dict):
        raise ProviderError(
            f"Block {job.block_id}: expected a JSON object of id -> text.",
            retryable=True,
        )

    expected = {line.id for line in job.translate}
    got = set(map(str, data))
    if got != expected:
        missing = sorted(expected - got)
        extra = sorted(got - expected)
        raise ProviderError(
            f"Block {job.block_id}: id mismatch (missing={missing}, extra={extra}).",
            retryable=True,
        )
    # Each value must already be a string; a list/dict from the model must not be silently
    # coerced (str([...]) would produce a bogus but non-empty "translation").
    non_text = sorted(k for k, v in data.items() if not isinstance(v, str))
    if non_text:
        raise ProviderError(
            f"Block {job.block_id}: non-string translations for {non_text[:3]}.",
            retryable=True,
        )
    # The source is shown with line breaks as the literal token `\n`, so the model echoes it back
    # the same way; turn it into a real newline (a model that instead emitted a JSON newline escape
    # already has one, and this leaves that untouched) so reinsertion produces an actual break.
    translations = {str(k): v.replace("\\n", "\n") for k, v in data.items()}
    empty = sorted(key for key, value in translations.items() if not value.strip())
    if empty:
        raise IncompleteTranslation(job.block_id, translations, empty)
    return translations


class CliTranslationProvider(TranslationProvider):
    """Translate block by block through an agent CLI runner.

    `runner` is injectable (a callable `prompt -> reply`) so tests can avoid the
    real CLI; by default it wraps `ClaudeCli`.
    """

    def __init__(
        self,
        runner: Callable[[str], str] | None = None,
        *,
        max_retries: int = 2,
    ) -> None:
        if runner is None:
            from translate_subs.ai.claude_cli import ClaudeCli

            runner = ClaudeCli()
        self.runner = runner
        self.max_retries = max(0, max_retries)
        # Ids whose translation stayed empty across all attempts; we keep the source
        # text for these so one stubborn line never aborts the whole episode.
        self.untranslated_ids: list[str] = []

    def translate_block(self, job: TranslationJobIn) -> tuple[dict[str, str], list[str]]:
        """Translate a single job block, returning (translations, untranslated_ids).

        All state is local to this call, so it is safe to invoke from multiple threads
        concurrently (each thread gets its own return values, no shared mutation).
        """

        def call() -> dict[str, str]:
            return parse_translation_reply(self.runner(build_translation_prompt(job)), job)

        untranslated: list[str] = []
        try:
            translations = retry_provider_call(
                call,
                max_retries=self.max_retries,
                label=f"Block {job.block_id}",
            )
        except ProviderError as exc:
            incomplete = exc.__cause__
            if not isinstance(incomplete, IncompleteTranslation):
                raise
            originals = {line.id: line.text for line in job.translate}
            translations = dict(incomplete.translations)
            for line_id in incomplete.empty_ids:
                translations[line_id] = originals[line_id]
                untranslated.append(line_id)
        return translations, untranslated

    def translate(self, jobs: list[TranslationJobIn]) -> dict[str, str]:
        result: dict[str, str] = {}
        self.untranslated_ids = []
        for job in jobs:
            block_translations, block_untranslated = self.translate_block(job)
            result.update(block_translations)
            self.untranslated_ids.extend(block_untranslated)
        return result


# Back-compat alias; any CLI runner can be injected.
ClaudeTranslationProvider = CliTranslationProvider
