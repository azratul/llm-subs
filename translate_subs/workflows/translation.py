"""Single-file and batch translation workflows."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

from translate_subs import config
from translate_subs.ai.analysis import EpisodeContext, source_digest
from translate_subs.ai.blocks import build_jobs
from translate_subs.ai.checkpoint import (
    CHECKPOINT_FILE,
    BlockCheckpoint,
    BlockProgress,
    translate_with_checkpoint,
)
from translate_subs.ai.cli_adapters import CLI_PROVIDERS
from translate_subs.ai.provider import ProviderError, TranslationProvider
from translate_subs.io.media_probe import MediaToolError
from translate_subs.io.source_resolver import SourceError
from translate_subs.memory.rules import build_memory_rules, rules_for_text
from translate_subs.memory.store import ProjectMemory
from translate_subs.naming import (
    DEFAULT_FORMAT,
    SUPPORTED_FORMATS,
    lang_code,
    output_path,
    validate_target,
)
from translate_subs.subs import document
from translate_subs.subs.extractor import extract_units
from translate_subs.subs.reinserter import apply_translations, flatten_overlaps, prune_to_units
from translate_subs.subs.validator import (
    ValidationResult,
    validate_file,
    validate_translations,
)
from translate_subs.workflows.models import (
    AnalysisCurrentError,
    AnalyzeBatchItem,
    AnalyzeBatchResult,
    BatchItem,
    BatchResult,
    OutputExistsError,
    PipelineError,
    TranslateResult,
)
from translate_subs.workflows.support import (
    atomic_save,
    context_path,
    episode_dir,
    memory_root,
    project_episode,
)

DEFAULT_BATCH_GLOBS = ("*.mkv",)
_EXPECTED_PIPELINE_ERRORS = (ProviderError, SourceError, MediaToolError, OSError, ValueError)
# API-backed providers that benefit from parallel block translation (pure HTTP, no subprocess).
_API_PROVIDERS = frozenset({"ollama", "litellm"})
_DEFAULT_API_PARALLEL = 4
ProviderFactory = Callable[..., TranslationProvider]


def _same_path(a: str | Path, b: str | Path) -> bool:
    """True if two paths point at the same location (resolved, even if not yet created)."""
    return Path(a).resolve() == Path(b).resolve()


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
    parallel: int | None = None,
    timeout: int | None = None,
    on_progress: Callable[[BlockProgress], None] | None = None,
    resolve_source_fn,
    provider_factory: ProviderFactory,
    validate_output_fn,
) -> TranslateResult:
    """Resolve the source, translate by blocks and export the requested subtitle format."""
    if fmt not in SUPPORTED_FORMATS:
        raise PipelineError(
            f"Unsupported format '{fmt}'. Use one of: {', '.join(SUPPORTED_FORMATS)}."
        )
    try:
        target = validate_target(target)
    except ValueError as exc:
        raise PipelineError(str(exc)) from exc
    source = resolve_source_fn(
        input_path,
        work_dir=config.WORK_DIR,
        lang=lang,
        track_index=track_index,
        interactive=interactive,
        strict_lang=strict_lang,
    )

    subs = document.load(source.subtitle_path)
    units = extract_units(subs)
    if not units:
        raise PipelineError("No translatable lines found in the subtitle.")

    if output is not None:
        out_file = Path(output).with_suffix(f".{fmt}")
    else:
        out_file = output_path(source.origin, fmt=fmt, out_dir=out_dir, lang=lang_code(target))
        # Defence in depth: the filename is derived from the (now alnum-only) target, so it must
        # stay a single component inside the intended directory and can't escape via the language.
        intended = (
            Path(out_dir).resolve() if out_dir is not None else source.origin.resolve().parent
        )
        if out_file.resolve().parent != intended:
            raise PipelineError(f"Refusing to write outside the output directory: {out_file}.")
    # Never write over the file we are reading from: a misaimed --output (or a same-name source)
    # would otherwise destroy the original subtitle, even with --force.
    if _same_path(out_file, source.subtitle_path) or _same_path(out_file, source.origin):
        raise PipelineError(
            f"Refusing to overwrite the source file with the output: {out_file}. "
            "Choose a different --output/--out-dir or --target."
        )
    if out_file.exists() and not force:
        raise OutputExistsError(f"Output already exists: {out_file}. Use --force to overwrite.")

    project_name, episode_name = project_episode(source, project)
    jobs_dir = episode_dir(project_name, target, episode_name) / "jobs"

    project_memory = ProjectMemory.load(memory_root(project_name, target))
    context_used = False
    context_stale = False
    context = None
    episode_context_path = context_path(project_name, target, episode_name)
    if use_context and episode_context_path.exists():
        context = EpisodeContext.model_validate_json(episode_context_path.read_text("utf-8"))
        context_used = True
        # Warn (don't block) when the context was analyzed from a different source than this one.
        context_stale = context.source_hash is not None and context.source_hash != source_digest(
            units
        )

    base_rules = config.default_rules(target)
    memory_rules = build_memory_rules(project_memory, context)
    memory_used = bool(project_memory.glossary or project_memory.memory.characters)

    def rules_for(lines):
        text = " ".join(line.text for line in lines)
        speakers = [line.speaker for line in lines]
        return base_rules + rules_for_text(memory_rules, text, speakers)

    jobs = build_jobs(units, target=target, rules_for=rules_for)
    translation_provider = provider_factory(
        provider,
        jobs_dir,
        model=model,
        reasoning=reasoning,
        max_retries=max_retries,
        timeout=timeout,
    )
    if provider in CLI_PROVIDERS:
        # Key the checkpoint on the model the runner will actually use, not the (possibly unset)
        # --model flag: when --model is omitted the runner falls back to its own default (e.g.
        # claude-opus-4-8), and a later change to that default must not silently reuse blocks
        # translated by the old one.
        effective_model = getattr(getattr(translation_provider, "runner", None), "model", None)
        signature = f"{provider}|{effective_model or model or ''}|{reasoning or ''}"
        checkpoint_file = jobs_dir.parent / CHECKPOINT_FILE
        checkpoint = (
            BlockCheckpoint.load(checkpoint_file, signature)
            if resume
            else BlockCheckpoint(path=checkpoint_file, signature=signature)
        )
        if parallel is None:
            parallel = _DEFAULT_API_PARALLEL if provider in _API_PROVIDERS else 1
        translations, untranslated_ids = translate_with_checkpoint(
            translation_provider,
            jobs,
            checkpoint=checkpoint,
            on_progress=on_progress,
            parallel=parallel,
        )
    else:
        translations = translation_provider.translate(jobs)
        untranslated_ids = list(getattr(translation_provider, "untranslated_ids", []))

    mapping_check = validate_translations(units, translations)
    if not mapping_check.ok:
        raise PipelineError("Invalid translation: " + "; ".join(mapping_check.errors))

    apply_translations(subs, units, translations)
    if fmt == "srt":
        # SRT has no positioning or drawing support: prune non-translatable events (drawings,
        # comments) so flatten_overlaps doesn't see empty cues from stripped override blocks.
        prune_to_units(subs, units)
        flatten_overlaps(subs)

    def validate_rendered(path: Path) -> ValidationResult:
        if fmt == "srt":
            return validate_file(path)
        # .ass output comes from these same units, so also assert each event kept its source
        # style and whole-line leading override block (position/colour/alignment).
        return validate_output_fn(path, units, check_fidelity=True)

    validation = atomic_save(subs, out_file, fmt=fmt, validate=validate_rendered)
    assert validation is not None
    return TranslateResult(
        source=source,
        output_path=out_file,
        n_units=len(units),
        n_jobs=len(jobs),
        output_validation=validation,
        context_used=context_used,
        memory_used=memory_used,
        untranslated_ids=untranslated_ids,
        context_stale=context_stale,
    )


def discover_inputs(
    directory: str | Path,
    *,
    globs: tuple[str, ...] = DEFAULT_BATCH_GLOBS,
    recursive: bool = False,
    target: str = config.DEFAULT_TARGET,
) -> list[Path]:
    """List sorted, deduplicated inputs while excluding outputs for the target language."""
    base = Path(directory)
    if not base.is_dir():
        raise PipelineError(f"Not a directory: {base}")
    target_code = lang_code(target)
    found: set[Path] = set()
    for pattern in globs:
        matches = base.rglob(pattern) if recursive else base.glob(pattern)
        for path in matches:
            if not path.is_file():
                continue
            stem_tail = path.stem.rpartition(".")[2].lower()
            if stem_tail != target_code:
                found.add(path)
    return sorted(found)


def batch_analyze(
    directory: str | Path,
    *,
    globs: tuple[str, ...] = DEFAULT_BATCH_GLOBS,
    recursive: bool = False,
    on_episode: Callable[[int, int, Path], None] | None = None,
    discover_inputs_fn=discover_inputs,
    analyze_fn,
    **analyze_kwargs,
) -> AnalyzeBatchResult:
    """Analyze matching inputs to build series memory, continuing past per-file failures.

    Meant to run before `batch_translate` so every episode contributes to the shared
    project memory (characters, glossary, style guide) before any translation begins.
    """
    target = analyze_kwargs.get("target", config.DEFAULT_TARGET)
    inputs = discover_inputs_fn(directory, globs=globs, recursive=recursive, target=target)
    result = AnalyzeBatchResult()
    total = len(inputs)
    for index, path in enumerate(inputs, start=1):
        if on_episode is not None:
            on_episode(index, total, path)
        try:
            analyze_fn(path, **analyze_kwargs)
        except AnalysisCurrentError:
            result.items.append(AnalyzeBatchItem(path, "skipped"))
        except ProviderError:
            raise  # systemic failure (quota, bad model, auth) — abort the batch
        except (PipelineError, *_EXPECTED_PIPELINE_ERRORS) as exc:
            result.items.append(AnalyzeBatchItem(path, "failed", error=str(exc)))
        else:
            result.items.append(AnalyzeBatchItem(path, "analyzed"))
    return result


def batch_translate(
    directory: str | Path,
    *,
    globs: tuple[str, ...] = DEFAULT_BATCH_GLOBS,
    recursive: bool = False,
    on_episode: Callable[[int, int, Path], None] | None = None,
    discover_inputs_fn=discover_inputs,
    translate_fn,
    **translate_kwargs,
) -> BatchResult:
    """Translate matching inputs independently, continuing after per-file failures."""
    target = translate_kwargs.get("target", config.DEFAULT_TARGET)
    inputs = discover_inputs_fn(directory, globs=globs, recursive=recursive, target=target)
    result = BatchResult()
    total = len(inputs)
    for index, path in enumerate(inputs, start=1):
        if on_episode is not None:
            on_episode(index, total, path)
        try:
            translated = translate_fn(path, **translate_kwargs)
        except OutputExistsError:
            result.items.append(BatchItem(path, "skipped", error=None))
        except ProviderError:
            raise  # systemic failure (quota, bad model, auth) — abort the batch
        except (PipelineError, *_EXPECTED_PIPELINE_ERRORS) as exc:
            result.items.append(BatchItem(path, "failed", error=str(exc)))
        else:
            result.items.append(
                BatchItem(
                    path,
                    "translated",
                    output_path=translated.output_path,
                    untranslated_ids=translated.untranslated_ids,
                )
            )
    return result
