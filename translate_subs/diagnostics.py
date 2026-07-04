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
import subprocess
import sys
import urllib.error
import urllib.request
from dataclasses import dataclass
from importlib.metadata import PackageNotFoundError
from importlib.metadata import version as _pkg_version
from pathlib import Path
from typing import Literal

from translate_subs import config
from translate_subs.ai.api_adapters import host_is_loopback
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
    """Paths under `root` (including `root`) still readable/traversable by group or other.

    Symlinks are skipped: their own mode is fixed (0o777 on Linux) and not chmod-able, so they
    would be flagged forever and `--fix` could never converge; a target that lives under the
    root is audited as its own entry, and one outside it is not this tool's state.
    """
    loose: list[Path] = []
    for path in (root, *root.rglob("*")) if root.exists() else ():
        try:
            if path.is_symlink():
                continue
            mode = path.lstat().st_mode
        except OSError:
            continue
        if mode & 0o077:
            loose.append(path)
    return loose


def fix_permissions() -> tuple[int, list[str]]:
    """Tighten group/other-accessible state/cache entries to owner-only (`doctor --fix`).

    Covers exactly the subtrees `_permissions_check` audits (PROJECTS_DIR and WORK_DIR — private
    state that may carry subtitle text), so a fixed run turns that check green. Returns the
    number of entries fixed and any per-entry errors (the fix keeps going past an unfixable
    entry).
    """
    fixed = 0
    errors: list[str] = []
    for path in _loose_entries(config.PROJECTS_DIR) + _loose_entries(config.WORK_DIR):
        try:
            path.chmod(path.lstat().st_mode & ~0o077)
        except OSError as exc:
            errors.append(f"{path}: {exc}")
        else:
            fixed += 1
    return fixed, errors


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
        f"old mode. Fix: run `llm-subs doctor --fix` (or chmod -R go= {config.PROJECTS_DIR} "
        f"{config.WORK_DIR}). e.g. {sample}{more}",
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
    check = _ollama_server_check(base, model)
    # The provider's privacy pitch (subtitle text never leaves the machine) only holds for a
    # loopback host; $OLLAMA_HOST pointing elsewhere silently turns "local" into a network send.
    if check.status == "ok" and not host_is_loopback(base):
        return Check(
            "ollama",
            "warn",
            f"{check.detail} — remote host: subtitle text will be sent to it over HTTP "
            f"(the 'local and private' notes apply only to a server on this machine).",
        )
    return check


def _ollama_server_check(base: str, model: str | None) -> Check:
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


def _cli_binary_version(path: str) -> str | None:
    """First line of `<cli> --version`, or None when it can't be read.

    Best-effort: doctor must never throw, and some CLIs may not support the flag. The version
    pins down "works here / fails there" reports far better than a bare path.
    """
    try:
        proc = subprocess.run([path, "--version"], capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.SubprocessError):
        return None
    if proc.returncode != 0:
        return None
    first_line = (proc.stdout.strip() or proc.stderr.strip()).splitlines()
    return first_line[0].strip() if first_line else None


def _effective_model(provider: str, model: str | None) -> str | None:
    """The model the runner would actually use — the built-in default when --model is omitted.

    Construction is side-effect-free (no network); runners without an exposed model (the CLIs
    that pick one internally) yield None.
    """
    from translate_subs.ai.cli_adapters import make_runner

    try:
        resolved = getattr(make_runner(provider, model), "model", None)
    except Exception:
        return None
    return resolved if isinstance(resolved, str) and resolved else None


def _provider_check(provider: str, model: str | None = None) -> Check:
    if provider in ("identity", "file-handoff"):
        return Check(provider, "ok", "no external backend required")
    if provider in _CLI_BINARIES:
        binary = _CLI_BINARIES[provider]
        path = shutil.which(binary)
        if path is None:
            return Check(provider, "fail", f"`{binary}` CLI not found on PATH.")
        detail = path
        version = _cli_binary_version(path)
        if version:
            detail += f" ({version})"
        effective = _effective_model(provider, model)
        if effective:
            detail += f"; model: {effective}"
        elif model is None:
            detail += "; model: chosen by the CLI (pass --model to pin it)"
        return Check(provider, "ok", detail)
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
