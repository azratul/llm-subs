"""Translation runners for the supported backends.

Each runner is a callable `prompt -> assistant_text`, swappable behind the same
provider. `claude` is the verified default; `codex`, `gemini` and `opencode` use each
tool's documented non-interactive invocation; `ollama` and `litellm` (see
`api_adapters`) talk to a local server / model router for cheap models. Higher layers
build the prompt and parse the reply, so these stay free of domain knowledge.
"""

from __future__ import annotations

import shutil
import subprocess
import tempfile
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from translate_subs.ai.api_adapters import LiteLLMRunner, OllamaRunner
from translate_subs.ai.claude_cli import ClaudeCli
from translate_subs.ai.provider import ProviderError, backend_error_is_retryable


def _run(binary_name: str, cmd: list[str], prompt: str | None, timeout: int) -> str:
    binary = shutil.which(binary_name)
    if binary is None:
        raise ProviderError(f"`{binary_name}` CLI not found on PATH.", retryable=False)
    cmd = [binary, *cmd[1:]]
    # Run from an empty throwaway directory: on top of each CLI's read-only sandbox, this keeps a
    # crafted subtitle from nudging the agent toward whatever files sit in the user's real cwd.
    try:
        with tempfile.TemporaryDirectory(prefix="translate-subs-cwd-") as cwd:
            proc = subprocess.run(
                cmd, input=prompt, capture_output=True, text=True, timeout=timeout, cwd=cwd
            )
    except subprocess.TimeoutExpired as exc:
        raise ProviderError(
            f"`{binary_name}` timed out after {timeout}s",
            retryable=True,
        ) from exc
    if proc.returncode != 0:
        detail = proc.stderr.strip() or proc.stdout.strip() or "no output"
        raise ProviderError(
            f"`{binary_name}` failed (exit {proc.returncode}): {detail}",
            retryable=backend_error_is_retryable(detail),
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
class GeminiCli:
    """Gemini CLI in headless mode: `-p` triggers non-interactive; stdin carries the prompt."""

    model: str | None = None
    binary: str = "gemini"
    timeout: int = 600

    def __call__(self, prompt: str) -> str:
        # plan = read-only approval mode: tools that would modify anything are auto-rejected.
        cmd = [self.binary, "-o", "text", "--approval-mode", "plan"]
        if self.model:
            cmd += ["-m", self.model]
        cmd += ["-p", ""]  # empty flag prompt; real prompt is appended from stdin
        return _run(self.binary, cmd, prompt, self.timeout)


@dataclass
class OpencodeCli:
    """opencode CLI: `opencode run <message>` runs headless and prints the reply."""

    model: str | None = None
    binary: str = "opencode"
    timeout: int = 600

    def __call__(self, prompt: str) -> str:
        # --pure: no external plugins; and we never pass --dangerously-skip-permissions, so
        # tool actions still require (here, unavailable) approval rather than auto-running.
        cmd = [self.binary, "run", "--pure"]
        if self.model:
            cmd += ["-m", self.model]
        cmd += [prompt]
        return _run(self.binary, cmd, None, self.timeout)


Runner = Callable[[str], str]


def _codex(model: str | None, reasoning: str | None) -> Runner:
    return CodexCli(model, reasoning_effort=reasoning or "low")


# Each builder takes (model, reasoning); `reasoning` is codex-specific and ignored elsewhere.
_RUNNERS: dict[str, Callable[[str | None, str | None], Runner]] = {
    "claude": lambda model, reasoning: ClaudeCli(model) if model else ClaudeCli(),
    "codex": _codex,
    "gemini": lambda model, reasoning: GeminiCli(model),
    "opencode": lambda model, reasoning: OpencodeCli(model),
    "ollama": lambda model, reasoning: OllamaRunner(model),
    "litellm": lambda model, reasoning: LiteLLMRunner(model),
}

CLI_PROVIDERS = tuple(_RUNNERS)


def make_runner(provider: str, model: str | None = None, reasoning: str | None = None) -> Runner:
    try:
        return _RUNNERS[provider](model, reasoning)
    except KeyError:
        raise ProviderError(
            f"No CLI runner for provider '{provider}'.",
            retryable=False,
        ) from None
