"""Per-output provenance manifest for staleness detection in `batch`.

When `translate` writes an output it records, in the per-episode state directory, the source
fingerprint and the settings that produced it. On a later `batch` run that finds the output already
present, the stored manifest lets it tell an up-to-date output (skip) from one whose source,
provider/model, prompt or steering memory changed since (report as *stale*, never silently
overwritten). The source fingerprint (`output_source_digest`) covers timing and style, not just
text, so a re-timed or re-styled source — which leaves the existing output desynchronised — is
flagged stale too; `memory_hash` covers the series memory and episode context, so editing the
glossary or characters flags outputs whose *source* is unchanged.

The manifest is **per output artifact**, named by a hash of the output's *resolved path*
(`<hash>.manifest.json`), not one fixed name per episode. One episode can yield several artifacts —
an `.ass` and an `.srt`, or the same basename written to two different `--output`/`--out-dir`
destinations — and a single shared manifest would be overwritten by whichever ran last, masking the
staleness of the others (force-refreshing the `.ass` would mark the untouched `.srt` up to date).
Hashing the full path (not just the basename) keeps two same-named outputs in different directories
independent, and sidesteps the filesystem's per-name length limit; the readable path is stored
inside the manifest (`output`).

The recorded model is the one the runner will actually use: `translate` builds the provider (a
side-effect-free construction — no network, the litellm import is lazy) and reads its resolved
model, so with `--model` omitted the provider's built-in default (e.g. `claude-opus-4-8`) is
recorded rather than an empty string. That way a later change to a provider's default flags affected
outputs stale, instead of two runs both recording `""` and the change going unnoticed. Providers
with no model concept (`identity`, `file-handoff`) still record `""`. One consequence: a manifest
written before resolved-model recording landed (model `""`) is flagged stale once against a
now-known default on the next run — an honest "can't prove the old output used this model", not a
data loss.
Source changes — the common case after re-ripping or editing a subtitle — are always detected.
"""

from __future__ import annotations

import hashlib
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict

from translate_subs.ai.provider import TRANSLATION_PROMPT_VERSION
from translate_subs.fsutil import atomic_write_text
from translate_subs.workflows.support import episode_dir

_MANIFEST_SUFFIX = ".manifest.json"


def tool_version() -> str:
    """Installed llm-subs version, or "" when running from an uninstalled source tree."""
    try:
        return version("llm-subs")
    except PackageNotFoundError:
        return ""


def file_digest(path: Path) -> str:
    """Content fingerprint of a written output file, to detect later hand-edits."""
    return hashlib.sha256(path.read_bytes()).hexdigest()[:16]


class OutputManifest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: Literal[1] = 1
    source_hash: str
    target: str
    provider: str
    model: str
    # Reasoning effort steers the output (e.g. Codex), so a change should flag the output stale.
    # Defaults to "" so a manifest written before this field loads as "no reasoning recorded".
    reasoning: str = ""
    prompt_version: int = TRANSLATION_PROMPT_VERSION
    # Fingerprint of the series memory + episode context that steered the prompts. A glossary,
    # character or style-guide edit changes the translation but not the source, so without this the
    # output would be skipped as up to date. Defaults to "" so a manifest written before this field
    # loads as "no memory recorded" and is not spuriously flagged (see `_changes`).
    memory_hash: str = ""
    # Self-description of the artifact this manifest tracks. Not part of the staleness comparison —
    # a different format/filename is a *separate* artifact with its own manifest, never a stale one.
    # Empty on manifests written before these fields existed.
    fmt: str = ""
    output: str = ""
    # The llm-subs version that wrote the output (provenance only; not a staleness signal — a bump
    # must not mass-flag old outputs, `prompt_version` already covers prompt changes).
    tool_version: str = ""
    # Content hash of the output *as written*. Lets a later run notice the file was edited by hand
    # since generation and refuse to clobber it (a separate axis from input staleness). Empty on
    # legacy manifests and until the first write records it.
    output_hash: str = ""


def manifest_path(project: str, target: str, episode: str, output_path: Path) -> Path:
    """State-dir manifest for one output artifact, named by a hash of its resolved path.

    Keyed on the full resolved output path (not its basename) so two outputs sharing a filename in
    different directories keep separate manifests, and a long output name can't overflow the
    filesystem's per-name limit. The readable path is stored inside the manifest (`output`).
    """
    digest = hashlib.sha256(str(output_path.resolve()).encode("utf-8")).hexdigest()[:16]
    return episode_dir(project, target, episode) / f"{digest}{_MANIFEST_SUFFIX}"


def load_manifest(path: Path) -> OutputManifest | None:
    """The stored manifest, or None when absent or unreadable.

    A missing/legacy/corrupt manifest is treated as absent so an output that predates this feature
    is skipped as before rather than wrongly flagged stale.
    """
    if not path.exists():
        return None
    try:
        return OutputManifest.model_validate_json(path.read_text("utf-8"))
    except Exception:
        return None


def write_manifest(path: Path, manifest: OutputManifest) -> None:
    atomic_write_text(path, manifest.model_dump_json(indent=2), private=True)


def refresh_output_manifest(project: str, target: str, output_path: Path) -> None:
    """Re-record an output's content hash after *our own* `--apply` tools rewrite it.

    `review --apply`/`tighten --apply` legitimately modify a translated file; without this the next
    `translate` run would see the changed hash and wrongly report it as hand-edited. This refreshes
    the tracking manifest (found by matching the recorded `output` path) so only edits made
    *outside* the tool are flagged. No-op when nothing tracks this output (produced elsewhere).
    """
    from translate_subs.workflows.support import memory_root

    root = memory_root(project, target)
    if not root.exists():
        return
    wanted = str(output_path.resolve())
    for mpath in root.glob(f"*/*{_MANIFEST_SUFFIX}"):
        manifest = load_manifest(mpath)
        if manifest is not None and manifest.output == wanted:
            manifest.output_hash = file_digest(output_path)
            write_manifest(mpath, manifest)
            return


def _changes(stored: OutputManifest, current: OutputManifest) -> list[str]:
    changed = []
    if stored.source_hash != current.source_hash:
        changed.append("source")
    if stored.provider != current.provider or stored.model != current.model:
        changed.append("provider/model")
    if stored.reasoning != current.reasoning:
        changed.append("reasoning")
    if stored.prompt_version != current.prompt_version:
        changed.append("prompt")
    # Legacy tolerance: a field the stored manifest never recorded (empty on an older release's
    # manifest) is not a change, so pre-existing outputs aren't all flagged the moment the field is
    # introduced. `current.memory_hash` is always populated, so only the stored side needs guarding.
    if stored.memory_hash and stored.memory_hash != current.memory_hash:
        changed.append("memory")
    return changed


def is_stale(stored: OutputManifest, current: OutputManifest) -> bool:
    """Whether the stored manifest differs from the current one in a way that dates the output."""
    return bool(_changes(stored, current))


def describe_change(stored: OutputManifest, current: OutputManifest) -> str:
    """Human-readable list of what changed between a stored and a current manifest."""
    return ", ".join(_changes(stored, current)) or "settings"
