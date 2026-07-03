"""Per-episode block translation checkpoint.

Translating a full episode is dozens of slow LLM calls; a crash on the last block must not
throw away the rest. Each block's result is persisted as soon as it returns, keyed by a hash of
the block's own translatable input (target + rules + the id/speaker/text of its lines).
Re-running reuses every block whose input is unchanged — both a resume after a failure and a
cache when only a few lines moved (the rest of the episode is reused verbatim).

The block's surrounding context (the before/after lines sent for reference) is part of the hash:
since context steers the translation (pronouns, register, callbacks), a block must be re-done if
its neighbours changed, even when its own lines did not. The checkpoint is scoped to a
provider/model signature: switching the backend (e.g. ollama -> claude) discards it so the new
model re-translates instead of inheriting the old output. The file is a regenerable cache, so a
corrupt or older-format one is treated as empty, never fatal.
"""

from __future__ import annotations

import hashlib
import json
import threading
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from translate_subs.ai.job_protocol import JobLine, TranslationJobIn
from translate_subs.ai.provider import TRANSLATION_PROMPT_VERSION, TranslationProvider
from translate_subs.fsutil import atomic_write_text

# Bumped when the persisted schema or hash payload changes. Version 3 validates the complete
# file strictly and includes the translation-prompt version in every block hash.
CHECKPOINT_VERSION = 3
CHECKPOINT_FILE = "translations.checkpoint.json"


def _lines(lines: list[JobLine]) -> list[dict[str, Any]]:
    return [{"id": line.id, "speaker": line.speaker, "text": line.text} for line in lines]


def block_hash(job: TranslationJobIn) -> str:
    """Stable hash of everything that steers a block's translation, context included."""
    payload = {
        "prompt_version": TRANSLATION_PROMPT_VERSION,
        "target": job.target,
        "rules": list(job.rules),
        "context_before": _lines(job.context_before),
        "translate": _lines(job.translate),
        "context_after": _lines(job.context_after),
    }
    blob = json.dumps(payload, ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:16]


@dataclass
class _Entry:
    block_id: str
    translations: dict[str, str]
    untranslated: list[str] = field(default_factory=list)


class _CheckpointEntryModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    block_id: str
    translations: dict[str, str]
    untranslated: list[str] = Field(default_factory=list)


class _CheckpointFileModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    version: int
    signature: str
    blocks: dict[str, _CheckpointEntryModel] = Field(default_factory=dict)


@dataclass
class BlockProgress:
    done: int
    total: int
    block_id: str
    reused: bool


@dataclass
class BlockCheckpoint:
    path: Path
    signature: str
    entries: dict[str, _Entry] = field(default_factory=dict)
    _lock: threading.Lock = field(
        default_factory=threading.Lock, init=False, repr=False, compare=False
    )

    @classmethod
    def load(cls, path: str | Path, signature: str) -> BlockCheckpoint:
        """Load the checkpoint; an unreadable, stale or wrong-signature file loads as empty."""
        path = Path(path)
        entries: dict[str, _Entry] = {}
        if path.exists():
            try:
                data = _CheckpointFileModel.model_validate_json(path.read_text("utf-8"))
            except (OSError, UnicodeDecodeError, ValidationError):
                data = None
            if (
                data is not None
                and data.version == CHECKPOINT_VERSION
                and data.signature == signature
            ):
                entries = {
                    h: _Entry(e.block_id, dict(e.translations), list(e.untranslated))
                    for h, e in data.blocks.items()
                }
        return cls(path, signature, entries)

    def save(self) -> None:
        data = {
            "version": CHECKPOINT_VERSION,
            "signature": self.signature,
            "blocks": {
                h: {
                    "block_id": e.block_id,
                    "translations": e.translations,
                    "untranslated": e.untranslated,
                }
                for h, e in self.entries.items()
            },
        }
        atomic_write_text(self.path, json.dumps(data, ensure_ascii=False, indent=2), private=True)

    def save_entry(self, h: str, entry: _Entry) -> None:
        """Thread-safe: add an entry to the checkpoint and persist it atomically."""
        with self._lock:
            self.entries[h] = entry
            self.save()


def _is_valid_cached(cached: _Entry, job: TranslationJobIn) -> bool:
    expected_ids = {line.id for line in job.translate}
    cached_ids = set(cached.translations)
    untranslated_ids = set(cached.untranslated)
    return (
        cached_ids == expected_ids
        and untranslated_ids <= expected_ids
        and all(text.strip() for text in cached.translations.values())
    )


def _reusable_entry(checkpoint: BlockCheckpoint, h: str, job: TranslationJobIn) -> _Entry | None:
    """Return the cached entry for `job` if it is still valid; drop it from the file otherwise.

    A structurally valid checkpoint can still contain a stale/mismatched entry: drop only that
    entry and let the caller regenerate the block instead of failing the whole translation.
    """
    cached = checkpoint.entries.get(h)
    if cached is None:
        return None
    if _is_valid_cached(cached, job):
        return cached
    checkpoint.entries.pop(h, None)
    return None


