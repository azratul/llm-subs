"""Shared result models and errors for application workflows."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

from translate_subs.ai.analysis import EpisodeContext
from translate_subs.io.source_resolver import ResolvedSource
from translate_subs.memory.compact import CompactReport
from translate_subs.memory.merge import MergeReport
from translate_subs.review.models import ReviewReport
from translate_subs.subs.validator import ValidationResult


class PipelineError(Exception):
    pass


class OutputExistsError(PipelineError):
    """Raised when an output file already exists and `--force` was not given.

    A distinct type so `batch` can record the episode as *skipped* (not *failed*) without
    pattern-matching the error message.
    """


class StaleOutputError(PipelineError):
    """Raised when an output exists but the source, provider/model or prompt changed since it was
    written (per its recorded manifest).

    A distinct type so `batch` can record the episode as *stale* — surfaced as a warning, never
    silently overwritten — instead of skipping it as up to date or failing it.
    """


class AnalysisCurrentError(Exception):
    """Raised by `analyze_subtitle` when the context is already current (source unchanged).

    Caught by `batch_analyze` to record the episode as *skipped* rather than *failed*.
    Not a `PipelineError` so it bypasses the general per-episode error handler.
    """


@dataclass
class TranslateResult:
    source: ResolvedSource
    output_path: Path
    n_units: int
    n_jobs: int
    output_validation: ValidationResult
    context_used: bool
    memory_used: bool
    untranslated_ids: list[str] = field(default_factory=list)
    context_stale: bool = False  # episode.context.json was analyzed from a different source


@dataclass
class BatchItem:
    input_path: Path
    status: Literal["translated", "skipped", "stale", "failed"]
    output_path: Path | None = None
    error: str | None = None
    untranslated_ids: list[str] = field(default_factory=list)


@dataclass
class BatchResult:
    items: list[BatchItem] = field(default_factory=list)

    @property
    def n_translated(self) -> int:
        return sum(1 for item in self.items if item.status == "translated")

    @property
    def n_skipped(self) -> int:
        return sum(1 for item in self.items if item.status == "skipped")

    @property
    def n_stale(self) -> int:
        return sum(1 for item in self.items if item.status == "stale")

    @property
    def n_failed(self) -> int:
        return sum(1 for item in self.items if item.status == "failed")


@dataclass
class AnalyzeBatchItem:
    input_path: Path
    status: Literal["analyzed", "skipped", "failed"]
    error: str | None = None


@dataclass
class AnalyzeBatchResult:
    items: list[AnalyzeBatchItem] = field(default_factory=list)

    @property
    def n_analyzed(self) -> int:
        return sum(1 for item in self.items if item.status == "analyzed")

    @property
    def n_skipped(self) -> int:
        return sum(1 for item in self.items if item.status == "skipped")

    @property
    def n_failed(self) -> int:
        return sum(1 for item in self.items if item.status == "failed")


@dataclass
class AnalyzeResult:
    source: ResolvedSource
    context_path: Path
    context: EpisodeContext
    n_units: int
    merge: MergeReport
    truncated_lines: int = 0


@dataclass
class UpdateMemoryResult:
    project_dir: Path
    context_path: Path
    merge: MergeReport


@dataclass
class CompactMemoryResult:
    project_dir: Path
    report: CompactReport


@dataclass
class ResolveConflictsResult:
    project_dir: Path
    resolved: int
    remaining: int


@dataclass
class EpisodeStatus:
    name: str
    analyzed: bool  # episode.context.json present
    # A checkpoint file is on disk. Not "resumable": whether its blocks are reused depends on the
    # provider/model/content matching at run time, which an offline status view can't verify.
    has_checkpoint_file: bool
    outputs: list[str] = field(default_factory=list)  # output paths recorded in each manifest


@dataclass
class ProjectStatusResult:
    project_dir: Path
    target: str
    glossary_terms: int
    characters: int
    conflicts: int
    episodes: list[EpisodeStatus] = field(default_factory=list)


@dataclass
class ReviewResult:
    report: ReviewReport
    report_path: Path
    translated_path: Path
    n_lines: int
    n_applied: int
    mapping_aligned: bool = True
    context_stale: bool = False  # episode.context.json was analyzed from a different source
    # (id, old_text, new_text) pairs for fixes that would/were applied
    planned_fixes: list[tuple[str, str, str]] = field(default_factory=list)
    applied_fixes: list[tuple[str, str, str]] = field(default_factory=list)


@dataclass
class TightenResult:
    report_path: Path
    translated_path: Path
    n_subs: int
    n_flagged: int
    n_compacted: int
    n_applied: int
    n_residual: int
    # (id, old_text, new_text) pairs for compactions that were written
    applied_compactions: list[tuple[str, str, str]] = field(default_factory=list)


ConflictChoice = Literal["keep", "use", "skip"]
ConflictPrompt = Callable[[dict], ConflictChoice]
