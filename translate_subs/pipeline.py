"""Stable public facade for translation, memory, review and readability workflows."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

from translate_subs import config
from translate_subs.ai.checkpoint import BlockProgress
from translate_subs.io.source_resolver import resolve_source
from translate_subs.memory.merge import ConflictPolicy, ConflictResolver
from translate_subs.naming import DEFAULT_FORMAT
from translate_subs.readability.metrics import ReadabilityLimits
from translate_subs.review.checks import DEFAULT_MAX_CHARS
from translate_subs.review.structure import pair_lines as _pair_lines_impl
from translate_subs.subs.validator import ValidationResult, validate_output
from translate_subs.workflows.memory import (
    _apply_conflict_choice as _apply_conflict_choice_impl,
)
from translate_subs.workflows.memory import (
    analyze_subtitle as _analyze_subtitle,
)
from translate_subs.workflows.memory import (
    compact_memory as _compact_memory,
)
from translate_subs.workflows.memory import (
    merge_into_memory as _merge_into_memory_impl,
)
from translate_subs.workflows.memory import prior_known as _prior_known_impl
from translate_subs.workflows.memory import (
    resolve_conflicts as _resolve_conflicts,
)
from translate_subs.workflows.memory import update_memory as _update_memory
from translate_subs.workflows.models import (
    AnalyzeBatchResult,
    AnalyzeResult,
    BatchResult,
    CompactMemoryResult,
    ConflictPrompt,
    ResolveConflictsResult,
    ReviewResult,
    TightenResult,
    TranslateResult,
    UpdateMemoryResult,
)
from translate_subs.workflows.models import BatchItem as _BatchItem
from translate_subs.workflows.models import (
    ConflictChoice as _ConflictChoice,
)
from translate_subs.workflows.models import OutputExistsError as _OutputExistsError
from translate_subs.workflows.models import PipelineError as _PipelineError
from translate_subs.workflows.quality import (
    review_translation as _review_translation,
)
from translate_subs.workflows.quality import (
    tighten_subtitle as _tighten_subtitle,
)
from translate_subs.workflows.quality import (
    validate_subtitle as _validate_subtitle,
)
from translate_subs.workflows.support import (
    atomic_save as _atomic_save_impl,
)
from translate_subs.workflows.support import (
    context_path as _context_path,
)
from translate_subs.workflows.support import (
    make_ai_runner,
    make_provider,
)
from translate_subs.workflows.support import (
    project_dir as _project_dir,
)
from translate_subs.workflows.support import (
    project_episode as _project_episode_impl,
)
from translate_subs.workflows.support import (
    readability_path as _readability_path,
)
from translate_subs.workflows.support import (
    review_path as _review_path,
)
from translate_subs.workflows.translation import (
    DEFAULT_BATCH_GLOBS,
)
from translate_subs.workflows.translation import (
    batch_analyze as _batch_analyze,
)
from translate_subs.workflows.translation import (
    batch_translate as _batch_translate,
)
from translate_subs.workflows.translation import (
    discover_inputs as _discover_inputs,
)
from translate_subs.workflows.translation import (
    translate_subtitle as _translate_subtitle,
)

ConflictChoice = _ConflictChoice
BatchItem = _BatchItem
PipelineError = _PipelineError
OutputExistsError = _OutputExistsError
project_dir = _project_dir
context_path = _context_path
review_path = _review_path
readability_path = _readability_path
_project_episode = _project_episode_impl
_pair_lines = _pair_lines_impl
_atomic_save = _atomic_save_impl
_prior_known = _prior_known_impl
_merge_into_memory = _merge_into_memory_impl
_apply_conflict_choice = _apply_conflict_choice_impl


def analyze_subtitle(
    input_path: str | Path,
    *,
    target: str = "es-latam",
    track_index: int | None = None,
    lang: str = "en",
    project: str | None = None,
    interactive: bool = True,
    on_conflict: ConflictPolicy = "flag",
    conflict_resolver: ConflictResolver | None = None,
    provider: str = "claude",
    model: str | None = None,
    reasoning: str | None = None,
    max_retries: int = 2,
    runner=None,
) -> AnalyzeResult:
    """Analyze the full episode, save episode.context.json, and update series memory."""
    return _analyze_subtitle(
        input_path,
        target=target,
        track_index=track_index,
        lang=lang,
        project=project,
        interactive=interactive,
        on_conflict=on_conflict,
        conflict_resolver=conflict_resolver,
        provider=provider,
        model=model,
        reasoning=reasoning,
        max_retries=max_retries,
        runner=runner,
        resolve_source_fn=resolve_source,
        ai_runner_factory=make_ai_runner,
    )


def update_memory(
    input_path: str | Path,
    *,
    target: str = "es-latam",
    track_index: int | None = None,
    lang: str = "en",
    project: str | None = None,
    interactive: bool = True,
    on_conflict: ConflictPolicy = "flag",
    conflict_resolver: ConflictResolver | None = None,
) -> UpdateMemoryResult:
    """Re-merge an existing episode.context.json into series memory (no LLM call)."""
    return _update_memory(
        input_path,
        target=target,
        track_index=track_index,
        lang=lang,
        project=project,
        interactive=interactive,
        on_conflict=on_conflict,
        conflict_resolver=conflict_resolver,
        resolve_source_fn=resolve_source,
    )


def compact_memory(project: str, target: str = "es-latam") -> CompactMemoryResult:
    """Prune redundant entries from a series' memory (no LLM call)."""
    return _compact_memory(project, target)


