"""Single-file and batch translation workflows."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any

from translate_subs import config
from translate_subs.ai.analysis import EpisodeContext, output_source_digest, source_digest
from translate_subs.ai.blocks import build_jobs
from translate_subs.ai.checkpoint import (
    CHECKPOINT_FILE,
    BlockCheckpoint,
    BlockProgress,
    translate_with_checkpoint,
)
from translate_subs.ai.cli_adapters import CLI_PROVIDERS
from translate_subs.ai.job_protocol import JobLine, TranslationJobIn
from translate_subs.ai.provider import (
    ProviderError,
    TranslationProvider,
    is_per_episode_failure,
)
from translate_subs.domain.models import TranslatableUnit
from translate_subs.io.media_probe import MediaToolError
from translate_subs.io.source_resolver import ResolvedSource, SourceError
from translate_subs.memory.rules import (
    build_memory_rules,
    memory_prompt_digest,
    rules_for_text,
)
from translate_subs.memory.store import ProjectMemory
from translate_subs.naming import (
    DEFAULT_FORMAT,
    SUPPORTED_FORMATS,
    lang_code,
    output_path,
    validate_target,
)
from translate_subs.subs import document
from translate_subs.subs.extractor import ass_fidelity_lines, extract_units
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
    AnalyzeResult,
    BatchItem,
    BatchResult,
    ModifiedOutputError,
    OutputExistsError,
    PipelineError,
    StaleOutputError,
    TranslateResult,
)
from translate_subs.workflows.output_manifest import (
    OutputManifest,
    describe_change,
    file_digest,
    is_stale,
    load_manifest,
    manifest_path,
    tool_version,
    write_manifest,
)
from translate_subs.workflows.seams import (
    DiscoverInputsFn,
    ProviderFactory,
    ResolveSourceFn,
    ValidateOutputFn,
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
# Per-episode batch seams: `batch_*` forwards an arbitrary `**kwargs` to these, so they stay loose
# `Callable[..., X]` (a fixed Protocol signature would reject the concrete facade functions). The
# construction seams below are typed precisely as Protocols (see `workflows/seams.py`).
TranslateFn = Callable[..., TranslateResult]
AnalyzeFn = Callable[..., AnalyzeResult]


def _same_path(a: str | Path, b: str | Path) -> bool:
    """True if two paths point at the same location (resolved, even if not yet created)."""
    return Path(a).resolve() == Path(b).resolve()


def _resolve_output_file(
    source: ResolvedSource,
    *,
    output: str | Path | None,
    out_dir: str | Path | None,
    fmt: str,
    target: str,
) -> Path:
    """Decide where the translation is written, refusing escapes and source overwrites."""
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
    return out_file


def _load_episode_context(
    project_name: str,
    target: str,
    episode_name: str,
    *,
    use_context: bool,
    units: list[TranslatableUnit],
) -> tuple[EpisodeContext | None, bool, bool]:
    """Load episode.context.json if wanted and present; returns (context, used, stale)."""
    if not use_context:
        return None, False, False
    episode_context_path = context_path(project_name, target, episode_name)
    if not episode_context_path.exists():
        return None, False, False
    context = EpisodeContext.model_validate_json(episode_context_path.read_text("utf-8"))
    # Stale means the context was analyzed from a different source than this one: warn, not block.
    stale = context.source_hash is not None and context.source_hash != source_digest(units)
    return context, True, stale


def _ensure_output_writable(
    out_file: Path, out_manifest: OutputManifest, out_manifest_path: Path, *, force: bool
) -> None:
    """Raise the matching skip/stale/modified error when `out_file` must not be rewritten."""
    if not out_file.exists() or force:
        return
    stored = load_manifest(out_manifest_path)
    if stored is None and out_manifest_path.exists():
        # The manifest file exists but is unreadable (corrupt/tampered). We can't confirm the
        # output is current, so surface it rather than silently skipping it as up to date.
        raise StaleOutputError(
            f"Output manifest is unreadable ({out_manifest_path}); cannot verify "
            f"{out_file} is up to date. Use --force to regenerate it."
        )
    if stored is not None:
        # Protect hand-edits first: if the file changed since we wrote it, never overwrite it
        # on our own, even if the source also changed.
        if stored.output_hash and file_digest(out_file) != stored.output_hash:
            raise ModifiedOutputError(
                f"Output was edited since it was generated: {out_file}. "
                "Use --force to overwrite your changes."
            )
        if is_stale(stored, out_manifest):
            raise StaleOutputError(
                f"Output is stale ({describe_change(stored, out_manifest)} changed since it "
                f"was written): {out_file}. Use --force to retranslate."
            )
    raise OutputExistsError(f"Output already exists: {out_file}. Use --force to overwrite.")


def _run_provider(
    translation_provider: TranslationProvider,
    jobs: list[TranslationJobIn],
    *,
    provider: str,
    signature: str,
    checkpoint_file: Path,
    resume: bool,
    parallel: int | None,
    on_progress: Callable[[BlockProgress], None] | None,
) -> tuple[dict[str, str], list[str]]:
    """Run the translation jobs; the slow CLI/API backends go through the block checkpoint.

    The checkpoint is keyed on `signature` (provider|resolved model|reasoning) so a later change
    to a provider's built-in default doesn't silently reuse old blocks.
    """
    if provider in CLI_PROVIDERS:
        checkpoint = (
            BlockCheckpoint.load(checkpoint_file, signature)
            if resume
            else BlockCheckpoint(path=checkpoint_file, signature=signature)
        )
        if parallel is None:
            parallel = _DEFAULT_API_PARALLEL if provider in _API_PROVIDERS else 1
        return translate_with_checkpoint(
            translation_provider,
            jobs,
            checkpoint=checkpoint,
            on_progress=on_progress,
            parallel=parallel,
        )
    translations = translation_provider.translate(jobs)
    return translations, list(getattr(translation_provider, "untranslated_ids", []))


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
    encoding: str | None = None,
    model: str | None = None,
    reasoning: str | None = None,
    max_retries: int = 2,
    force: bool = False,
    strict_lang: bool = False,
    resume: bool = True,
    parallel: int | None = None,
    timeout: int | None = None,
    dry_run: bool = False,
    on_progress: Callable[[BlockProgress], None] | None = None,
    resolve_source_fn: ResolveSourceFn,
    provider_factory: ProviderFactory,
    validate_output_fn: ValidateOutputFn,
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

    subs = document.load(source.subtitle_path, encoding=encoding, lang_hint=lang)
    units = extract_units(subs)
    if not units:
        raise PipelineError("No translatable lines found in the subtitle.")

    out_file = _resolve_output_file(source, output=output, out_dir=out_dir, fmt=fmt, target=target)
    project_name, episode_name = project_episode(source, project)

    # Load the steering state before the staleness check so the manifest can fingerprint it: a
    # glossary/character/context edit changes the translation without touching the source.
    project_memory = ProjectMemory.load(memory_root(project_name, target))
    context, context_used, context_stale = _load_episode_context(
        project_name, target, episode_name, use_context=use_context, units=units
    )

    base_rules = config.default_rules(target)
    memory_rules = build_memory_rules(project_memory, context)
    memory_used = bool(project_memory.glossary or project_memory.memory.characters)

    jobs_dir = episode_dir(project_name, target, episode_name) / "jobs"
    translation_provider = provider_factory(
        provider,
        jobs_dir,
        model=model,
        reasoning=reasoning,
        max_retries=max_retries,
        timeout=timeout,
    )
    # Record the model the runner will actually use, not the (possibly unset) --model flag: with
    # --model omitted the runner falls back to its own default (e.g. claude-opus-4-8). Storing the
    # resolved model lets a later run notice that default changing — otherwise both runs record an
    # empty model and the change is invisible. Construction is side-effect-free (no network, the
    # litellm import is lazy), so building the provider before the existence check is safe even when
    # the run turns out to skip.
    effective_model = getattr(getattr(translation_provider, "runner", None), "model", None)

    # For .ass the output preserves far more than the translatable units: style definitions, each
    # event's layout metadata (layer/margins/effect), and non-translatable events (drawings,
    # comments) copied verbatim. Fold all of that into the fingerprint so a re-style or a
    # drawing/margin edit flags the output stale even though no translated line changed. .srt is
    # flat and prunes those, so it contributes nothing and the hash stays units-only. `subs` here is
    # still the untouched source (translations/pruning happen later).
    ass_extra = [] if fmt == "srt" else ass_fidelity_lines(subs)
    out_manifest = OutputManifest(
        source_hash=output_source_digest(units, extra_lines=ass_extra),
        target=target,
        provider=provider,
        model=effective_model or model or "",
        reasoning=reasoning or "",
        memory_hash=memory_prompt_digest(memory_rules),
        fmt=fmt,
        output=str(out_file.resolve()),
    )
    out_manifest_path = manifest_path(project_name, target, episode_name, out_file)
    _ensure_output_writable(out_file, out_manifest, out_manifest_path, force=force)

    def rules_for(lines: list[JobLine]) -> list[str]:
        text = " ".join(line.text for line in lines)
        speakers = [line.speaker for line in lines]
        return base_rules + rules_for_text(memory_rules, text, speakers)

    jobs = build_jobs(units, target=target, rules_for=rules_for)
    if dry_run:
        # Preview stops here, before any provider call: no LLM runs and no output, manifest or
        # checkpoint is written. Everything above already made the real run's decisions — the
        # source was resolved (an embedded track may land in the extraction cache), the effective
        # model recorded, and `_ensure_output_writable` raised the same skip/stale/modified errors
        # a real run would — so the preview cannot drift from what an actual run does.
        return TranslateResult(
            source=source,
            output_path=out_file,
            n_units=len(units),
            n_jobs=len(jobs),
            output_validation=ValidationResult(ok=True),
            context_used=context_used,
            memory_used=memory_used,
            context_stale=context_stale,
        )
    translations, untranslated_ids = _run_provider(
        translation_provider,
        jobs,
        provider=provider,
        signature=f"{provider}|{effective_model or model or ''}|{reasoning or ''}",
        checkpoint_file=jobs_dir.parent / CHECKPOINT_FILE,
        resume=resume,
        parallel=parallel,
        on_progress=on_progress,
    )

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
    if validation is None:  # atomic_save returns a result whenever a validator is passed
        raise PipelineError("Internal error: the rendered output was not validated.")
    # Record what produced this output (for staleness) plus the tool version and the file's own
    # hash (for the "edited since generated" check on a later run).
    out_manifest.tool_version = tool_version()
    out_manifest.output_hash = file_digest(out_file)
    write_manifest(out_manifest_path, out_manifest)
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
    discover_inputs_fn: DiscoverInputsFn = discover_inputs,
    analyze_fn: AnalyzeFn,
    **analyze_kwargs: Any,
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
        except ProviderError as exc:
            # A content/protocol fault (unparseable reply) is local to this episode: record it and
            # continue. Auth/config/quota/service (or an unclassified error) is systemic — abort.
            if not is_per_episode_failure(exc):
                raise
            result.items.append(AnalyzeBatchItem(path, "failed", error=str(exc)))
        except (PipelineError, *_EXPECTED_PIPELINE_ERRORS) as exc:
            result.items.append(AnalyzeBatchItem(path, "failed", error=str(exc)))
        else:
            result.items.append(AnalyzeBatchItem(path, "analyzed"))
    return result


def _success_item(path: Path, translated: TranslateResult, *, dry_run: bool) -> BatchItem:
    """The batch record for an episode that translated — or, under dry_run, would translate."""
    if dry_run:
        return BatchItem(
            path,
            "planned",
            output_path=translated.output_path,
            n_units=translated.n_units,
            n_jobs=translated.n_jobs,
        )
    return BatchItem(
        path,
        "translated",
        output_path=translated.output_path,
        untranslated_ids=translated.untranslated_ids,
    )


def batch_translate(
    directory: str | Path,
    *,
    globs: tuple[str, ...] = DEFAULT_BATCH_GLOBS,
    recursive: bool = False,
    on_episode: Callable[[int, int, Path], None] | None = None,
    discover_inputs_fn: DiscoverInputsFn = discover_inputs,
    translate_fn: TranslateFn,
    **translate_kwargs: Any,
) -> BatchResult:
    """Translate matching inputs independently, continuing after per-file failures."""
    target = translate_kwargs.get("target", config.DEFAULT_TARGET)
    inputs = discover_inputs_fn(directory, globs=globs, recursive=recursive, target=target)
    out_dir = translate_kwargs.get("out_dir")
    dry_run = bool(translate_kwargs.get("dry_run"))
    base_resolved = Path(directory).resolve()
    result = BatchResult()
    total = len(inputs)
    for index, path in enumerate(inputs, start=1):
        if on_episode is not None:
            on_episode(index, total, path)
        try:
            kwargs = translate_kwargs
            if out_dir is not None:
                # Mirror each input's sub-directory under out_dir so same-named episodes in
                # different folders (Season 1/Episode 01 vs Season 2/Episode 01) don't both collapse
                # onto one flat <out-dir>/Episode 01.<lang>.<fmt> and overwrite each other.
                try:
                    subdir = path.parent.resolve().relative_to(base_resolved)
                except ValueError:
                    subdir = Path()
                kwargs = {**translate_kwargs, "out_dir": Path(out_dir) / subdir}
            translated = translate_fn(path, **kwargs)
        except OutputExistsError:
            result.items.append(BatchItem(path, "skipped", error=None))
        except StaleOutputError as exc:
            # Source/model/prompt changed since this output was written: warn, never overwrite.
            result.items.append(BatchItem(path, "stale", error=str(exc)))
        except ModifiedOutputError as exc:
            # The output was hand-edited since we wrote it: warn, never overwrite without --force.
            result.items.append(BatchItem(path, "modified", error=str(exc)))
        except ProviderError as exc:
            # A content/protocol fault (unparseable reply, wrong ids) is local to this episode:
            # record it and move on. Auth/config/quota/service (or an unclassified error) is
            # systemic — retrying the whole season would repeat it — so abort.
            if not is_per_episode_failure(exc):
                raise
            result.items.append(BatchItem(path, "failed", error=str(exc)))
        except (PipelineError, *_EXPECTED_PIPELINE_ERRORS) as exc:
            result.items.append(BatchItem(path, "failed", error=str(exc)))
        else:
            result.items.append(_success_item(path, translated, dry_run=dry_run))
    return result
