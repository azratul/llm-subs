"""Error-path coverage for the backend adapters.

The happy paths and argv construction are tested elsewhere; this focuses on what happens when a
backend is missing, exits non-zero, times out, returns an error envelope, or can't be reached —
the zones most likely to bite in real use and least exercised by the rest of the suite.
"""

from __future__ import annotations

import io
import subprocess
import urllib.error
from pathlib import Path

import pytest

from translate_subs.ai import api_adapters, claude_cli, cli_adapters
from translate_subs.ai.claude_cli import ClaudeCli, _unwrap_result
from translate_subs.ai.provider import ProviderError


def _completed(returncode, stdout="", stderr=""):
    return subprocess.CompletedProcess(
        args=["x"], returncode=returncode, stdout=stdout, stderr=stderr
    )


def test_run_raises_when_binary_missing(monkeypatch):
    monkeypatch.setattr(cli_adapters.shutil, "which", lambda name: None)
    with pytest.raises(ProviderError, match="not found on PATH"):
        cli_adapters._run("codex", ["codex", "exec"], "prompt", 10)


def test_run_raises_on_nonzero_exit_with_detail(monkeypatch):
    monkeypatch.setattr(cli_adapters.shutil, "which", lambda name: "/usr/bin/codex")
    monkeypatch.setattr(
        cli_adapters.subprocess, "run", lambda *a, **k: _completed(2, stderr="explode")
    )
    with pytest.raises(ProviderError, match=r"failed \(exit 2\): explode"):
        cli_adapters._run("codex", ["codex"], "prompt", 10)


def test_run_raises_on_timeout(monkeypatch):
    monkeypatch.setattr(cli_adapters.shutil, "which", lambda name: "/usr/bin/agy")

    def boom(*a, **k):
        raise subprocess.TimeoutExpired(cmd="agy", timeout=10)

    monkeypatch.setattr(cli_adapters.subprocess, "run", boom)
    with pytest.raises(ProviderError, match="timed out after 10s"):
        cli_adapters._run("agy", ["agy"], "prompt", 10)


def test_claude_cli_hardens_argv(monkeypatch):
    captured = {}

    def fake_run(cmd, **kwargs):
        captured["cmd"] = cmd
        captured["cwd"] = kwargs.get("cwd")
        return _completed(0, stdout='{"result": "ok", "is_error": false}')

    monkeypatch.setattr(claude_cli.shutil, "which", lambda name: "/usr/bin/claude")
    monkeypatch.setattr(claude_cli.subprocess, "run", fake_run)

    assert ClaudeCli()("prompt") == "ok"
    cmd = captured["cmd"]
    # No MCP servers, and every filesystem/exec/network/subagent tool is denied.
    assert "--strict-mcp-config" in cmd
    assert "--dangerously-skip-permissions" not in cmd
    denied = cmd[cmd.index("--disallowedTools") + 1 :]
    for tool in ("Bash", "Edit", "Write", "Read", "WebFetch", "Task"):
        assert tool in denied
    # Hardening: runs from an empty throwaway directory, not the user's real cwd.
    cwd = captured["cwd"]
    assert cwd is not None and Path(cwd).name.startswith("llm-subs-cwd-")


def test_claude_cli_raises_when_binary_missing(monkeypatch):
    monkeypatch.setattr(claude_cli.shutil, "which", lambda name: None)
    with pytest.raises(ProviderError, match="not found on PATH"):
        ClaudeCli()("prompt")


def test_claude_cli_raises_on_nonzero_exit(monkeypatch):
    monkeypatch.setattr(claude_cli.shutil, "which", lambda name: "/usr/bin/claude")
    monkeypatch.setattr(
        claude_cli.subprocess, "run", lambda *a, **k: _completed(1, stderr="auth error")
    )
    with pytest.raises(ProviderError, match=r"failed \(exit 1\): auth error") as exc:
        ClaudeCli()("prompt")
    assert exc.value.retryable is False


def test_claude_unwrap_handles_error_envelope_and_empty():
    assert _unwrap_result('{"result": "hello", "is_error": false}') == "hello"
    with pytest.raises(ProviderError, match="reported an error"):
        _unwrap_result('{"result": "boom", "is_error": true}')
    with pytest.raises(ProviderError, match="no output"):
        _unwrap_result("   ")
    # A non-JSON reply falls back to the raw text rather than failing.
    assert _unwrap_result("plain text") == "plain text"


def test_ollama_wraps_connection_failure(monkeypatch):
    def refuse(*a, **k):
        raise urllib.error.URLError("connection refused")

    monkeypatch.setattr(api_adapters.urllib.request, "urlopen", refuse)
    with pytest.raises(ProviderError, match="Request to .* failed"):
        api_adapters.OllamaRunner(model="qwen3:4b")("prompt")


def test_ollama_wraps_invalid_json_as_retryable(monkeypatch):
    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return None

        def read(self):
            return b"not-json"

    monkeypatch.setattr(api_adapters.urllib.request, "urlopen", lambda *a, **k: Response())
    with pytest.raises(ProviderError, match="not valid JSON") as exc:
        api_adapters.OllamaRunner(model="qwen3:4b")("prompt")
    assert exc.value.retryable is True


@pytest.mark.parametrize(("status", "retryable"), [(401, False), (429, True), (503, True)])
def test_ollama_classifies_http_errors(monkeypatch, status, retryable):
    headers = {"Retry-After": "5"} if status == 429 else {}

    def fail(*args, **kwargs):
        raise urllib.error.HTTPError(
            url="http://localhost/api/chat",
            code=status,
            msg="error",
            hdrs=headers,
            fp=io.BytesIO(b""),
        )

    monkeypatch.setattr(api_adapters.urllib.request, "urlopen", fail)
    with pytest.raises(ProviderError) as exc:
        api_adapters.OllamaRunner(model="qwen3:4b")("prompt")
    assert exc.value.retryable is retryable
    assert exc.value.retry_after == (5.0 if status == 429 else None)