def resolve_conflicts(
    project: str, prompt: ConflictPrompt, target: str = "es-latam"
) -> ResolveConflictsResult:
    """Walk flagged conflicts; apply each decision and drop resolved ones from the log."""
    return _resolve_conflicts(project, prompt, target)


def translate_subtitle(
    input_path: str | Path,
    *,
    target: str = "es-latam",
    provider: str = "claude",
    track_index: int | None = None,
    lang: str = "en",
    out_dir: str | Path | None = None,
    output: str | Path | None = None,
    fmt: str = DEFAULT_FORMAT,
    project: str | None = None,
    interactive: bool = True,
    use_context: bool = True,
    model: str | None = None,
    reasoning: str | None = None,
    max_retries: int = 2,
    force: bool = False,
    strict_lang: bool = False,
    resume: bool = True,
    on_progress: Callable[[BlockProgress], None] | None = None,
) -> TranslateResult:
    """Resolve the source, translate by blocks, and export `<base>.<lang>.<fmt>`.

    Series memory (glossary, characters, style guide) and, if present, the episode
    context are folded into the translation prompts; series decisions take precedence.
    The default `.ass` keeps style-level positioning so simultaneous cues (e.g. a
    translator note above the dialogue) stay readable; `.srt` is flat, so overlapping
    cues are merged into single stacked cues.
    """
    return _translate_subtitle(
        input_path,
        target=target,
        provider=provider,
        track_index=track_index,
        lang=lang,
        out_dir=out_dir,
        output=output,
        fmt=fmt,
        project=project,
        interactive=interactive,
        use_context=use_context,
        model=model,
        reasoning=reasoning,
        max_retries=max_retries,
        force=force,
        strict_lang=strict_lang,
        resume=resume,
        on_progress=on_progress,
        resolve_source_fn=resolve_source,
        provider_factory=make_provider,
        validate_output_fn=validate_output,
    )


def discover_inputs(
    directory: str | Path,
    *,
    globs: tuple[str, ...] = DEFAULT_BATCH_GLOBS,
    recursive: bool = False,
    target: str = config.DEFAULT_TARGET,
) -> list[Path]:
    """List input files in `directory` matching `globs`, sorted, deduplicated.

    Files that already look like this tool's own output for `target` (their stem ends with the
    target language code, e.g. `ep01.es.srt`) are skipped, so re-globbing a directory with a
    subtitle pattern never feeds a previous translation back in as a source.
    """
    return _discover_inputs(
        directory,
        globs=globs,
        recursive=recursive,
        target=target,
    )


