from __future__ import annotations

import asyncio
import contextvars
import io
import json
import threading
import urllib.error

import pytest

from termfixlib import llm_client
from termfixlib.llm_client import (
    ApiError,
    _build_chat_messages,
    _chat_completions_url,
    _clean_markdown,
    _post_connection_test,
    _post_chat_completion,
    _post_chat_completion_stream,
    _remove_prefix,
    _run_blocking_in_thread,
    check_provider_connection,
)


class _FakeResponse:
    def __init__(self, body: dict | None = None, lines: list[bytes] | None = None) -> None:
        self.body = body or {}
        self.lines = lines or []

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, traceback):  # noqa: ANN001
        return False

    def read(self) -> bytes:
        return json.dumps(self.body).encode("utf-8")

    def __iter__(self):
        return iter(self.lines)


def _http_error(status_code: int, message: str = "try again") -> urllib.error.HTTPError:
    return urllib.error.HTTPError(
        "https://example.test/chat/completions",
        status_code,
        "status",
        {},
        io.BytesIO(json.dumps({"error": {"message": message}}).encode("utf-8")),
    )


@pytest.mark.parametrize(
    ("base_url", "expected"),
    [
        ("https://api.openai.com", "https://api.openai.com/v1/chat/completions"),
        ("https://api.example.com/v1", "https://api.example.com/v1/chat/completions"),
        (
            "https://proxy.example.com/openai/chat/completions",
            "https://proxy.example.com/openai/chat/completions",
        ),
        ("https://api.deepseek.com/", "https://api.deepseek.com/chat/completions"),
        ("", "https://api.deepseek.com/chat/completions"),
    ],
)
def test_chat_completions_url_normalizes_provider_base_urls(base_url, expected):
    assert _chat_completions_url(base_url) == expected


def test_build_chat_messages_filters_and_normalizes_history():
    messages = [
        {"role": "system", "content": "ignored"},
        {"role": "user", "content": [{"type": "text", "text": "hello"}]},
        {"role": "assistant", "content": ""},
        {"role": "assistant", "content": [{"type": "text", "text": "answer"}]},
        {"role": "tool", "content": "ignored"},
        "not a message",
    ]

    assert _build_chat_messages("system prompt", "fallback", messages) == [
        {"role": "system", "content": "system prompt"},
        {"role": "user", "content": "hello"},
        {"role": "assistant", "content": "answer"},
    ]


def test_build_chat_messages_falls_back_to_user_message_when_history_is_empty():
    assert _build_chat_messages("system prompt", "what failed?", [{"role": "system"}]) == [
        {"role": "system", "content": "system prompt"},
        {"role": "user", "content": "what failed?"},
    ]


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("```markdown\n### Fix\nRun it again.\n```", "### Fix\nRun it again."),
        ("```md\ntext\n```", "text"),
        ("  plain markdown  ", "plain markdown"),
    ],
)
def test_clean_markdown_removes_outer_response_fences(raw, expected):
    assert _clean_markdown(raw) == expected


def test_remove_prefix_removes_only_matching_prefix():
    assert _remove_prefix("```markdown\n### Fix", "```markdown") == "\n### Fix"
    assert _remove_prefix("```markdown\n### Fix", "```md") == "```markdown\n### Fix"
    assert _remove_prefix("plain text", "") == "plain text"


def test_run_blocking_in_thread_runs_callable_with_context_and_kwargs():
    request_id = contextvars.ContextVar("request_id", default="missing")
    request_id.set("ctx-123")
    caller_thread = threading.get_ident()

    def read_context(prefix, suffix=None):
        return threading.get_ident(), f"{prefix}:{request_id.get()}:{suffix}"

    worker_thread, result = asyncio.run(
        _run_blocking_in_thread(read_context, "value", suffix="done")
    )

    assert worker_thread != caller_thread
    assert result == "value:ctx-123:done"


def test_check_provider_connection_reports_missing_api_key_without_request(monkeypatch):
    def fail_urlopen(request, timeout):  # noqa: ANN001
        raise AssertionError("connection test should not send a request")

    monkeypatch.setattr(llm_client, "_urlopen", fail_urlopen)

    result = check_provider_connection("", "https://api.example.test", "model")

    assert result == {
        "ok": False,
        "kind": "missing_api_key",
        "error": "API key is not configured. Set the API Key knob before testing.",
    }


def test_post_connection_test_uses_configured_url_header_payload_and_timeout(monkeypatch):
    captured = {}

    def fake_urlopen(request, timeout):  # noqa: ANN001
        captured["url"] = request.full_url
        captured["authorization"] = request.get_header("Authorization")
        captured["content_type"] = request.get_header("Content-type")
        captured["user_agent"] = request.get_header("User-agent")
        captured["payload"] = json.loads(request.data.decode("utf-8"))
        captured["timeout"] = timeout
        return _FakeResponse({"choices": [{"message": {"content": "O"}}]})

    monkeypatch.setattr(llm_client, "_urlopen", fake_urlopen)

    _post_connection_test("sk-secret", "https://api.example.test/v1", "model-x")

    assert captured["url"] == "https://api.example.test/v1/chat/completions"
    assert captured["authorization"] == "Bearer sk-secret"
    assert captured["content_type"] == "application/json"
    assert captured["user_agent"] == "TermFix/1.0"
    assert captured["timeout"] == 8
    assert captured["payload"]["model"] == "model-x"
    assert captured["payload"]["max_tokens"] == 2
    assert captured["payload"]["stream"] is False
    assert captured["payload"]["messages"] == [
        {"role": "system", "content": "You are a connection test. Reply with OK."},
        {"role": "user", "content": "Reply with OK."},
    ]


