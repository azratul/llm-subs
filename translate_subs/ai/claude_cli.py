"""Adapter over the `claude` CLI in print mode (`claude -p`).

A thin, reusable runner: it sends a prompt to the local Claude CLI and returns
the assistant's text. Higher layers (analysis, translation) build the prompt and
parse the reply, so this stays free of domain knowledge.

No paid API is used directly; the CLI carries the user's own auth/session.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import tempfile
from dataclasses import dataclass

from translate_subs.ai.provider import ProviderError, backend_error_is_retryable

DEFAULT_MODEL = "claude-opus-4-8"

# Translation is pure text in / text out: the agent needs no tools. Subtitle text is untrusted
# (it could try to talk the agent into reading or modifying personal files), so deny every
# filesystem/exec/network/subagent tool and ignore all MCP servers. An unknown name here is
# harmless — the CLI just ignores it — so the list can stay conservative across versions.
_DENIED_TOOLS = (
    "Bash",
    "Edit",
    "MultiEdit",
    "Write",
    "NotebookEdit",
    "Read",
    "Glob",
    "Grep",
    "WebFetch",
    "WebSearch",
    "Task",
)

_JSON_FENCE_RE = re.compile(r"```(?:json)?\s*(.*?)\s*```", re.DOTALL)


def extract_json(raw: str) -> str:
    """Return the JSON substring of a model reply.

    Tolerates code fences and prose around the object/array.
    """
    text = raw.strip()
    fenced = _JSON_FENCE_RE.search(text)
    if fenced:
        return fenced.group(1).strip()
    start = min(
        (p for p in (text.find("{"), text.find("[")) if p != -1),
        default=-1,
    )
    end = max(text.rfind("}"), text.rfind("]"))
    if start != -1 and end > start:
        return text[start : end + 1]
    return text


@dataclass
class ClaudeCli:
    """Callable runner: `runner(prompt) -> assistant_text`.

    Designed to be injected, so callers can swap in a fake in tests.
    """

    model: str = DEFAULT_MODEL
    binary: str = "claude"
    timeout: int = 600  # seconds; whole-episode prompts can be slow.

    def __call__(self, prompt: str) -> str:
        binary = shutil.which(self.binary)
        if binary is None:
            raise ProviderError(
                f"`{self.binary}` CLI not found on PATH. Install it or pass a runner.",
                retryable=False,
            )

        cmd = [
            binary,
            "-p",
            "--model",
            self.model,
            "--output-format",
            "json",
            "--strict-mcp-config",  # no --mcp-config passed -> ignore all configured MCP servers
            "--disallowedTools",
            *_DENIED_TOOLS,
        ]
        # Run from an empty throwaway directory: on top of the denied-tools sandbox, this keeps a
        # crafted subtitle from steering the agent toward files in the user's real cwd.
        try:
            with tempfile.TemporaryDirectory(prefix="translate-subs-cwd-") as cwd:
                proc = subprocess.run(
                    cmd,
                    input=prompt,
                    capture_output=True,
                    text=True,
                    timeout=self.timeout,
                    cwd=cwd,
                )
        except subprocess.TimeoutExpired as exc:
            raise ProviderError(
                f"`{self.binary}` timed out after {self.timeout}s",
                retryable=True,
            ) from exc

        if proc.returncode != 0:
            detail = proc.stderr.strip() or proc.stdout.strip() or "no output"
            raise ProviderError(
                f"`{self.binary}` failed (exit {proc.returncode}): {detail}",
                retryable=backend_error_is_retryable(detail),
            )

        return _unwrap_result(proc.stdout)


def _unwrap_result(stdout: str) -> str:
    """Extract the assistant text from `--output-format json`.

    The CLI wraps the reply in a result envelope (`{"result": "...", ...}`). If the
    envelope is missing or malformed, fall back to the raw stdout.
    """
    stdout = stdout.strip()
    if not stdout:
        raise ProviderError("`claude` returned no output", retryable=True)
    try:
        envelope = json.loads(stdout)
    except json.JSONDecodeError:
        return stdout
    if isinstance(envelope, dict):
        if envelope.get("is_error"):
            raise ProviderError(
                f"`claude` reported an error: {envelope.get('result', envelope)}",
                retryable=backend_error_is_retryable(str(envelope.get("result", envelope))),
            )
        result = envelope.get("result")
        if isinstance(result, str):
            return result
    return stdout
