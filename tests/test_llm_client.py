import asyncio
import contextvars
import threading

import pytest

from termfixlib.llm_client import (
    _build_chat_messages,
    _chat_completions_url,
    _clean_markdown,
    _remove_prefix,
    _run_blocking_in_thread,
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
