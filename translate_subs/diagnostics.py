"""Environment diagnostics for the `doctor` command.

Each check returns a `Check` (name, status, detail). It does not call an LLM; beyond ensuring
its own data/cache directories exist (owner-only), it only inspects what the tool needs to run:
the external media tools, the writable data/cache directories, whether any private state is
group/other-readable, and — when a provider is named — that provider's backend (a CLI on PATH,
a reachable Ollama server, or the optional litellm package).
"""

from __future__ import annotations

import json
import os
import shutil
import sys
import urllib.error
import urllib.request
from dataclasses import dataclass
from importlib.metadata import PackageNotFoundError
from importlib.metadata import version as _pkg_version
from pathlib import Path
from typing import Literal

from translate_subs import config
from translate_subs.fsutil import ensure_private_dir

Status = Literal["ok", "warn", "fail"]

# Providers whose backend is a CLI on PATH, mapped to that CLI's binary name (usually the same,
# but `antigravity` ships as `agy`).
_CLI_BINARIES = {
    "claude": "claude",
    "codex": "codex",
    "antigravity": "agy",
    "opencode": "opencode",
}


@dataclass
class Check:
    name: str
    status: Status
    detail: str


def _media_checks() -> list[Check]:
    checks: list[Check] = []
    for tool in ("ffprobe", "ffmpeg"):
        path = shutil.which(tool)
        if path:
            checks.append(Check(tool, "ok", path))
        else:
            checks.append(
                Check(
                    tool,
                    "warn",
                    "not on PATH — needed only to read subtitles embedded in video "
                    "containers; sidecar .ass/.srt inputs work without it.",
                )
            )
    return checks


def _writable_dir(label: str, path: Path) -> Check:
    try:
        # These are all private roots (state + cache); create them owner-only so a fresh install
        # or a doctor-first run doesn't leave the roots group/other-traversable.
        ensure_private_dir(path)
        probe = path / ".doctor-write-test"
        probe.write_text("", encoding="utf-8")
        probe.unlink()
    except OSError as exc:
        return Check(label, "fail", f"{path} is not writable: {exc}")
    return Check(label, "ok", str(path))


def _loose_entries(root: Path) -> list[Path]:
    """Paths under `root` (including `root`) still readable/traversable by group or other."""
    loose: list[Path] = []
    for path in (root, *root.rglob("*")) if root.exists() else ():
        try:
            mode = path.lstat().st_mode
        except OSError:
            continue
        if mode & 0o077:
            loose.append(path)
    return loose


def _permissions_check() -> Check:
    # Audit only the private subtrees: series memory/state (PROJECTS_DIR) and the extracted-track
    # cache (WORK_DIR), both of which can hold subtitle text. The sandbox output dir is deliberately
    # world-readable (a media server reads the final subtitle), so it is not audited here.
    loose = _loose_entries(config.PROJECTS_DIR) + _loose_entries(config.WORK_DIR)
    if not loose:
        return Check("state permissions", "ok", "state and cache are owner-only")
    sample = ", ".join(str(p) for p in loose[:3])
    more = f" (+{len(loose) - 3} more)" if len(loose) > 3 else ""
    return Check(
        "state permissions",
        "warn",
        f"{len(loose)} state/cache entries are group/other-accessible and may carry subtitle "
        f"text. Current versions write these owner-only; files from an older release keep their "
        f"old mode. Fix: chmod -R go= {config.PROJECTS_DIR} {config.WORK_DIR}. e.g. {sample}{more}",
    )


def _path_checks() -> list[Check]:
    return [
        _writable_dir("data dir", config.DATA_DIR),
        _writable_dir("projects dir", config.PROJECTS_DIR),
        _writable_dir("cache dir", config.WORK_DIR),
        _permissions_check(),
    ]


def _ollama_check(model: str | None = None) -> Check:
    host = os.environ.get("OLLAMA_HOST", "http://localhost:11434")
    base = host if host.startswith("http") else f"http://{host}"
    url = f"{base.rstrip('/')}/api/tags"
    try:
        with urllib.request.urlopen(url, timeout=5) as resp:  # noqa: S310 - local server URL
            payload = json.loads(resp.read().decode("utf-8"))
    except urllib.error.URLError as exc:
        return Check(
            "ollama",
            "fail",
            f"no server at {base} ({exc}). Start it with `ollama serve` or set $OLLAMA_HOST.",
        )
    except (ValueError, OSError) as exc:
        return Check("ollama", "warn", f"server at {base} but /api/tags was unreadable ({exc}).")

    # Be defensive: a 200 with an unexpected JSON shape (not an object, models missing/null, or
    # non-object entries) must not crash doctor — it's the one command meant to never throw.
    models = payload.get("models") if isinstance(payload, dict) else None
    if not isinstance(models, list):
        return Check("ollama", "warn", f"server at {base} returned an unexpected /api/tags shape.")
    installed = [
        m["name"] for m in models if isinstance(m, dict) and isinstance(m.get("name"), str)
    ]
    if model is None:
        listed = ", ".join(sorted(installed)) or "none"
        return Check("ollama", "ok", f"server reachable at {base}; models: {listed}")
    # Ollama tags carry a tag suffix (`qwen3:4b`); accept an exact match or the bare name.
    if model in installed or any(name.split(":", 1)[0] == model for name in installed):
        return Check("ollama", "ok", f"model '{model}' available at {base}")
    available = ", ".join(sorted(installed)) or "none"
    return Check(
        "ollama",
        "fail",
        f"model '{model}' not installed at {base} (have: {available}). Run `ollama pull {model}`.",
    )


def _litellm_check() -> Check:
    try:
        import litellm  # noqa: F401
    except ImportError:
        return Check(
            "litellm",
            "fail",
            "package not installed. Run `uv sync --extra litellm`.",
        )
    return Check("litellm", "ok", "package importable")


def _provider_check(provider: str, model: str | None = None) -> Check:
    if provider in ("identity", "file-handoff"):
        return Check(provider, "ok", "no external backend required")
    if provider in _CLI_BINARIES:
        binary = _CLI_BINARIES[provider]
        path = shutil.which(binary)
        if path:
            return Check(provider, "ok", path)
        return Check(provider, "fail", f"`{binary}` CLI not found on PATH.")
    if provider == "ollama":
        return _ollama_check(model)
    if provider == "litellm":
        return _litellm_check()
    return Check(provider, "fail", f"unknown provider '{provider}'.")


def _version_check() -> Check:
    try:
        return Check("llm-subs", "ok", _pkg_version("llm-subs"))
    except PackageNotFoundError:
        return Check("llm-subs", "warn", "running from source (package not installed)")


def run_diagnostics(provider: str | None = None, model: str | None = None) -> list[Check]:
    """Collect all checks; when `provider` is given, also verify its backend (and model)."""
    checks: list[Check] = [
        _version_check(),
        Check("python", "ok", sys.version.split()[0]),
        *_media_checks(),
        *_path_checks(),
    ]
    if provider is not None:
        checks.append(_provider_check(provider, model))
        if provider == "antigravity":
            checks.append(
                Check(
                    "antigravity isolation",
                    "warn",
                    "weakest backend: --sandbox restricts only the terminal, not tools, so a "
                    "crafted subtitle cue could steer it. Prefer a local `ollama` model for "
                    "material from an untrusted source.",
                )
            )
    return checks
