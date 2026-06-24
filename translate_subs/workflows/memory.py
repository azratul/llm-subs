"""Episode analysis and persistent project-memory workflows."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

from translate_subs import config
from translate_subs.ai.analysis import (
    TRANSCRIPT_LIMIT,
    EpisodeContext,
    analyze_episode,
    source_digest,
)
from translate_subs.memory.compact import (
    compact_project_memory,
    detect_character_aliases,
    merge_alias,
)
from translate_subs.memory.merge import (
    ConflictPolicy,
    ConflictResolver,
    MergeReport,
    merge_episode_context,
)
from translate_subs.memory.models import normalize_gender
from translate_subs.memory.store import ProjectMemory, atomic_write_text
from translate_subs.naming import validate_target
from translate_subs.subs import document
from translate_subs.subs.extractor import extract_units
from translate_subs.workflows.models import (
    AnalysisCurrentError,
    AnalyzeResult,
    CompactMemoryResult,
    ConflictPrompt,
    PipelineError,
    ResolveConflictsResult,
    UpdateMemoryResult,
)
from translate_subs.workflows.support import (
    context_path,
    memory_root,
    project_episode,
)


def prior_known(project_memory: ProjectMemory) -> str | None:
    """Render one known fact per line to keep later analysis consistent."""
    lines: list[str] = []
    for character in project_memory.memory.characters:
        if character.gender in ("male", "female"):
            lines.append(f"- {character.name}: {character.gender}")
    for source, target in project_memory.glossary.items():
        lines.append(f"- glossary: {source} -> {target}")
    return "\n".join(lines) if lines else None


def merge_into_memory(
    project_name: str,
    context: EpisodeContext,
    *,
    target: str,
    policy: ConflictPolicy,
    resolver: ConflictResolver | None,
) -> MergeReport:
    project_memory = ProjectMemory.load(memory_root(project_name, target))
    report = merge_episode_context(
        project_memory.memory,
        project_memory.glossary,
        context,
        policy=policy,
        resolver=resolver,
    )
    project_memory.save()
    project_memory.append_conflicts([conflict.model_dump() for conflict in report.conflicts])
    return report


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
    skip_if_current: bool = False,
    runner=None,
    resolve_source_fn,
    ai_runner_factory,
) -> AnalyzeResult:
    try:
        validate_target(target)
    except ValueError as exc:
        raise PipelineError(str(exc)) from exc
    source = resolve_source_fn(
        input_path,
        work_dir=config.WORK_DIR,
        lang=lang,
        track_index=track_index,
        interactive=interactive,
    )
    units = extract_units(document.load(source.subtitle_path))
    if not units:
        raise PipelineError("No translatable lines found in the subtitle.")

    project_name, episode_name = project_episode(source, project)

    if skip_if_current:
        ep_ctx_path = context_path(project_name, target, episode_name)
        if ep_ctx_path.exists():
            existing = EpisodeContext.model_validate_json(ep_ctx_path.read_text("utf-8"))
            if existing.source_hash and existing.source_hash == source_digest(units):
                raise AnalysisCurrentError(f"Context already current: {ep_ctx_path}")

    project_memory = ProjectMemory.load(memory_root(project_name, target))
    context = analyze_episode(
        units,
        target=target,
        runner=runner or ai_runner_factory(provider, model=model, reasoning=reasoning),
        prior_known=prior_known(project_memory),
        max_retries=max_retries,
    )
    # Record the source fingerprint so later runs can detect a changed subtitle.
    context.source_hash = source_digest(units)

    out_path = context_path(project_name, target, episode_name)
    atomic_write_text(out_path, context.model_dump_json(indent=2))
    report = merge_into_memory(
        project_name,
        context,
        target=target,
        policy=on_conflict,
        resolver=conflict_resolver,
    )
    return AnalyzeResult(
        source=source,
        context_path=out_path,
        context=context,
        n_units=len(units),
        merge=report,
        truncated_lines=max(0, len(units) - TRANSCRIPT_LIMIT),
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
    resolve_source_fn,
) -> UpdateMemoryResult:
    source = resolve_source_fn(
        input_path,
        work_dir=config.WORK_DIR,
        lang=lang,
        track_index=track_index,
        interactive=interactive,
    )
    project_name, episode_name = project_episode(source, project)
    context_file = context_path(project_name, target, episode_name)
    if not context_file.exists():
        raise PipelineError(f"No episode context at {context_file}. Run `analyze` first.")
    context = EpisodeContext.model_validate_json(context_file.read_text("utf-8"))
    report = merge_into_memory(
        project_name,
        context,
        target=target,
        policy=on_conflict,
        resolver=conflict_resolver,
    )
    return UpdateMemoryResult(
        project_dir=memory_root(project_name, target),
        context_path=context_file,
        merge=report,
    )


def compact_memory(
    project: str,
    target: str = "es-latam",
    *,
    provider: str | None = None,
    model: str | None = None,
    reasoning: str | None = None,
    max_retries: int = 2,
    alias_confirm: Callable[..., str] | None = None,
    ai_runner_factory=None,
) -> CompactMemoryResult:
    project_path = memory_root(project, target)
    if not project_path.exists():
        raise PipelineError(f"No memory at {project_path}. Run `analyze` first.")
    project_memory = ProjectMemory.load(project_path)
    report = compact_project_memory(project_memory)

    if provider is not None and ai_runner_factory is not None:
        from translate_subs.ai.provider import retry_provider_call

        runner = ai_runner_factory(provider, model=model, reasoning=reasoning)
        aliases = retry_provider_call(
            lambda: detect_character_aliases(runner, project_memory.memory.characters),
            max_retries=max_retries,
            label="Alias detection",
        )
        for match in aliases:
            if alias_confirm is not None:
                choice = alias_confirm(match)
                if choice == "skip":
                    continue
            if merge_alias(project_memory, match.canonical, match.alias):
                report.merged_aliases.append(match)

    project_memory.save()
    return CompactMemoryResult(project_dir=project_path, report=report)


def _apply_conflict_choice(project_memory: ProjectMemory, conflict: dict) -> bool:
    kind = conflict.get("kind")
    key = conflict.get("key", "")
    suggested = conflict.get("suggested", "")
    if kind == "glossary":
        project_memory.glossary[key] = suggested
        return True
    if kind == "gender":
        character = project_memory.memory.find(key)
        if character is not None:
            character.gender = normalize_gender(suggested)
            return True
    return False


def resolve_conflicts(
    project: str, prompt: ConflictPrompt, target: str = "es-latam"
) -> ResolveConflictsResult:
    project_path = memory_root(project, target)
    if not project_path.exists():
        raise PipelineError(f"No memory at {project_path}. Run `analyze` first.")
    project_memory = ProjectMemory.load(project_path)
    conflicts = project_memory.load_conflicts()
    if not conflicts:
        return ResolveConflictsResult(project_dir=project_path, resolved=0, remaining=0)

    remaining: list[dict] = []
    resolved = 0
    changed = False
    for conflict in conflicts:
        choice = prompt(conflict)
        if choice == "skip":
            remaining.append(conflict)
            continue
        if choice == "use" and _apply_conflict_choice(project_memory, conflict):
            changed = True
        resolved += 1

    if changed:
        project_memory.save()
    project_memory.write_conflicts(remaining)
    return ResolveConflictsResult(
        project_dir=project_path,
        resolved=resolved,
        remaining=len(remaining),
    )