def _translate_sequential(
    provider: TranslationProvider,
    jobs: list[TranslationJobIn],
    checkpoint: BlockCheckpoint,
    on_progress: Callable[[BlockProgress], None] | None,
) -> tuple[dict[str, str], list[str]]:
    translations: dict[str, str] = {}
    untranslated: list[str] = []
    total = len(jobs)
    for i, job in enumerate(jobs, start=1):
        h = block_hash(job)
        cached = _reusable_entry(checkpoint, h, job)
        if cached is not None:
            translations.update(cached.translations)
            untranslated.extend(cached.untranslated)
            if on_progress:
                on_progress(BlockProgress(i, total, job.block_id, reused=True))
            continue
        block_map = provider.translate([job])
        block_untranslated = list(getattr(provider, "untranslated_ids", []))
        checkpoint.save_entry(h, _Entry(job.block_id, dict(block_map), block_untranslated))
        translations.update(block_map)
        untranslated.extend(block_untranslated)
        if on_progress:
            on_progress(BlockProgress(i, total, job.block_id, reused=False))
    return translations, untranslated


def _translate_parallel(
    translate_block: Callable[[TranslationJobIn], tuple[dict[str, str], list[str]]],
    jobs: list[TranslationJobIn],
    checkpoint: BlockCheckpoint,
    on_progress: Callable[[BlockProgress], None] | None,
    parallel: int,
) -> tuple[dict[str, str], list[str]]:
    """Serve cache hits first, then translate the misses concurrently in a thread pool."""
    translations: dict[str, str] = {}
    untranslated: list[str] = []
    total = len(jobs)
    pending: list[TranslationJobIn] = []
    done = 0
    for job in jobs:
        cached = _reusable_entry(checkpoint, block_hash(job), job)
        if cached is not None:
            translations.update(cached.translations)
            untranslated.extend(cached.untranslated)
            done += 1
            if on_progress:
                on_progress(BlockProgress(done, total, job.block_id, reused=True))
            continue
        pending.append(job)

    if not pending:
        return translations, untranslated

    progress_lock = threading.Lock()

    def _do_block(job: TranslationJobIn) -> tuple[str, str, dict[str, str], list[str]]:
        h = block_hash(job)
        block_translations, block_untranslated = translate_block(job)
        checkpoint.save_entry(h, _Entry(job.block_id, dict(block_translations), block_untranslated))
        return job.block_id, h, block_translations, block_untranslated

    executor = ThreadPoolExecutor(max_workers=parallel)
    try:
        futures = [executor.submit(_do_block, job) for job in pending]
        for future in as_completed(futures):
            block_id, _h, block_translations, block_untranslated = future.result()
            translations.update(block_translations)
            untranslated.extend(block_untranslated)
            with progress_lock:
                done += 1
                if on_progress:
                    on_progress(BlockProgress(done, total, block_id, reused=False))
    except BaseException:
        # On the first failed block (or Ctrl-C), surface the error NOW: cancel the blocks not
        # yet started and do not wait for the in-flight ones — a `with` block
        # (shutdown(wait=True)) would sit silent for up to the per-block timeout before the
        # user sees anything. An in-flight HTTP call can't be interrupted mid-request; it
        # finishes in a background thread and still persists its block via save_entry, so
        # nothing is lost for the resume.
        executor.shutdown(wait=False, cancel_futures=True)
        raise
    executor.shutdown()
    return translations, untranslated


def translate_with_checkpoint(
    provider: TranslationProvider,
    jobs: list[TranslationJobIn],
    *,
    checkpoint: BlockCheckpoint,
    on_progress: Callable[[BlockProgress], None] | None = None,
    parallel: int = 1,
) -> tuple[dict[str, str], list[str]]:
    """Translate `jobs` block by block, persisting each result before moving on.

    A block whose hash is already in `checkpoint` is reused without calling the provider. The
    checkpoint is saved after every freshly translated block, so an interruption (or a provider
    error) leaves the completed blocks recoverable on the next run. Pass an empty checkpoint to
    force a full re-translation while still writing fresh progress.

    When `parallel > 1` and the provider exposes a `translate_block` method (i.e. it is a
    `CliTranslationProvider`), cache hits are served first and then the remaining blocks are
    submitted concurrently to a thread pool. Results are collected in completion order and saved
    to the checkpoint under a lock as each block finishes. Suitable for API-backed runners
    (Ollama, LiteLLM) where the bottleneck is network/model latency, not subprocess overhead.
    """
    translate_block = getattr(provider, "translate_block", None)
    if parallel <= 1 or translate_block is None:
        # Sequential path: original behaviour, also handles providers without translate_block.
        return _translate_sequential(provider, jobs, checkpoint, on_progress)
    return _translate_parallel(translate_block, jobs, checkpoint, on_progress, parallel)
