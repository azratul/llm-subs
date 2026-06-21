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
    status: Literal["translated", "skipped", "failed"]
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
class ReviewResult:
    report: ReviewReport
    report_path: Path
    translated_path: Path
    n_lines: int
    n_applied: int
    mapping_aligned: bool = True
    context_stale: bool = False  # episode.context.json was analyzed from a different source


@dataclass
class TightenResult:
    report_path: Path
    translated_path: Path
    n_subs: int
    n_flagged: int
    n_compacted: int
    n_applied: int
    n_residual: int


ConflictChoice = Literal["keep", "use", "skip"]
ConflictPrompt = Callable[[dict], ConflictChoice]
