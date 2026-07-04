"""Translation runners for the supported backends.

Each runner is a callable `prompt -> assistant_text`, swappable behind the same
provider. `claude` is the verified default; `codex`, `antigravity` and `opencode` use each
tool's documented non-interactive invocation; `ollama` and `litellm` (see
`api_adapters`) talk to a local server / model router for cheap models. Higher layers
build the prompt and parse the reply, so these stay free of domain knowledge.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from translate_subs.ai.api_adapters import LiteLLMRunner, OllamaRunner
from translate_subs.ai.claude_cli import ClaudeCli
from translate_subs.ai.provider import ProviderError, backend_error_is_retryable, truncate_detail


def _run(
    binary_name: str,
    cmd: list[str],
    prompt: str | None,
    timeout: int,
    env: dict[str, str] | None = None,
) -> str:
    binary = shutil.which(binary_name)
    if binary is None:
        raise ProviderError(
            f"`{binary_name}` CLI not found on PATH.", retryable=False, category="config"
        )
    cmd = [binary, *cmd[1:]]
    # Run from an empty throwaway directory: on top of each CLI's read-only sandbox, this keeps a
    # crafted subtitle from nudging the agent toward whatever files sit in the user's real cwd.
    try:
        with tempfile.TemporaryDirectory(prefix="llm-subs-cwd-") as cwd:
            proc = subprocess.run(
                cmd,
                input=prompt,
                capture_output=True,
                text=True,
                timeout=timeout,
                cwd=cwd,
                env=env,
            )
    except subprocess.TimeoutExpired as exc:
        raise ProviderError(
            f"`{binary_name}` timed out after {timeout}s",
            retryable=True,
            category="service",
        ) from exc
    if proc.returncode != 0:
        detail = proc.stderr.strip() or proc.stdout.strip() or "no output"
        raise ProviderError(
            f"`{binary_name}` failed (exit {proc.returncode}): {truncate_detail(detail)}",
            retryable=backend_error_is_retryable(detail),
        )
    if not proc.stdout.strip():
        detail = proc.stderr.strip() or "no output on stdout"
        raise ProviderError(
            f"`{binary_name}` produced no output: {truncate_detail(detail)}",
            retryable=True,
        )
    return proc.stdout


@dataclass
class CodexCli:
    """OpenAI Codex CLI: `codex exec` reads the prompt from stdin.

    The final assistant message is captured via `--output-last-message`, avoiding the
    interleaved event log on stdout.
    """

    model: str | None = None
    binary: str = "codex"
    timeout: int = 600
    # translation is not reasoning-heavy; avoid the slow xhigh default
    reasoning_effort: str = "low"

    def __call__(self, prompt: str) -> str:
        with tempfile.TemporaryDirectory() as tmp:
            out_file = Path(tmp) / "last_message.txt"
            # read-only sandbox: model-generated shell commands can't touch the filesystem,
            # so untrusted subtitle text can't trick the agent into writing/deleting files.
            cmd = [self.binary, "exec", "--skip-git-repo-check", "--sandbox", "read-only"]
            if self.model:
                cmd += ["-m", self.model]
            if self.reasoning_effort:
                cmd += ["-c", f'model_reasoning_effort="{self.reasoning_effort}"']
            cmd += ["-o", str(out_file), "-"]
            stdout = _run(self.binary, cmd, prompt, self.timeout)
            if out_file.exists():
                text = out_file.read_text("utf-8").strip()
                if text:
                    return text
            return stdout


@dataclass
class AntigravityCli:
    """Google's Antigravity CLI (`agy`): `--print` runs one prompt non-interactively from stdin.

    Antigravity replaced the standalone Gemini CLI. Unlike `gemini --approval-mode plan` (which
    auto-rejected every tool), `agy` is agentic and has no read-only/no-tools switch: `--sandbox`
    only restricts the terminal, so it can still run commands. The containment we rely on is the
    empty throwaway cwd `_run` provides (the sandbox confines file access to that empty workspace)
    plus never passing `--dangerously-skip-permissions`. Model names are the descriptive labels
    `agy models` prints (e.g. "Gemini 3.5 Flash (Low)"), not API ids; omitting `--model` uses the
    CLI's own default.
    """

    model: str | None = None
    binary: str = "agy"
    timeout: int = 600

    def __call__(self, prompt: str) -> str:
        cmd = [self.binary, "--print", "--sandbox"]
        if self.model:
            cmd += ["--model", self.model]
        # Bound agy's own wait to our timeout; give the subprocess a small grace period on top so
        # agy reports its timeout rather than being killed mid-write.
        cmd += ["--print-timeout", f"{self.timeout}s"]
        return _run(self.binary, cmd, prompt, self.timeout + 10)


@dataclass
class OpencodeCli:
    """opencode CLI: `opencode run` runs headless, reads the prompt from stdin, prints the reply.

    The prompt goes through stdin, not argv (verified live: stdout is byte-identical either way,
    the banner goes to stderr): argv is visible to every local user in the process list, and
    subtitle-derived prompts don't belong there — it also sidesteps OS argv length limits.
    """

    model: str | None = None
    binary: str = "opencode"
    timeout: int = 600

    def __call__(self, prompt: str) -> str:
        # Hardening: `--pure` only disables external plugins — opencode's built-in tools
        # (read/bash/webfetch/websearch/edit) are allowed by default, and from a headless `run`
        # untrusted subtitle text could read absolute paths (e.g. ~/.ssh) and exfiltrate them. We
        # deny every tool via an inline config (highest standard precedence, overriding any global
        # opencode.json that allows tools): translation needs no tools, only text in/text out. This
        # gives opencode a containment comparable to `claude --disallowedTools`.
        env = {**os.environ, "OPENCODE_CONFIG_CONTENT": json.dumps({"permission": {"*": "deny"}})}
        cmd = [self.binary, "run", "--pure"]
        if self.model:
            cmd += ["-m", self.model]
        return _run(self.binary, cmd, prompt, self.timeout, env=env)


Runner = Callable[[str], str]


def _codex(model: str | None, reasoning: str | None) -> Runner:
    return CodexCli(model, reasoning_effort=reasoning or "low")


# Each builder takes (model, reasoning); `reasoning` is codex-specific and ignored elsewhere.
_RUNNERS: dict[str, Callable[[str | None, str | None], Runner]] = {
    "claude": lambda model, reasoning: ClaudeCli(model) if model else ClaudeCli(),
    "codex": _codex,
    "antigravity": lambda model, reasoning: AntigravityCli(model),
    "opencode": lambda model, reasoning: OpencodeCli(model),
    "ollama": lambda model, reasoning: OllamaRunner(model),
    "litellm": lambda model, reasoning: LiteLLMRunner(model),
}

CLI_PROVIDERS = tuple(_RUNNERS)


def make_runner(
    provider: str,
    model: str | None = None,
    reasoning: str | None = None,
    *,
    timeout: int | None = None,
) -> Runner:
    try:
        runner = _RUNNERS[provider](model, reasoning)
    except KeyError:
        raise ProviderError(
            f"No CLI runner for provider '{provider}'.",
            retryable=False,
        ) from None
    if timeout is not None:
        # Every runner is a dataclass exposing a `timeout` field; the Runner alias hides it.
        runner.timeout = timeout  # type: ignore[attr-defined]
    return runner
