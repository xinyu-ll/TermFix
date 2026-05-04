"""LLM client for TermFix using only the Python standard library."""

from __future__ import annotations

import asyncio
import contextvars
import functools
import json
import logging
import os
import socket
import ssl
import threading
import time
import urllib.error
import urllib.request
import urllib.parse
from typing import Any, AsyncIterator, Callable, Optional, TypeVar

from .config import DEFAULT_BASE_URL, DEFAULT_MAX_TOKENS, DEFAULT_MODEL, SYSTEM_PROMPT
from .context import build_manual_system_prompt, build_user_message

logger = logging.getLogger(__name__)

AnalysisResult = str
_ResultT = TypeVar("_ResultT")

_EMPTY_RESULT: AnalysisResult = """\
### Cause
Analysis unavailable.

### Fix
Check your API key and network connection.

### Details
Could not contact the API.
"""

_USER_AGENT = "TermFix/1.0"
_MACOS_CA_FILE = "/private/etc/ssl/cert.pem"
_TRANSIENT_RETRY_DELAYS = (1.0, 2.0)
_TRANSIENT_HTTP_STATUS_CODES = {429, 503}
_CONNECTION_TEST_MAX_TOKENS = 2
_CONNECTION_TEST_TIMEOUT = 8
_CONNECTION_TEST_SYSTEM_PROMPT = "You are a connection test. Reply with OK."
_CONNECTION_TEST_USER_MESSAGE = "Reply with OK."
_TRANSIENT_NETWORK_ERRORS = (
    urllib.error.URLError,
    ConnectionError,
    ConnectionResetError,
    ConnectionAbortedError,
    TimeoutError,
    socket.timeout,
)
_SSL_CONTEXT_UNSET = object()
_ssl_ctx_cache = _SSL_CONTEXT_UNSET


async def _run_blocking_in_thread(
    func: Callable[..., _ResultT],
    *args: Any,
    **kwargs: Any,
) -> _ResultT:
    """Run blocking work on the default executor while preserving context."""
    loop = asyncio.get_running_loop()
    context = contextvars.copy_context()
    call = functools.partial(context.run, func, *args, **kwargs)
    return await loop.run_in_executor(None, call)


def _remove_prefix(value: str, prefix: str) -> str:
    """Return value without prefix when present."""
    if value.startswith(prefix):
        return value[len(prefix) :]
    return value


class ApiError(Exception):
    """Represents a non-2xx API response or malformed API payload."""

    def __init__(self, status_code: int, message: str) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.message = message


def check_provider_connection(
    api_key: str,
    base_url: str = DEFAULT_BASE_URL,
    model: str = DEFAULT_MODEL,
) -> dict[str, Any]:
    """Send a minimal chat-completions request and return JSON-safe status."""
    if not api_key:
        return {
            "ok": False,
            "kind": "missing_api_key",
            "error": "API key is not configured. Set the API Key knob before testing.",
        }

    safe_base_url = _redact_secret(_normalise_base_url(base_url), api_key)
    safe_model = _redact_secret(model, api_key)
    try:
        logger.info("Testing LLM provider connection via %s model=%s", safe_base_url, safe_model)
        _post_connection_test(api_key, base_url, model)
    except ApiError as exc:
        error = _connection_test_api_error(exc, api_key)
        logger.warning(
            "LLM provider connection test failed with status %s: %s",
            exc.status_code,
            error,
        )
        return {
            "ok": False,
            "kind": _connection_test_error_kind(exc.status_code),
            "status_code": exc.status_code,
            "error": error,
        }
    except _TRANSIENT_NETWORK_ERRORS as exc:
        error = _redact_secret(str(getattr(exc, "reason", exc)), api_key)
        logger.warning("LLM provider connection test network failure: %s", error)
        return {
            "ok": False,
            "kind": "network",
            "error": f"Network error reaching provider: {error}",
        }
    except Exception as exc:
        error = _redact_secret(str(exc), api_key)
        logger.warning("LLM provider connection test failed: %s", error)
        return {
            "ok": False,
            "kind": "unexpected",
            "error": error or "Connection test failed.",
        }

    logger.info("LLM provider connection test succeeded via %s model=%s", safe_base_url, safe_model)
    return {
        "ok": True,
        "message": "Connection succeeded.",
        "base_url": safe_base_url,
        "model": safe_model,
    }


