from __future__ import annotations

import sys
import types

import pytest

from translate_subs.ai import api_adapters
from translate_subs.ai.api_adapters import LiteLLMRunner, OllamaRunner
from translate_subs.ai.cli_adapters import make_runner
from translate_subs.ai.provider import CliTranslationProvider, ProviderError
from translate_subs.pipeline import make_provider


def test_ollama_builds_chat_request(monkeypatch):
    captured = {}

    def fake_post(url, payload, timeout):
        captured.update(url=url, payload=payload, timeout=timeout)
        return {"message": {"content": "HOLA"}}

    monkeypatch.setattr(api_adapters, "_post_json", fake_post)

    assert OllamaRunner(model="qwen3:4b", host="http://localhost:11434")("PROMPT") == "HOLA"
    assert captured["url"] == "http://localhost:11434/api/chat"
    payload = captured["payload"]
    assert payload["model"] == "qwen3:4b"
    assert payload["stream"] is False
    assert payload["format"] == "json"
    assert payload["think"] is False  # off by default: faster, cleaner JSON
    assert payload["messages"][0]["content"] == "PROMPT"


def test_post_json_rejects_oversized_response(monkeypatch):
    """A broken server streaming an endless body must not be buffered whole into memory."""

    class FakeResponse:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def read(self, limit):
            return b"x" * limit  # always fills the cap, i.e. the body is larger than allowed

    monkeypatch.setattr(api_adapters.urllib.request, "urlopen", lambda req, timeout: FakeResponse())
    with pytest.raises(ProviderError, match="exceeded") as exc_info:
        api_adapters._post_json("http://localhost:11434/api/chat", {}, timeout=5)
    assert exc_info.value.category == "content"
    assert not exc_info.value.retryable


def test_ollama_can_omit_think(monkeypatch):
    captured = {}

    def fake_post(url, payload, timeout):
        captured.update(payload=payload)
        return {"message": {"content": "x"}}

    monkeypatch.setattr(api_adapters, "_post_json", fake_post)
    OllamaRunner(model="m", think=None)("P")
    assert "think" not in captured["payload"]


def test_ollama_host_from_env_gets_scheme(monkeypatch):
    captured = {}
    monkeypatch.setenv("OLLAMA_HOST", "remote:1234")
    monkeypatch.setattr(
        api_adapters,
        "_post_json",
        lambda url, payload, timeout: captured.update(url=url) or {"message": {"content": "x"}},
    )
    OllamaRunner(model="m")("P")
    assert captured["url"] == "http://remote:1234/api/chat"


def test_ollama_host_keeps_explicit_https(monkeypatch):
    captured = {}
    monkeypatch.setattr(
        api_adapters,
        "_post_json",
        lambda url, payload, timeout: captured.update(url=url) or {"message": {"content": "x"}},
    )
    OllamaRunner(model="m", host="https://ollama.example:443")("P")
    assert captured["url"] == "https://ollama.example:443/api/chat"


def test_ollama_rejects_non_http_scheme():
    with pytest.raises(ProviderError, match="http:// or https://"):
        OllamaRunner(model="m", host="file:///etc/passwd")("P")


def test_ollama_requires_model():
    with pytest.raises(ProviderError, match="requires --model"):
        OllamaRunner()("P")


def test_ollama_rejects_unexpected_response(monkeypatch):
    monkeypatch.setattr(api_adapters, "_post_json", lambda *a, **k: {"oops": 1})
    with pytest.raises(ProviderError, match="Unexpected Ollama response"):
        OllamaRunner(model="m")("P")


def test_ollama_rejects_null_content(monkeypatch):
    monkeypatch.setattr(api_adapters, "_post_json", lambda *a, **k: {"message": {"content": None}})
    with pytest.raises(ProviderError, match="null/non-text content"):
        OllamaRunner(model="m")("P")


def test_litellm_rejects_null_content(monkeypatch):
    fake = types.ModuleType("litellm")

    def completion(model, messages, timeout=None, **kwargs):
        message = types.SimpleNamespace(content=None)  # provider returned null content
        return types.SimpleNamespace(choices=[types.SimpleNamespace(message=message)])

    fake.completion = completion
    monkeypatch.setitem(sys.modules, "litellm", fake)
    with pytest.raises(ProviderError, match="null/non-text content"):
        LiteLLMRunner(model="gpt-4o-mini")("P")


def test_litellm_calls_completion(monkeypatch):
    calls = {}

    fake = types.ModuleType("litellm")

    def completion(model, messages, timeout=None, **kwargs):
        calls.update(model=model, messages=messages, timeout=timeout)
        message = types.SimpleNamespace(content="HOLA")
        return types.SimpleNamespace(choices=[types.SimpleNamespace(message=message)])

    fake.completion = completion
    monkeypatch.setitem(sys.modules, "litellm", fake)

    assert LiteLLMRunner(model="ollama/qwen3:4b")("PROMPT") == "HOLA"
    assert calls["model"] == "ollama/qwen3:4b"
    assert calls["messages"][0]["content"] == "PROMPT"


def test_litellm_missing_dependency(monkeypatch):
    monkeypatch.setitem(sys.modules, "litellm", None)  # makes `import litellm` raise
    with pytest.raises(ProviderError, match="litellm is not installed"):
        LiteLLMRunner(model="gpt-4o-mini")("P")


def test_litellm_requires_model():
    with pytest.raises(ProviderError, match="requires --model"):
        LiteLLMRunner()("P")


def test_make_runner_builds_local_backends():
    assert isinstance(make_runner("ollama"), OllamaRunner)
    assert isinstance(make_runner("litellm"), LiteLLMRunner)


def test_make_provider_wires_local_backends(tmp_path):
    for name in ("ollama", "litellm"):
        assert isinstance(make_provider(name, tmp_path), CliTranslationProvider)
