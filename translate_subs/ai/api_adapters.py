"""Model runners backed by a local server (Ollama) or a router (LiteLLM).

Like the CLI adapters, each is a callable `prompt -> assistant_text` swappable behind
`CliTranslationProvider`. These talk to a model API instead of spawning an agent CLI, so
they suit cheap/local models for the high-volume `translate` step. Ollama is reached over
its native HTTP API (stdlib only) and asked for JSON output, which keeps small models
inside the strict id->text contract the higher layers parse. LiteLLM routes to any backend
(`ollama/...`, `gpt-...`, `anthropic/...`) through its SDK, which is an optional dependency.
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from dataclasses import dataclass
from email.utils import parsedate_to_datetime
from time import time
from typing import Any

from translate_subs.ai.provider import ErrorCategory, ProviderError, backend_error_is_retryable

_DEFAULT_OLLAMA_HOST = "http://localhost:11434"
_RETRYABLE_HTTP_STATUSES = {408, 409, 425, 429, 500, 502, 503, 504}


def _retry_after_seconds(value: str | None) -> float | None:
    if not value:
        return None
    try:
        return max(0.0, float(value))
    except ValueError:
        try:
            return max(0.0, parsedate_to_datetime(value).timestamp() - time())
        except (TypeError, ValueError, OverflowError):
            return None


def _post_json(url: str, payload: dict[str, Any], timeout: int) -> dict[str, Any]:
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        retryable = exc.code in _RETRYABLE_HTTP_STATUSES or 500 <= exc.code < 600
        retry_after = _retry_after_seconds(
            exc.headers.get("Retry-After") if exc.headers is not None else None
        )
        if exc.code == 429:
            category: ErrorCategory = "quota"
        elif exc.code in (401, 403):
            category = "auth"
        elif retryable:  # 5xx and transient 4xx (408/409/425): the server/link, not our config
            category = "service"
        else:
            category = "config"
        raise ProviderError(
            f"Request to {url} failed with HTTP {exc.code}: {exc.reason}",
            retryable=retryable,
            retry_after=retry_after,
            category=category,
        ) from exc
    except urllib.error.URLError as exc:
        # Connection refused / timeout / DNS: the server is unreachable — systemic, not per-episode.
        raise ProviderError(
            f"Request to {url} failed: {exc}", retryable=True, category="service"
        ) from exc
    except UnicodeDecodeError as exc:
        raise ProviderError(
            f"Response from {url} was not valid UTF-8: {exc}",
            retryable=True,
        ) from exc
    try:
        result = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ProviderError(
            f"Response from {url} was not valid JSON: {exc}",
            retryable=True,
        ) from exc
    if not isinstance(result, dict):
        raise ProviderError(
            f"Response from {url} must be a JSON object.",
            retryable=True,
        )
    return result


def _normalize_host(host: str) -> str:
    """Normalize an Ollama host to an http(s) base URL.

    A bare `host:port` (the common `localhost:11434`) gets an `http://` scheme. An explicit scheme
    is accepted only when it is http/https — anything else (`file://`, `ftp://`, a typo like
    `http://`) is rejected rather than silently turned into a request. `urlparse` is unusable here
    because it reads the port of a schemeless `localhost:11434` as the scheme, so the split is
    explicit.
    """
    host = host.rstrip("/")
    if "://" in host:
        scheme = host.split("://", 1)[0].lower()
        if scheme not in ("http", "https"):
            raise ProviderError(
                f"Ollama host must use http:// or https://, got {scheme}://.",
                retryable=False,
                category="config",
            )
        return host
    return f"http://{host}"


@dataclass
class OllamaRunner:
    """Local Ollama via its native /api/chat endpoint.

    `format="json"` constrains the reply to valid JSON, which is what every prompt here
    asks for and what small models most often get wrong otherwise. `think` defaults to
    False: translation is not reasoning-heavy, and a thinking model (e.g. qwen3) is far
    slower and more verbose with it on; non-thinking models ignore the flag. The host
    falls back to $OLLAMA_HOST then localhost.
    """

    model: str | None = None
    host: str | None = None
    timeout: int = 600
    json_format: bool = True
    think: bool | None = False

    def __call__(self, prompt: str) -> str:
        if not self.model:
            raise ProviderError(
                "Ollama requires --model (e.g. qwen3:4b).",
                retryable=False,
                category="config",
            )
        base = _normalize_host(self.host or os.environ.get("OLLAMA_HOST", _DEFAULT_OLLAMA_HOST))
        payload: dict[str, Any] = {
            "model": self.model,
            "messages": [{"role": "user", "content": prompt}],
            "stream": False,
        }
        if self.json_format:
            payload["format"] = "json"
        if self.think is not None:
            payload["think"] = self.think
        data = _post_json(f"{base}/api/chat", payload, self.timeout)
        try:
            content = data["message"]["content"]
        except (KeyError, TypeError) as exc:
            raise ProviderError(
                f"Unexpected Ollama response: {data}",
                retryable=True,
            ) from exc
        if not isinstance(content, str):
            raise ProviderError(
                "Ollama returned null/non-text content.",
                retryable=True,
            )
        return content


@dataclass
class LiteLLMRunner:
    """Any backend through the LiteLLM SDK; `model` carries the provider prefix.

    Examples: `ollama/qwen3:4b`, `gpt-4o-mini`, `anthropic/claude-opus-4-8`. The SDK is
    imported lazily so it is only required when this provider is actually used.
    """

    model: str | None = None
    timeout: int = 600

    def __call__(self, prompt: str) -> str:
        if not self.model:
            raise ProviderError(
                "LiteLLM requires --model (e.g. ollama/qwen3:4b, gpt-4o-mini).",
                retryable=False,
            )
        try:
            import litellm
        except ImportError as exc:
            raise ProviderError(
                "litellm is not installed. Run `uv sync --extra litellm` "
                "to use --provider litellm.",
                retryable=False,
            ) from exc
        try:
            resp = litellm.completion(
                model=self.model,
                messages=[{"role": "user", "content": prompt}],
                timeout=self.timeout,
            )
        except Exception as exc:  # noqa: BLE001 - litellm raises backend-specific errors
            raise ProviderError(
                f"litellm completion failed: {exc}",
                retryable=backend_error_is_retryable(str(exc)),
            ) from exc
        try:
            content = resp.choices[0].message.content
        except (AttributeError, IndexError, TypeError) as exc:
            raise ProviderError(
                f"Unexpected litellm response: {resp}",
                retryable=True,
            ) from exc
        if not isinstance(content, str):
            raise ProviderError(
                "litellm returned null/non-text content.",
                retryable=True,
            )
        return content