async def analyze_error(
    context: dict,
    api_key: str,
    base_url: str = DEFAULT_BASE_URL,
    model: str = DEFAULT_MODEL,
    max_tokens: int = DEFAULT_MAX_TOKENS,
) -> AnalysisResult:
    """Call an OpenAI-compatible endpoint and return Markdown."""
    result = ""
    async for snapshot in stream_analyze_error(
        context,
        api_key=api_key,
        base_url=base_url,
        model=model,
        max_tokens=max_tokens,
    ):
        result = snapshot
    return result or _EMPTY_RESULT


async def stream_analyze_error(
    context: dict,
    api_key: str,
    base_url: str = DEFAULT_BASE_URL,
    model: str = DEFAULT_MODEL,
    max_tokens: int = DEFAULT_MAX_TOKENS,
) -> AsyncIterator[AnalysisResult]:
    """Call an OpenAI-compatible endpoint and yield cumulative Markdown snapshots."""
    if not api_key:
        logger.warning("No API key configured — skipping LLM analysis")
        yield """\
### Cause
No API key is configured.

### Fix
Configure the `API Key` knob on the TermFix status bar component.

### Details
TermFix needs a provider API key before it can analyze terminal errors.
"""
        return

    user_message = build_user_message(context)

    try:
        logger.info("Starting LLM analysis via %s model=%s", base_url, model)
        result = ""
        async for snapshot in _stream_api(api_key, base_url, model, user_message, max_tokens=max_tokens):
            result = snapshot
            yield snapshot
        logger.info("LLM analysis completed (%d chars)", len(result))
    except ApiError as exc:
        if exc.status_code == 401:
            logger.error("Authentication failed — check API key")
            yield _error_markdown("Authentication failed.", "Verify your API key.")
            return
        if exc.status_code == 429:
            logger.warning("Rate limit hit")
            yield _error_markdown("Rate limited by API.", "Wait and try again.")
            return
        logger.error("API status error %s: %s", exc.status_code, exc.message)
        yield _error_markdown(f"API error {exc.status_code}.", exc.message)
    except urllib.error.URLError as exc:
        logger.error("Network error reaching API: %s", exc)
        yield _error_markdown("Network error.", str(exc.reason))
    except Exception as exc:
        logger.exception("Unexpected error during LLM analysis")
        yield _error_markdown("Unexpected error.", str(exc))


async def stream_user_prompt(
    context: dict,
    user_prompt: str,
    api_key: str,
    base_url: str = DEFAULT_BASE_URL,
    model: str = DEFAULT_MODEL,
    messages: Optional[list[dict]] = None,
    max_tokens: int = DEFAULT_MAX_TOKENS,
) -> AsyncIterator[AnalysisResult]:
    """Call the model with terminal context in system prompt and user text/history."""
    if not api_key:
        logger.warning("No API key configured — skipping user prompt")
        yield """\
### Cause
No API key is configured.

### Fix
Configure the `API Key` knob on the TermFix status bar component.

### Details
TermFix needs a provider API key before it can answer a prompt.
"""
        return

    system_prompt = build_manual_system_prompt(context)

    try:
        logger.info("Starting user prompt via %s model=%s", base_url, model)
        result = ""
        async for snapshot in _stream_api(
            api_key,
            base_url,
            model,
            user_prompt,
            system_prompt,
            max_tokens=max_tokens,
            messages=messages,
        ):
            result = snapshot
            yield snapshot
        logger.info("User prompt completed (%d chars)", len(result))
    except ApiError as exc:
        if exc.status_code == 401:
            logger.error("Authentication failed — check API key")
            yield _error_markdown("Authentication failed.", "Verify your API key.")
            return
        if exc.status_code == 429:
            logger.warning("Rate limit hit")
            yield _error_markdown("Rate limited by API.", "Wait and try again.")
            return
        logger.error("API status error %s: %s", exc.status_code, exc.message)
        yield _error_markdown(f"API error {exc.status_code}.", exc.message)
    except urllib.error.URLError as exc:
        logger.error("Network error reaching API: %s", exc)
        yield _error_markdown("Network error.", str(exc.reason))
    except Exception as exc:
        logger.exception("Unexpected error during user prompt")
        yield _error_markdown("Unexpected error.", str(exc))