def batch_analyze(
    directory: str | Path,
    *,
    globs: tuple[str, ...] = DEFAULT_BATCH_GLOBS,
    recursive: bool = False,
    on_episode: Callable[[int, int, Path], None] | None = None,
    **analyze_kwargs,
) -> AnalyzeBatchResult:
    """Analyze every matching file in `directory` to build series memory.

    Each file goes through `analyze_subtitle` with the shared `analyze_kwargs`. A failed
    episode is recorded and the batch moves on. Meant to run before `batch_translate` so the
    full series memory (characters, glossary, style guide) is available for every translation.
    `on_episode(index, total, path)` is called before each file for progress reporting.
    """
    return _batch_analyze(
        directory,
        globs=globs,
        recursive=recursive,
        on_episode=on_episode,
        discover_inputs_fn=discover_inputs,
        analyze_fn=analyze_subtitle,
        **analyze_kwargs,
    )


def batch_translate(
    directory: str | Path,
    *,
    globs: tuple[str, ...] = DEFAULT_BATCH_GLOBS,
    recursive: bool = False,
    on_episode: Callable[[int, int, Path], None] | None = None,
    **translate_kwargs,
) -> BatchResult:
    """Translate every matching file in `directory`, continuing past per-episode failures.

    Each file goes through `translate_subtitle` with the shared `translate_kwargs`. An episode
    whose output already exists is recorded as skipped (unless `force=True`); one that raises is
    recorded as failed and the batch moves on, so a single bad episode never aborts the season.
    `on_episode(index, total, path)` is called before each file for progress reporting.
    """
    return _batch_translate(
        directory,
        globs=globs,
        recursive=recursive,
        on_episode=on_episode,
        discover_inputs_fn=discover_inputs,
        translate_fn=translate_subtitle,
        **translate_kwargs,
    )


def review_translation(
    input_path: str | Path,
    translated_path: str | Path,
    *,
    target: str = "es-latam",
    track_index: int | None = None,
    lang: str = "en",
    project: str | None = None,
    interactive: bool = True,
    max_chars: int = DEFAULT_MAX_CHARS,
    use_llm: bool = True,
    apply: bool = False,
    provider: str = "claude",
    model: str | None = None,
    reasoning: str | None = None,
    max_retries: int = 2,
    runner=None,
) -> ReviewResult:
    """Review a translation, write episode.review.md, optionally apply safe fixes."""
    return _review_translation(
        input_path,
        translated_path,
        target=target,
        track_index=track_index,
        lang=lang,
        project=project,
        interactive=interactive,
        max_chars=max_chars,
        use_llm=use_llm,
        apply=apply,
        provider=provider,
        model=model,
        reasoning=reasoning,
        max_retries=max_retries,
        runner=runner,
        resolve_source_fn=resolve_source,
        ai_runner_factory=make_ai_runner,
    )


def tighten_subtitle(
    translated_path: str | Path,
    *,
    target: str = "es-latam",
    project: str | None = None,
    limits: ReadabilityLimits | None = None,
    use_llm: bool = True,
    apply: bool = False,
    provider: str = "claude",
    model: str | None = None,
    reasoning: str | None = None,
    max_retries: int = 2,
    runner=None,
) -> TightenResult:
    """Measure readability of a translated subtitle, compact over-limit lines, report."""
    return _tighten_subtitle(
        translated_path,
        target=target,
        project=project,
        limits=limits,
        use_llm=use_llm,
        apply=apply,
        provider=provider,
        model=model,
        reasoning=reasoning,
        max_retries=max_retries,
        runner=runner,
        ai_runner_factory=make_ai_runner,
    )


def validate_subtitle(path: str | Path) -> ValidationResult:
    """Structural validation of an existing subtitle file."""
    return _validate_subtitle(path)
