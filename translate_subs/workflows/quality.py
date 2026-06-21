"""Review, readability and validation workflows."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

from translate_subs import config
from translate_subs.ai.analysis import EpisodeContext, source_digest
from translate_subs.memory.store import ProjectMemory, atomic_write_text
from translate_subs.naming import base_stem
from translate_subs.readability.compactor import FlaggedLine, compact_lines
from translate_subs.readability.metrics import (
    ReadabilityLimits,
    exceeds,
    is_safe_improvement,
    measure,
)
from translate_subs.readability.report import ReadabilityEntry
from translate_subs.readability.report import render_markdown as render_readability_md
from translate_subs.review.checks import DEFAULT_MAX_CHARS, run_deterministic_checks
from translate_subs.review.models import ReviewReport
from translate_subs.review.report import render_markdown
from translate_subs.review.reviewer import review_lines
from translate_subs.review.structure import ALIGN_TOLERANCE_MS, pair_lines
from translate_subs.subs import document
from translate_subs.subs.extractor import extract_units
from translate_subs.subs.reinserter import replace_visible_text
from translate_subs.subs.validator import ValidationResult, validate_file, validate_output
from translate_subs.workflows.models import PipelineError, ReviewResult, TightenResult
from translate_subs.workflows.support import (
    atomic_save,
    context_path,
    memory_root,
    project_episode,
    readability_path,
    review_path,
)

RunnerFactory = Callable[..., Callable[[str], str]]


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
    resolve_source_fn,
    ai_runner_factory: RunnerFactory,
) -> ReviewResult:
    source = resolve_source_fn(
        input_path,
        work_dir=config.WORK_DIR,
        lang=lang,
        track_index=track_index,
        interactive=interactive,
    )
    source_subs = document.load(source.subtitle_path)
    units = extract_units(source_subs)
    if not units:
        raise PipelineError("No translatable lines found in the source.")

    translated_path = Path(translated_path)
    if not translated_path.exists():
        raise PipelineError(f"Translated file not found: {translated_path}")
    target_subs = document.load(translated_path)

    compare_styles = source.subtitle_path.suffix.lower() in (
        ".ass",
        ".ssa",
    ) and translated_path.suffix.lower() in (".ass", ".ssa")
    lines, structural = pair_lines(
        units,
        target_subs,
        source_subs=source_subs,
        compare_styles=compare_styles,
    )

    project_name, episode_name = project_episode(source, project)
    pm = ProjectMemory.load(memory_root(project_name, target))
    glossary = dict(pm.glossary)
    context_stale = False
    ctx_file = context_path(project_name, target, episode_name)
    if ctx_file.exists():
        ctx = EpisodeContext.model_validate_json(ctx_file.read_text("utf-8"))
        for term, rendering in ctx.glossary.items():
            glossary.setdefault(term, rendering)
        context_stale = ctx.source_hash is not None and ctx.source_hash != source_digest(units)
    confirmed: dict[str, str] = {
        character.name: character.gender
        for character in pm.memory.characters
        if character.gender in ("male", "female")
    }
    names = [character.name for character in pm.memory.characters]

    findings = structural + run_deterministic_checks(
        lines,
        glossary=glossary,
        names=names,
        max_chars=max_chars,
    )
    if use_llm and lines:
        findings += review_lines(
            lines,
            glossary=glossary,
            genders=confirmed,
            target=target,
            source_lang=lang,
            runner=runner or ai_runner_factory(provider, model=model, reasoning=reasoning),
            max_retries=max_retries,
        )

    report = ReviewReport(episode=episode_name, findings=findings)
    out_path = review_path(project_name, target, episode_name)
    atomic_write_text(out_path, render_markdown(report))

    aligned = (
        len(target_subs.events) == len(units)
        and len({unit.id for unit in units}) == len(units)
        and all(
            abs(unit.start - event.start) <= ALIGN_TOLERANCE_MS
            and abs(unit.end - event.end) <= ALIGN_TOLERANCE_MS
            for unit, event in zip(units, target_subs.events, strict=False)
        )
    )
    n_applied = 0
    auto_fixes = report.auto_fixes()
    if apply and auto_fixes and aligned:
        index_by_id = {line.id: line.event_index for line in lines}
        for fix in auto_fixes:
            index = index_by_id.get(fix.id or "")
            if index is not None and fix.suggested is not None:
                replace_visible_text(target_subs.events[index], fix.suggested)
                n_applied += 1
        if n_applied:
            atomic_save(
                target_subs,
                translated_path,
                validate=lambda path: validate_output(path, units),
            )

    return ReviewResult(
        report=report,
        report_path=out_path,
        translated_path=translated_path,
        n_lines=len(lines),
        n_applied=n_applied,
        mapping_aligned=aligned,
        context_stale=context_stale,
    )


def tighten_subtitle(
    translated_path: str | Path,
    *,
    project: str | None = None,
    limits: ReadabilityLimits | None = None,
    use_llm: bool = True,
    apply: bool = False,
    provider: str = "claude",
    model: str | None = None,
    reasoning: str | None = None,
    max_retries: int = 2,
    runner=None,
    ai_runner_factory: RunnerFactory,
) -> TightenResult:
    limits = limits or ReadabilityLimits()
    translated_path = Path(translated_path)
    if not translated_path.exists():
        raise PipelineError(f"Translated file not found: {translated_path}")
    subs = document.load(translated_path)

    flagged: list[FlaggedLine] = []
    for index, event in enumerate(subs.events):
        metrics = measure(event.plaintext, event.start, event.end)
        reasons = exceeds(metrics, limits)
        if reasons:
            flagged.append(
                FlaggedLine(
                    id=f"{index + 1:04d}",
                    event_index=index,
                    text=event.plaintext,
                    metrics=metrics,
                    reasons=reasons,
                )
            )

    compactions: dict[str, str] = {}
    if use_llm and flagged:
        compactions = compact_lines(
            flagged,
            limits=limits,
            runner=runner or ai_runner_factory(provider, model=model, reasoning=reasoning),
            max_retries=max_retries,
        )

    entries: list[ReadabilityEntry] = []
    n_applied = 0
    n_residual = 0
    for line in flagged:
        compact = compactions.get(line.id)
        residual: list[str] = []
        rejected = False
        if compact is not None:
            event = subs.events[line.event_index]
            new_metrics = measure(compact, event.start, event.end)
            residual = exceeds(new_metrics, limits)
            # Only write a compaction that actually helps: a candidate that adds a new kind of
            # violation or grows the text is kept out of the file (reported, not applied).
            improved = is_safe_improvement(line.metrics, new_metrics, limits)
            rejected = not improved
            if apply and improved:
                replace_visible_text(event, compact)
                n_applied += 1
            if residual and improved:
                n_residual += 1
        entries.append(
            ReadabilityEntry(
                id=line.id,
                reasons=line.reasons,
                current=line.text,
                compact=compact,
                residual=residual,
                rejected=rejected,
            )
        )

    if apply and n_applied:
        atomic_save(subs, translated_path, validate=validate_file)

    project_name = project or translated_path.parent.name or "default"
    episode_name = base_stem(translated_path)
    # tighten has no --target; infer it from the file's own language suffix (e.g. ep.es.srt -> es)
    # so the report lands beside the rest of that language's state, else fall back to the default.
    full_stem = translated_path.stem
    target = full_stem[len(episode_name) :].lstrip(".") if full_stem != episode_name else None
    out_path = readability_path(project_name, target or config.DEFAULT_TARGET, episode_name)
    atomic_write_text(out_path, render_readability_md(episode_name, entries))

    return TightenResult(
        report_path=out_path,
        translated_path=translated_path,
        n_subs=len(subs.events),
        n_flagged=len(flagged),
        n_compacted=len(compactions),
        n_applied=n_applied,
        n_residual=n_residual,
    )


def validate_subtitle(path: str | Path) -> ValidationResult:
    path = Path(path)
    if not path.exists():
        raise PipelineError(f"File not found: {path}")
    return validate_file(path)