async def _call_api(
    api_key: str,
    base_url: str,
    model: str,
    user_message: str,
    system_prompt: str = SYSTEM_PROMPT,
    messages: Optional[list[dict]] = None,
    max_tokens: int = DEFAULT_MAX_TOKENS,
) -> AnalysisResult:
    """Make the actual API call and return Markdown."""
    response_text = await _run_blocking_in_thread(
        _post_chat_completion,
        api_key,
        base_url,
        model,
        user_message,
        system_prompt,
        messages,
        max_tokens,
    )

    logger.debug("LLM response received (%d chars)", len(response_text))
    return _clean_markdown(response_text)


async def _stream_api(
    api_key: str,
    base_url: str,
    model: str,
    user_message: str,
    system_prompt: str = SYSTEM_PROMPT,
    messages: Optional[list[dict]] = None,
    max_tokens: int = DEFAULT_MAX_TOKENS,
) -> AsyncIterator[AnalysisResult]:
    """Stream cumulative Markdown snapshots from the provider without blocking the loop."""
    loop = asyncio.get_running_loop()
    queue = asyncio.Queue()

    def emit(kind: str, value=None) -> None:
        loop.call_soon_threadsafe(queue.put_nowait, (kind, value))

    def worker() -> None:
        try:
            for snapshot in _post_chat_completion_stream(
                api_key,
                base_url,
                model,
                user_message,
                system_prompt,
                messages,
                max_tokens,
            ):
                emit("snapshot", snapshot)
            emit("done")
        except BaseException as exc:  # noqa: BLE001 - forwarded to async caller.
            emit("error", exc)

    thread = threading.Thread(target=worker, name="termfix-llm-stream", daemon=True)
    thread.start()

    while True:
        kind, value = await queue.get()
        if kind == "snapshot":
            yield _clean_markdown(str(value or ""))
        elif kind == "done":
            return
        elif kind == "error":
            assert isinstance(value, BaseException)
            raise value


def _post_chat_completion(
    api_key: str,
    base_url: str,
    model: str,
    user_message: str,
    system_prompt: str = SYSTEM_PROMPT,
    messages: Optional[list[dict]] = None,
    max_tokens: int = DEFAULT_MAX_TOKENS,
) -> str:
    """Send a chat-completions request using urllib."""
    payload = {
        "model": model,
        "max_tokens": max_tokens,
        "temperature": 0.1,
        "stream": False,
        "messages": _build_chat_messages(system_prompt, user_message, messages),
    }
    request = urllib.request.Request(
        url=_chat_completions_url(base_url),
        data=json.dumps(payload).encode("utf-8"),
        headers=_request_headers(api_key),
        method="POST",
    )

    attempt = 0
    while True:
        try:
            with _urlopen(request, timeout=45) as response:
                body = response.read().decode("utf-8")
            break
        except urllib.error.HTTPError as exc:
            body = exc.read().decode("utf-8", errors="replace")
            api_error = ApiError(exc.code, _extract_error_message(body))
            if _retry_transient_failure(api_error, attempt, "LLM request"):
                attempt += 1
                continue
            raise api_error from exc
        except _TRANSIENT_NETWORK_ERRORS as exc:
            if _retry_transient_failure(exc, attempt, "LLM request"):
                attempt += 1
                continue
            raise

    try:
        parsed = json.loads(body)
    except json.JSONDecodeError as exc:
        raise ApiError(502, f"Invalid JSON from provider: {body[:200]}") from exc

    message = parsed.get("choices", [{}])[0].get("message", {})
    content = _extract_content(message.get("content"))
    if not content:
        raise ApiError(502, "Provider response did not include message content.")
    return content


