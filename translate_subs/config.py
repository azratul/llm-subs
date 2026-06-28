"""Constants and working paths.

Paths default to the user's standard data/cache locations so the tool works from any
directory once installed (not just from a checkout). Override the whole data root with
``$LLM_SUBS_HOME``; the legacy ``$TRANSLATE_SUBS_HOME`` name remains supported.
Otherwise the XDG base-directory variables are honoured. Existing ``translate-subs``
data/cache directories are reused when the canonical ``llm-subs`` directory is absent.
"""

from __future__ import annotations

import os
from pathlib import Path

APP_DIR_NAME = "llm-subs"
LEGACY_APP_DIR_NAME = "translate-subs"


def _existing_or_canonical_root(base: str) -> Path:
    """Use the canonical app directory, with a non-destructive legacy fallback."""
    root = Path(base).expanduser()
    canonical = root / APP_DIR_NAME
    legacy = root / LEGACY_APP_DIR_NAME
    if not canonical.exists() and legacy.exists():
        return legacy
    return canonical


def _data_root() -> Path:
    override = os.environ.get("LLM_SUBS_HOME") or os.environ.get("TRANSLATE_SUBS_HOME")
    if override:
        return Path(override).expanduser()
    base = os.environ.get("XDG_DATA_HOME") or "~/.local/share"
    return _existing_or_canonical_root(base)


def _cache_root() -> Path:
    base = os.environ.get("XDG_CACHE_HOME") or "~/.cache"
    return _existing_or_canonical_root(base)


DATA_DIR = _data_root()
OUTPUT_DIR = DATA_DIR / "output"  # only a sandbox default; real output goes next to the input
PROJECTS_DIR = DATA_DIR / "projects"  # per-series memory; must persist across runs/dirs
WORK_DIR = _cache_root() / "work"  # subtitles extracted from containers (ephemeral)

# Default target when none is given (kept for the common anime EN->ES case).
DEFAULT_TARGET = "es-latam"


def default_rules(target: str) -> list[str]:
    """Language-agnostic base rules; enriched with series memory/context elsewhere."""
    return [
        f"Translate into {target}. Use the natural, standard register of that language/variant.",
        "Keep proper names; keep honorifics if the source uses them.",
        "Prefer natural, concise subtitles; avoid literal translation.",
        "Translate only the visible text, never tags or markup.",
    ]
