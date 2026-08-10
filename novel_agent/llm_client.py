from __future__ import annotations

import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Any, Callable

try:
    import requests
except ImportError:  # pragma: no cover - urllib fallback keeps the client usable.
    requests = None

try:
    import httpx
except ImportError:  # pragma: no cover - requests/urllib fallback keeps the client usable.
    httpx = None


class LLMError(RuntimeError):
    pass


class LLMHTTPError(LLMError):
    def __init__(self, status: int, detail: str):
        self.status = status
        self.detail = detail
        super().__init__(f"HTTP {status}: {detail}")


DEFAULT_HTTP_HEADERS = {
    "Accept": "application/json",
    "User-Agent": "curl/8.0",
}


def split_api_keys(raw: str | list[str] | tuple[str, ...] | None) -> list[str]:
    if raw is None:
        return []
    if isinstance(raw, (list, tuple)):
        parts: list[str] = []
        for item in raw:
            parts.extend(split_api_keys(item))
        return parts
    keys: list[str] = []
    for part in re.split(r"[\r\n,;]+", str(raw)):
        key = part.strip().lstrip("\ufeff")
        if key:
            keys.append(key)
    return keys


def collect_api_keys(*values: str | list[str] | tuple[str, ...] | None) -> list[str]:
    keys: list[str] = []
    seen: set[str] = set()
    for value in values:
        for key in split_api_keys(value):
            if key not in seen:
                keys.append(key)
                seen.add(key)
    return keys


def mask_key(key: str) -> str:
    if not key:
        return "<empty>"
    if len(key) <= 10:
        return "<key>"
    return f"{key[:6]}...{key[-4:]}"


@dataclass
class LLMConfig:
    provider: str
    model: str
    api_key: str = ""
    api_keys: list[str] = field(default_factory=list)
    base_url: str = ""
    timeout: int = 180
    retries: int = 2