def _post_chat_completion_stream(
    api_key: str,
    base_url: str,
    model: str,
    user_message: str,
    system_prompt: str = SYSTEM_PROMPT,
    messages: Optional[list[dict]] = None,
    max_tokens: int = DEFAULT_MAX_TOKENS,
):
    """Send a streaming chat-completions request and yield cumulative content."""
    payload = {
        "model": model,
        "max_tokens": max_tokens,
        "temperature": 0.1,
        "stream": True,
        "messages": _build_chat_messages(system_prompt, user_message, messages),
    }
    request = urllib.request.Request(
        url=_chat_completions_url(base_url),
        data=json.dumps(payload).encode("utf-8"),
        headers=_request_headers(api_key, accept="text/event-stream"),
        method="POST",
    )

    attempt = 0
    while True:
        content_parts: list[str] = []
        yielded = False

        try:
            with _urlopen(request, timeout=60) as response:
                for raw_line in response:
                    line = raw_line.decode("utf-8", errors="replace").strip()
                    if not line or line.startswith(":"):
                        continue
                    if not line.startswith("data:"):
                        continue

                    data = line[5:].strip()
                    if data == "[DONE]":
                        break

                    try:
                        parsed = json.loads(data)
                    except json.JSONDecodeError:
                        logger.debug("Ignoring invalid SSE chunk: %r", data[:200])
                        continue

                    choice = parsed.get("choices", [{}])[0]
                    delta = choice.get("delta") or choice.get("message") or {}
                    piece = _extract_content(delta.get("content"))
                    if piece:
                        content_parts.append(piece)
                        yielded = True
                        yield "".join(content_parts)
            if not yielded:
                raise ApiError(502, "Provider streaming response did not include content.")
            return
        except urllib.error.HTTPError as exc:
            body = exc.read().decode("utf-8", errors="replace")
            api_error = ApiError(exc.code, _extract_error_message(body))
            if not yielded and _retry_transient_failure(api_error, attempt, "LLM stream"):
                attempt += 1
                continue
            raise api_error from exc
        except _TRANSIENT_NETWORK_ERRORS as exc:
            if not yielded and _retry_transient_failure(exc, attempt, "LLM stream"):
                attempt += 1
                continue
            raise


def _post_connection_test(
    api_key: str,
    base_url: str,
    model: str,
    timeout: int = _CONNECTION_TEST_TIMEOUT,
) -> None:
    """Send one minimal non-streaming request to validate provider settings."""
    payload = {
        "model": model,
        "max_tokens": _CONNECTION_TEST_MAX_TOKENS,
        "temperature": 0,
        "stream": False,
        "messages": _build_chat_messages(
            _CONNECTION_TEST_SYSTEM_PROMPT,
            _CONNECTION_TEST_USER_MESSAGE,
        ),
    }
    request = urllib.request.Request(
        url=_chat_completions_url(base_url),
        data=json.dumps(payload).encode("utf-8"),
        headers=_request_headers(api_key),
        method="POST",
    )

    try:
        with _urlopen(request, timeout=timeout) as response:
            body = response.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        raise ApiError(exc.code, _extract_error_message(body)) from exc

    try:
        parsed = json.loads(body)
    except json.JSONDecodeError as exc:
        raise ApiError(502, f"Invalid JSON from provider: {body[:200]}") from exc

    if not isinstance(parsed.get("choices"), list):
        raise ApiError(502, "Provider response did not include chat completion choices.")


def _retry_transient_failure(exc: BaseException, attempt: int, operation: str) -> bool:
    """Sleep before retrying provider failures that are commonly transient."""
    if attempt >= len(_TRANSIENT_RETRY_DELAYS):
        return False
    if isinstance(exc, ApiError):
        if exc.status_code not in _TRANSIENT_HTTP_STATUS_CODES:
            return False
    elif not isinstance(exc, _TRANSIENT_NETWORK_ERRORS):
        return False

    delay = _TRANSIENT_RETRY_DELAYS[attempt]
    logger.warning(
        "%s failed transiently on attempt %d/%d; retrying in %.1fs: %s",
        operation,
        attempt + 1,
        len(_TRANSIENT_RETRY_DELAYS) + 1,
        delay,
        exc,
    )
    _sleep_before_retry(delay)
    return True


def _sleep_before_retry(delay: float) -> None:
    time.sleep(delay)


def _build_chat_messages(
    system_prompt: str,
    user_message: str,
    messages: Optional[list[dict]] = None,
) -> list[dict]:
    """Build a provider-safe message list from optional conversation history."""
    chat_messages = [{"role": "system", "content": system_prompt}]
    source_messages = messages or [{"role": "user", "content": user_message}]

    for message in source_messages:
        if not isinstance(message, dict):
            continue
        role = message.get("role")
        if role not in ("user", "assistant"):
            continue
        content = _extract_content(message.get("content"))
        if not content:
            content = str(message.get("content") or "")
        if content.strip():
            chat_messages.append({"role": role, "content": content})

    if len(chat_messages) == 1 and user_message.strip():
        chat_messages.append({"role": "user", "content": user_message})

    return chat_messages


