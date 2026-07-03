"""Typed call signatures for the dependency seams the pipeline facade injects into workflows.

Each workflow takes its source resolution, provider construction and output validation as injected
callables so tests can pass fakes and the deterministic core stays decoupled from I/O. Modelling
those seams as `Protocol`s (rather than bare `Callable[..., X]`) lets mypy verify that both the
concrete function the facade supplies *and* every call site inside the workflows agree on the
keyword arguments — a check `Callable[..., X]` silently skips.

The per-episode `translate_fn`/`analyze_fn` batch seams are deliberately left as loose
`Callable[..., X]` in `translation.py`: `batch_*` forwards an arbitrary `**kwargs` to them, which a
fixed `__call__` signature could not express without rejecting the concrete facade functions.
"""

from __future__ import annotations

from pathlib import Path
from typing import Protocol

from translate_subs.ai.cli_adapters import Runner
from translate_subs.ai.provider import TranslationProvider
from translate_subs.domain.models import TranslatableUnit
from translate_subs.io.source_resolver import ResolvedSource
from translate_subs.subs.validator import ValidationResult


class ResolveSourceFn(Protocol):
    """Resolve an input path to the subtitle to work on (sidecar or embedded track)."""

    def __call__(
        self,
        input_path: str | Path,
        *,
        work_dir: str | Path,
        lang: str | None = ...,
        track_index: int | None = ...,
        interactive: bool = ...,
        strict_lang: bool = ...,
    ) -> ResolvedSource: ...


class ProviderFactory(Protocol):
    """Build the translation provider for a backend name (identity/file-handoff/CLI/API)."""

    def __call__(
        self,
        name: str,
        jobs_dir: Path,
        *,
        model: str | None = ...,
        reasoning: str | None = ...,
        max_retries: int = ...,
        timeout: int | None = ...,
    ) -> TranslationProvider: ...


class RunnerFactory(Protocol):
    """Build a bare text-in/text-out runner for the analyze/review/compact LLM passes."""

    def __call__(
        self,
        provider: str,
        *,
        model: str | None = ...,
        reasoning: str | None = ...,
        timeout: int | None = ...,
    ) -> Runner: ...


class ValidateOutputFn(Protocol):
    """Reopen a written subtitle and check its structural integrity against the source units."""

    def __call__(
        self,
        srt_path: str | Path,
        units: list[TranslatableUnit],
        *,
        check_fidelity: bool = ...,
    ) -> ValidationResult: ...


class DiscoverInputsFn(Protocol):
    """List the batch inputs under a directory, excluding this tool's own outputs."""

    def __call__(
        self,
        directory: str | Path,
        *,
        globs: tuple[str, ...] = ...,
        recursive: bool = ...,
        target: str = ...,
    ) -> list[Path]: ...
