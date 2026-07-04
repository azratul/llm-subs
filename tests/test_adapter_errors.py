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


def test_run_truncates_giant_stderr_in_error_message(monkeypatch):
    # A crashing CLI can dump megabytes of stderr; the exception carries the head of it, not all
    # of it — but retryability is still classified on the full text (the marker sits at the end).
    big = "x" * 50_000 + " invalid api key"
    monkeypatch.setattr(cli_adapters.shutil, "which", lambda name: "/usr/bin/codex")
    monkeypatch.setattr(cli_adapters.subprocess, "run", lambda *a, **k: _completed(2, stderr=big))
    with pytest.raises(ProviderError) as exc_info:
        cli_adapters._run("codex", ["codex"], "prompt", 10)
    assert len(str(exc_info.value)) < 3000
    assert "characters truncated" in str(exc_info.value)
    assert not exc_info.value.retryable


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

        def read(self, limit=None):
            return b"not-json"

    monkeypatch.setattr(api_adapters.urllib.request, "urlopen", lambda *a, **k: Response())
    with pytest.raises(ProviderError, match="not valid JSON") as exc:
        api_adapters.OllamaRunner(model="qwen3:4b")("prompt")
    assert exc.value.retryable is True


@pytest.mark.parametrize(
    ("status", "retryable", "category"),
    [
        (401, False, "auth"),
        (403, False, "auth"),
        (400, False, "config"),
        (408, True, "service"),
        (409, True, "service"),
        (429, True, "quota"),
        (503, True, "service"),
    ],
)
def test_ollama_classifies_http_errors(monkeypatch, status, retryable, category):
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
    # 429 -> quota, 5xx -> service, 401/403 -> auth, other 4xx -> config: `batch` uses this to tell
    # a systemic backend fault (abort) from a per-episode one (continue).
    assert exc.value.category == category


def test_ollama_connection_failure_is_service_category(monkeypatch):
    def refuse(*a, **k):
        raise urllib.error.URLError("connection refused")

    monkeypatch.setattr(api_adapters.urllib.request, "urlopen", refuse)
    with pytest.raises(ProviderError) as exc:
        api_adapters.OllamaRunner(model="qwen3:4b")("prompt")
    assert exc.value.category == "service"  # unreachable server is systemic, not per-episode


def test_parse_translation_reply_tags_content_category():
    from translate_subs.ai.job_protocol import JobLine, TranslationJobIn
    from translate_subs.ai.provider import parse_translation_reply

    job = TranslationJobIn(
        block_id="b1", target="es-latam", translate=[JobLine(id="0001", text="Hi")]
    )
    # Invalid JSON and an id mismatch are both content/protocol faults local to this block.
    with pytest.raises(ProviderError) as exc:
        parse_translation_reply("not json at all", job)
    assert exc.value.category == "content"
    with pytest.raises(ProviderError) as exc:
        parse_translation_reply('{"9999": "x"}', job)
    assert exc.value.category == "content"


def test_retry_provider_call_preserves_category():
    from translate_subs.ai.provider import retry_provider_call

    def boom():
        raise ProviderError("bad reply", retryable=True, category="content")

    # The category must survive the re-wrap on exhausted retries, or `batch` can't see it.
    with pytest.raises(ProviderError) as exc:
        retry_provider_call(boom, max_retries=1, label="Translate", sleep=lambda _s: None)
    assert exc.value.category == "content"

    def auth_boom():
        raise ProviderError("unauthorized", retryable=False, category="auth")

    with pytest.raises(ProviderError) as exc:
        retry_provider_call(auth_boom, max_retries=2, label="Translate", sleep=lambda _s: None)
    assert exc.value.category == "auth"