def test_check_provider_connection_returns_structured_error_and_redacts_key(monkeypatch, caplog):
    def fake_urlopen(request, timeout):  # noqa: ANN001
        raise _http_error(500, "provider echoed sk-secret")

    monkeypatch.setattr(llm_client, "_urlopen", fake_urlopen)

    with caplog.at_level("WARNING", logger="termfixlib.llm_client"):
        result = check_provider_connection(
            "sk-secret",
            "https://api.example.test",
            "model-x",
        )

    assert result == {
        "ok": False,
        "kind": "api",
        "status_code": 500,
        "error": "provider echoed [redacted]",
    }
    assert "sk-secret" not in "\n".join(record.getMessage() for record in caplog.records)


def test_post_chat_completion_retries_transient_network_error(monkeypatch):
    calls = 0
    delays = []

    def fake_urlopen(request, timeout):  # noqa: ANN001
        nonlocal calls
        calls += 1
        if calls == 1:
            raise urllib.error.URLError("temporary dns failure")
        return _FakeResponse({"choices": [{"message": {"content": "fixed"}}]})

    monkeypatch.setattr(llm_client, "_urlopen", fake_urlopen)
    monkeypatch.setattr(llm_client, "_sleep_before_retry", delays.append)

    result = _post_chat_completion("key", "https://api.example.test", "model", "help")

    assert result == "fixed"
    assert calls == 2
    assert delays == [1.0]


def test_post_chat_completion_does_not_retry_auth_error(monkeypatch):
    calls = 0
    delays = []

    def fake_urlopen(request, timeout):  # noqa: ANN001
        nonlocal calls
        calls += 1
        raise _http_error(401, "bad key")

    monkeypatch.setattr(llm_client, "_urlopen", fake_urlopen)
    monkeypatch.setattr(llm_client, "_sleep_before_retry", delays.append)

    with pytest.raises(ApiError) as exc_info:
        _post_chat_completion("key", "https://api.example.test", "model", "help")

    assert exc_info.value.status_code == 401
    assert calls == 1
    assert delays == []


def test_post_chat_completion_uses_configured_max_tokens(monkeypatch):
    captured_payload = {}

    def fake_urlopen(request, timeout):  # noqa: ANN001
        nonlocal captured_payload
        captured_payload = json.loads(request.data.decode("utf-8"))
        return _FakeResponse({"choices": [{"message": {"content": "fixed"}}]})

    monkeypatch.setattr(llm_client, "_urlopen", fake_urlopen)

    result = _post_chat_completion(
        "key",
        "https://api.example.test",
        "model",
        "help",
        max_tokens=4096,
    )

    assert result == "fixed"
    assert captured_payload["max_tokens"] == 4096


def test_post_chat_completion_stream_retries_transient_status(monkeypatch):
    calls = 0
    delays = []

    def fake_urlopen(request, timeout):  # noqa: ANN001
        nonlocal calls
        calls += 1
        if calls == 1:
            raise _http_error(503, "overloaded")
        return _FakeResponse(
            lines=[
                b'data: {"choices":[{"delta":{"content":"hello"}}]}\n',
                b'data: {"choices":[{"delta":{"content":" world"}}]}\n',
                b"data: [DONE]\n",
            ]
        )

    monkeypatch.setattr(llm_client, "_urlopen", fake_urlopen)
    monkeypatch.setattr(llm_client, "_sleep_before_retry", delays.append)

    snapshots = list(
        _post_chat_completion_stream("key", "https://api.example.test", "model", "help")
    )

    assert snapshots == ["hello", "hello world"]
    assert calls == 2
    assert delays == [1.0]


def test_post_chat_completion_stream_uses_configured_max_tokens(monkeypatch):
    captured_payload = {}

    def fake_urlopen(request, timeout):  # noqa: ANN001
        nonlocal captured_payload
        captured_payload = json.loads(request.data.decode("utf-8"))
        return _FakeResponse(
            lines=[
                b'data: {"choices":[{"delta":{"content":"hello"}}]}\n',
                b"data: [DONE]\n",
            ]
        )

    monkeypatch.setattr(llm_client, "_urlopen", fake_urlopen)

    snapshots = list(
        _post_chat_completion_stream(
            "key",
            "https://api.example.test",
            "model",
            "help",
            max_tokens=8192,
        )
    )

    assert snapshots == ["hello"]
    assert captured_payload["max_tokens"] == 8192