def _extract_content(content) -> str:
    """Normalize OpenAI-compatible content payloads into plain text."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for item in content:
            if isinstance(item, dict) and item.get("type") == "text":
                parts.append(str(item.get("text", "")))
        return "".join(parts)
    return ""


def _request_headers(api_key: str, accept: Optional[str] = None) -> dict[str, str]:
    """Build headers without urllib's default Python User-Agent.

    Some OpenAI-compatible proxy providers sit behind Cloudflare rules that
    reject Python-urllib's default User-Agent with HTTP 403.
    """
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
        "User-Agent": _USER_AGENT,
    }
    if accept:
        headers["Accept"] = accept
    return headers


def _urlopen(request: urllib.request.Request, timeout: int):
    """Open a request with a macOS CA fallback for iTerm2's embedded Python."""
    context = _ssl_context()
    if context is None:
        return urllib.request.urlopen(request, timeout=timeout)
    return urllib.request.urlopen(request, timeout=timeout, context=context)


def _ssl_context() -> Optional[ssl.SSLContext]:
    global _ssl_ctx_cache
    if _ssl_ctx_cache is not _SSL_CONTEXT_UNSET:
        return _ssl_ctx_cache

    paths = ssl.get_default_verify_paths()
    if paths.cafile and os.path.exists(paths.cafile):
        _ssl_ctx_cache = None
        return _ssl_ctx_cache
    if os.path.exists(_MACOS_CA_FILE):
        _ssl_ctx_cache = ssl.create_default_context(cafile=_MACOS_CA_FILE)
        return _ssl_ctx_cache
    _ssl_ctx_cache = None
    return _ssl_ctx_cache


def _extract_error_message(body: str) -> str:
    """Best-effort extraction of an error message from an API error payload."""
    try:
        parsed = json.loads(body)
    except json.JSONDecodeError:
        return body[:200] or "Unknown API error."

    error = parsed.get("error", {})
    if isinstance(error, dict):
        return str(error.get("message") or body[:200] or "Unknown API error.")
    return str(error or body[:200] or "Unknown API error.")


def _clean_markdown(raw: str) -> str:
    """Strip accidental wrapper fences around a Markdown response."""
    text = raw.strip()

    if text.startswith("```markdown"):
        text = _remove_prefix(text, "```markdown").strip()
    elif text.startswith("```md"):
        text = _remove_prefix(text, "```md").strip()

    if text.endswith("```"):
        text = text[:-3].strip()

    return text


def _error_markdown(cause: str, details: str) -> str:
    """Return a Markdown error response for local/client-side failures."""
    return f"""\
### Cause
{cause}

### Fix
Check your TermFix API settings and try again.

### Details
{details}
"""


def _connection_test_api_error(exc: ApiError, api_key: str) -> str:
    message = _redact_secret(exc.message, api_key)
    if exc.status_code in (401, 403):
        return "Authentication failed. Verify your API key and provider permissions."
    if exc.status_code == 404:
        return f"Provider endpoint was not found. Check the Base URL. Details: {message}"
    if exc.status_code == 429:
        return "Provider rate limit reached. Wait and try again."
    return message or f"Provider returned HTTP {exc.status_code}."


def _connection_test_error_kind(status_code: int) -> str:
    if status_code in (401, 403):
        return "auth"
    if status_code == 404:
        return "endpoint"
    if status_code == 429:
        return "rate_limit"
    return "api"


def _redact_secret(text: str, secret: str) -> str:
    if not text or not secret:
        return text
    return text.replace(secret, "[redacted]")


def _normalise_base_url(base_url: str) -> str:
    """Accept provider URLs with or without a trailing slash."""
    cleaned = (base_url or "").strip()
    if not cleaned:
        return DEFAULT_BASE_URL
    return cleaned.rstrip("/")


def _chat_completions_url(base_url: str) -> str:
    """Resolve a user-provided base URL into a chat-completions endpoint."""
    cleaned = _normalise_base_url(base_url)
    if cleaned.endswith("/chat/completions"):
        return cleaned
    if cleaned.endswith("/v1"):
        return f"{cleaned}/chat/completions"

    parsed = urllib.parse.urlparse(cleaned)
    path = parsed.path.rstrip("/")
    if not path and parsed.netloc == "api.openai.com":
        return f"{cleaned}/v1/chat/completions"
    return f"{cleaned}/chat/completions"