class LLMClient:
    def __init__(self, config: LLMConfig):
        self.config = config
        if self.config.api_key and self.config.api_key not in self.config.api_keys:
            self.config.api_keys.insert(0, self.config.api_key)
        self._disabled_key_indices: set[int] = set()
        self._next_key_index = 0
        self._active_api_key = self.config.api_key

    def fork(self, *, timeout: int | None = None, retries: int | None = None, start_index: int = 0) -> "LLMClient":
        config = LLMConfig(
            provider=self.config.provider,
            model=self.config.model,
            api_key=self.config.api_key,
            api_keys=list(self.config.api_keys),
            base_url=self.config.base_url,
            timeout=self.config.timeout if timeout is None else timeout,
            retries=self.config.retries if retries is None else retries,
        )
        client = LLMClient(config)
        if client.config.api_keys:
            client._next_key_index = start_index % len(client.config.api_keys)
        return client

    @classmethod
    def from_values(
        cls,
        provider: str,
        model: str = "",
        api_key: str = "",
        api_keys: list[str] | str | None = None,
        base_url: str = "",
        timeout: int = 180,
        retries: int = 2,
    ) -> "LLMClient":
        provider = provider.lower()
        key_pool: list[str] = []
        if provider == "openai":
            model = model or os.environ.get("OPENAI_MODEL", "gpt-4.1")
            key_pool = collect_api_keys(api_keys, api_key, os.environ.get("OPENAI_API_KEYS", ""), os.environ.get("OPENAI_API_KEY", ""))
            api_key = key_pool[0] if key_pool else ""
            base_url = (base_url or os.environ.get("OPENAI_BASE_URL", "https://api.openai.com")).rstrip("/")
            if not api_key:
                raise LLMError("Missing OPENAI_API_KEY or OPENAI_API_KEYS. ChatGPT Plus alone is not an API key.")
        elif provider == "anthropic":
            model = model or os.environ.get("ANTHROPIC_MODEL", "claude-3-5-sonnet-latest")
            key_pool = collect_api_keys(api_keys, api_key, os.environ.get("ANTHROPIC_API_KEYS", ""), os.environ.get("ANTHROPIC_API_KEY", ""))
            api_key = key_pool[0] if key_pool else ""
            base_url = (base_url or os.environ.get("ANTHROPIC_BASE_URL", "https://api.anthropic.com")).rstrip("/")
            if not api_key:
                raise LLMError("Missing ANTHROPIC_API_KEY or ANTHROPIC_API_KEYS. Claude Pro alone is not an API key.")
        elif provider == "deepseek":
            model = model or os.environ.get("DEEPSEEK_MODEL", "deepseek-v4-pro")
            key_pool = collect_api_keys(api_keys, api_key, os.environ.get("DEEPSEEK_API_KEYS", ""), os.environ.get("DEEPSEEK_API_KEY", ""))
            api_key = key_pool[0] if key_pool else ""
            base_url = (base_url or os.environ.get("DEEPSEEK_BASE_URL", "https://api.deepseek.com")).rstrip("/")
            if not api_key:
                raise LLMError("Missing DEEPSEEK_API_KEY or DEEPSEEK_API_KEYS.")
        elif provider in {"openai-compatible", "sub2api"}:
            model = model or os.environ.get("OPENAI_COMPATIBLE_MODEL", "qwen2.5-72b-instruct")
            env_key_values: list[str] = [
                os.environ.get("OPENAI_COMPATIBLE_API_KEYS", ""),
                os.environ.get("OPENAI_COMPATIBLE_API_KEY", ""),
            ]
            if provider == "openai-compatible":
                env_key_values.extend([os.environ.get("OPENAI_API_KEYS", ""), os.environ.get("OPENAI_API_KEY", "")])
            key_pool = collect_api_keys(api_keys, api_key, *env_key_values)
            api_key = key_pool[0] if key_pool else ""
            default_base = "http://127.0.0.1:8080" if provider == "sub2api" else "http://127.0.0.1:8000"
            base_url = (base_url or os.environ.get("OPENAI_COMPATIBLE_BASE_URL", default_base)).rstrip("/")
        elif provider == "mock":
            model = model or "mock"
            base_url = base_url or "mock://local"
        else:
            raise LLMError(f"Unknown LLM provider: {provider}")
        return cls(LLMConfig(provider=provider, model=model, api_key=api_key, api_keys=key_pool, base_url=base_url, timeout=timeout, retries=retries))

    def complete(
        self,
        prompt: str,
        system: str = "",
        max_tokens: int | None = 4096,
        temperature: float = 0.7,
        stream_callback: Callable[[str], None] | None = None,
    ) -> str:
        if self.config.provider == "mock":
            return self._mock_complete(prompt)

        last_error: Exception | None = None
        temporarily_disabled_key_indices: set[int] = set()
        attempts_left = max(1, len(self.config.api_keys) or 1)
        while attempts_left:
            attempts_left -= 1
            key_index = self._select_api_key(temporarily_disabled_key_indices)
            if key_index is None and self.config.api_keys:
                break
            for attempt in range(self.config.retries + 1):
                try:
                    if self.config.provider in {"openai", "openai-compatible", "sub2api", "deepseek"}:
                        return self._openai_chat(prompt, system, max_tokens, temperature, stream_callback=stream_callback)
                    if self.config.provider == "anthropic":
                        return self._anthropic_messages(prompt, system, max_tokens, temperature)
                except Exception as exc:  # noqa: BLE001 - provider errors are normalized below.
                    last_error = exc
                    if key_index is not None and self._should_rotate_key(exc):
                        if self._is_permanent_key_failure(exc):
                            self._disable_api_key(key_index, exc)
                        else:
                            self._temporarily_disable_api_key(key_index, exc, temporarily_disabled_key_indices)
                        break
                    if attempt >= self.config.retries:
                        break
                    time.sleep(2 * (attempt + 1))
            if not self.config.api_keys:
                break
        if self.config.api_keys and len(self._disabled_key_indices | temporarily_disabled_key_indices) >= len(self.config.api_keys):
            raise LLMError(f"All configured API keys are unavailable or exhausted. Last error: {last_error}") from last_error
        raise LLMError(f"LLM call failed: {last_error}") from last_error

    def _select_api_key(self, temporarily_disabled_key_indices: set[int] | None = None) -> int | None:
        if not self.config.api_keys:
            self._active_api_key = self.config.api_key
            return None
        temporarily_disabled_key_indices = temporarily_disabled_key_indices or set()
        for offset in range(len(self.config.api_keys)):
            index = (self._next_key_index + offset) % len(self.config.api_keys)
            if index not in self._disabled_key_indices and index not in temporarily_disabled_key_indices:
                self._next_key_index = (index + 1) % len(self.config.api_keys)
                self._active_api_key = self.config.api_keys[index]
                return index
        return None

    def _disable_api_key(self, index: int, exc: Exception) -> None:
        self._disabled_key_indices.add(index)
        self._next_key_index = (index + 1) % len(self.config.api_keys)
        detail = str(exc).replace("\n", " ")[:180]
        print(
            f"[key-pool] key #{index + 1} {mask_key(self.config.api_keys[index])} unavailable; switching. {detail}",
            file=sys.stderr,
        )

    def _temporarily_disable_api_key(self, index: int, exc: Exception, disabled: set[int]) -> None:
        disabled.add(index)
        self._next_key_index = (index + 1) % len(self.config.api_keys)
        detail = str(exc).replace("\n", " ")[:180]
        print(
            f"[key-pool] key #{index + 1} {mask_key(self.config.api_keys[index])} temporarily failed; switching for this request. {detail}",
            file=sys.stderr,
        )

    def _is_permanent_key_failure(self, exc: Exception) -> bool:
        if self.config.provider == "sub2api":
            if isinstance(exc, LLMHTTPError) and exc.status in {401, 403}:
                return not self._sub2api_active_key_is_accepted()
            detail = str(exc).lower()
            if any(marker in detail for marker in ("invalid api key", "invalid_api_key", "unauthorized", "forbidden")):
                return not self._sub2api_active_key_is_accepted()
            return False
        if isinstance(exc, LLMHTTPError) and exc.status in {401, 402, 403}:
            return True
        detail = str(exc).lower()
        return any(marker in detail for marker in ("invalid api key", "invalid_api_key", "insufficient_quota", "billing"))

    def _should_rotate_key(self, exc: Exception) -> bool:
        if self.config.provider == "sub2api":
            return self._should_rotate_sub2api_key(exc)
        if isinstance(exc, LLMHTTPError) and exc.status in {401, 402, 403, 429}:
            return True
        detail = str(exc).lower()
        return any(
            marker in detail
            for marker in (
                "insufficient_quota",
                "quota",
                "exhausted",
                "rate_limit",
                "rate limit",
                "too many requests",
                "invalid_api_key",
                "invalid api key",
                "unauthorized",
                "forbidden",
                "billing",
                "balance",
                "credit",
            )
        )

    def _should_rotate_sub2api_key(self, exc: Exception) -> bool:
        if isinstance(exc, LLMHTTPError):
            if exc.status in {401, 403}:
                return not self._sub2api_active_key_is_accepted()
            if exc.status in {402, 429, 500, 502, 503, 504, 524}:
                return True
            return False

        detail = str(exc).lower()
        if "invalid api key" in detail or "invalid_api_key" in detail or "unauthorized" in detail or "forbidden" in detail:
            return not self._sub2api_active_key_is_accepted()
        return any(
            marker in detail
            for marker in (
                "upstream_error",
                "upstream request failed",
                "timeout",
                "timed out",
                "rate limit",
                "too many requests",
                "bad gateway",
                "service unavailable",
                "gateway timeout",
                "cloudflare",
                "error 524",
                "ssl",
                "ssleoferror",
                "eof occurred",
                "connectionpool",
                "max retries exceeded",
                "connection aborted",
                "connection reset",
                "remote disconnected",
                "protocol",
                "502",
                "503",
                "504",
                "524",
            )
        )

    def _sub2api_active_key_is_accepted(self) -> bool:
        if not self._active_api_key or not self.config.base_url:
            return False
        headers = {**DEFAULT_HTTP_HEADERS, "Authorization": f"Bearer {self._active_api_key}"}
        if httpx is not None:
            try:
                response = httpx.get(
                    f"{self.config.base_url}/v1/models",
                    headers=headers,
                    timeout=min(self.config.timeout, 30),
                    trust_env=False,
                )
                return response.status_code not in {401, 403}
            except Exception:
                pass
        if requests is not None:
            try:
                response = requests.get(
                    f"{self.config.base_url}/v1/models",
                    headers=headers,
                    timeout=min(self.config.timeout, 30),
                )
                return response.status_code not in {401, 403}
            except Exception:
                return False
        request = urllib.request.Request(
            f"{self.config.base_url}/v1/models",
            headers=headers,
            method="GET",
        )
        try:
            with urllib.request.urlopen(request, timeout=min(self.config.timeout, 30)) as response:
                response.read(64)
                return True
        except urllib.error.HTTPError as exc:
            return exc.code not in {401, 403}
        except Exception:
            return False

    def _openai_chat(
        self,
        prompt: str,
        system: str,
        max_tokens: int | None,
        temperature: float,
        stream_callback: Callable[[str], None] | None = None,
    ) -> str:
        messages = []
        if system:
            messages.append({"role": "system", "content": system})
        messages.append({"role": "user", "content": prompt})
        body = {
            "model": self.config.model,
            "messages": messages,
            "temperature": temperature,
        }
        if max_tokens and max_tokens > 0:
            body["max_tokens"] = max_tokens
        headers = {"Content-Type": "application/json"}
        if self._active_api_key:
            headers["Authorization"] = f"Bearer {self._active_api_key}"
        if stream_callback:
            body["stream"] = True
            return self._post_openai_stream(f"{self.config.base_url}/v1/chat/completions", headers, body, stream_callback)
        data = self._post_json(f"{self.config.base_url}/v1/chat/completions", headers, body)
        try:
            return data["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError) as exc:
            raise LLMError(f"OpenAI-compatible response shape is unexpected: {data}") from exc

    def _anthropic_messages(self, prompt: str, system: str, max_tokens: int, temperature: float) -> str:
        body: dict[str, Any] = {
            "model": self.config.model,
            "max_tokens": max_tokens,
            "temperature": temperature,
            "messages": [{"role": "user", "content": prompt}],
        }
        if system:
            body["system"] = system
        headers = {
            "Content-Type": "application/json",
            "x-api-key": self._active_api_key,
            "anthropic-version": "2023-06-01",
        }
        data = self._post_json(f"{self.config.base_url}/v1/messages", headers, body)
        try:
            parts = data["content"]
            return "".join(part.get("text", "") for part in parts if part.get("type") == "text")
        except (KeyError, TypeError) as exc:
            raise LLMError(f"Anthropic response shape is unexpected: {data}") from exc

    def _post_json(self, url: str, headers: dict[str, str], body: dict[str, Any]) -> dict[str, Any]:
        request_headers = {**DEFAULT_HTTP_HEADERS, **headers}
        if self.config.provider == "sub2api" and httpx is not None:
            return self._post_json_httpx(url, request_headers, body)

        request_error: Exception | None = None
        if requests is not None:
            try:
                response = requests.post(url, headers=request_headers, json=body, timeout=self.config.timeout)
            except requests.RequestException as exc:
                request_error = exc
            else:
                if response.status_code >= 400:
                    raise LLMHTTPError(response.status_code, response.text)
                return response.json()

        if httpx is not None:
            try:
                return self._post_json_httpx(url, request_headers, body)
            except LLMError as exc:
                if request_error is not None:
                    raise LLMError(f"HTTP request failed: {request_error}; httpx fallback failed: {exc}") from exc
                raise

        request = urllib.request.Request(
            url,
            data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
            headers=request_headers,
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=self.config.timeout) as response:
                payload = response.read().decode("utf-8")
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="ignore")
            raise LLMHTTPError(exc.code, detail) from exc
        except Exception as exc:
            if request_error is not None:
                raise LLMError(f"HTTP request failed: {request_error}; urllib fallback failed: {exc}") from exc
            raise
        return json.loads(payload)

    def _post_json_httpx(self, url: str, headers: dict[str, str], body: dict[str, Any]) -> dict[str, Any]:
        if httpx is None:  # pragma: no cover - guarded by caller.
            raise LLMError("httpx is not installed.")
        try:
            response = httpx.post(url, headers=headers, json=body, timeout=self.config.timeout, trust_env=False)
        except httpx.HTTPError as exc:
            raise LLMError(f"HTTP request failed via httpx: {exc}") from exc
        if response.status_code >= 400:
            raise LLMHTTPError(response.status_code, response.text)
        return response.json()

    def _post_openai_stream(
        self,
        url: str,
        headers: dict[str, str],
        body: dict[str, Any],
        stream_callback: Callable[[str], None],
    ) -> str:
        request_headers = {**DEFAULT_HTTP_HEADERS, **headers, "Accept": "text/event-stream"}
        if self.config.provider == "sub2api" and httpx is not None:
            return self._post_openai_stream_httpx(url, request_headers, body, stream_callback)

        request_error: Exception | None = None
        if requests is not None:
            try:
                with requests.post(
                    url,
                    headers=request_headers,
                    json=body,
                    timeout=self.config.timeout,
                    stream=True,
                ) as response:
                    if response.status_code >= 400:
                        raise LLMHTTPError(response.status_code, response.text)
                    return self._consume_openai_sse(response.iter_lines(decode_unicode=False), stream_callback)
            except requests.RequestException as exc:
                request_error = exc

        if httpx is not None:
            try:
                return self._post_openai_stream_httpx(url, request_headers, body, stream_callback)
            except LLMError as exc:
                if request_error is not None:
                    raise LLMError(f"HTTP stream request failed: {request_error}; httpx fallback failed: {exc}") from exc
                raise

        request = urllib.request.Request(
            url,
            data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
            headers=request_headers,
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=self.config.timeout) as response:
                lines = (line.decode("utf-8", errors="ignore").rstrip("\r\n") for line in response)
                return self._consume_openai_sse(lines, stream_callback)
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="ignore")
            raise LLMHTTPError(exc.code, detail) from exc
        except Exception as exc:
            if request_error is not None:
                raise LLMError(f"HTTP stream request failed: {request_error}; urllib fallback failed: {exc}") from exc
            raise

    def _post_openai_stream_httpx(
        self,
        url: str,
        headers: dict[str, str],
        body: dict[str, Any],
        stream_callback: Callable[[str], None],
    ) -> str:
        if httpx is None:  # pragma: no cover - guarded by caller.
            raise LLMError("httpx is not installed.")
        try:
            with httpx.stream("POST", url, headers=headers, json=body, timeout=self.config.timeout, trust_env=False) as response:
                if response.status_code >= 400:
                    response.read()
                    raise LLMHTTPError(response.status_code, response.text)
                return self._consume_openai_sse(response.iter_lines(), stream_callback)
        except httpx.HTTPError as exc:
            raise LLMError(f"HTTP stream request failed via httpx: {exc}") from exc

    def _consume_openai_sse(self, lines: Any, stream_callback: Callable[[str], None]) -> str:
        chunks: list[str] = []
        for raw_line in lines:
            if raw_line is None:
                continue
            if isinstance(raw_line, bytes):
                line = raw_line.decode("utf-8", errors="replace").strip()
            else:
                line = str(raw_line).strip()
            if not line or line.startswith(":"):
                continue
            if line.startswith("{"):
                try:
                    data = json.loads(line)
                except json.JSONDecodeError:
                    continue
                content = ((data.get("choices") or [{}])[0].get("message") or {}).get("content") or ""
                if content:
                    chunks.append(content)
                    stream_callback(content)
                continue
            if not line.startswith("data:"):
                continue
            payload = line[5:].strip()
            if payload == "[DONE]":
                break
            try:
                data = json.loads(payload)
            except json.JSONDecodeError:
                continue
            choice = (data.get("choices") or [{}])[0]
            delta = choice.get("delta") or {}
            content = delta.get("content") or delta.get("text") or ""
            if not content:
                message = choice.get("message") or {}
                content = message.get("content") or ""
            if content:
                chunks.append(content)
                stream_callback(content)
        return "".join(chunks)

    def _mock_complete(self, prompt: str) -> str:
        if "FILE_BUNDLE" in prompt:
            return (
                '<file path="09_handoff/mock_output.md">\n'
                "# Mock Output\n\n"
                "This is a mock provider placeholder output for testing the automated flow.\n"
                "</file>"
            )
        return "Mock Output"
