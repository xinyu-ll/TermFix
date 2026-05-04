from __future__ import annotations

import sys
import types

import pytest

sys.modules.setdefault(
    "iterm2",
    types.SimpleNamespace(Connection=object, ScreenContents=object, Session=object),
)

from termfixlib.context import build_manual_system_prompt, build_user_message
from termfixlib.safety import (
    REDACTION_PLACEHOLDER,
    UnsafeInsertError,
    prepare_insert_text,
    redact_text,
    redacted_terminal_context,
    unsafe_insert_reason,
)


def test_redact_text_covers_common_terminal_secret_shapes():
    raw = (
        "curl -H 'Authorization: Bearer abcdefghijklmnop' "
        "--token=cli-token --api-key sk-cli-secret "
        "password=hunter2 SECRET='quoted secret' "
        "OPENAI_API_KEY=sk-1234567890abcdef"
    )

    redacted = redact_text(raw)

    assert "abcdefghijklmnop" not in redacted
    assert "cli-token" not in redacted
    assert "sk-cli-secret" not in redacted
    assert "hunter2" not in redacted
    assert "quoted secret" not in redacted
    assert "sk-1234567890abcdef" not in redacted
    assert redacted.count(REDACTION_PLACEHOLDER) >= 6
    assert "Authorization: Bearer [REDACTED]" in redacted
    assert "--token=[REDACTED]" in redacted
    assert "SECRET='[REDACTED]'" in redacted


def test_redact_text_is_conservative_for_non_secret_words():
    raw = "token bucket refill failed; passwordless sudo is disabled"

    assert redact_text(raw) == raw


def test_redacted_terminal_context_copies_and_redacts_only_sent_fields():
    context = {
        "command": "deploy --token=secret-token",
        "terminal_output": "Bearer abcdefghijklmnop\nstatus failed",
        "cwd": "/tmp/project",
    }

    redacted = redacted_terminal_context(context)

    assert redacted is not context
    assert context["command"] == "deploy --token=secret-token"
    assert context["terminal_output"].startswith("Bearer abc")
    assert redacted["command"] == "deploy --token=[REDACTED]"
    assert redacted["terminal_output"] == "Bearer [REDACTED]\nstatus failed"
    assert redacted["cwd"] == "/tmp/project"


def test_model_prompt_builders_redact_command_and_terminal_output():
    context = {
        "command": "curl --token=secret-token",
        "exit_code": 1,
        "terminal_output": "password=hunter2\nOPENAI_API_KEY=sk-1234567890abcdef",
        "terminal_output_line_count": 2,
        "cwd": "/tmp/project",
        "shell": "zsh",
        "os_name": "Darwin",
        "os_version": "25.0.0",
    }

    user_message = build_user_message(context)
    system_prompt = build_manual_system_prompt(context)

    for rendered in (user_message, system_prompt):
        assert "secret-token" not in rendered
        assert "hunter2" not in rendered
        assert "sk-1234567890abcdef" not in rendered
        assert "Context redaction active" in rendered
        assert REDACTION_PLACEHOLDER in rendered

    assert context["command"] == "curl --token=secret-token"
    assert "hunter2" in context["terminal_output"]


def test_prepare_insert_text_strips_final_newline_without_touching_body():
    assert prepare_insert_text("printf 'hi'\n# leave this\n") == "printf 'hi'\n# leave this"
    assert prepare_insert_text("echo hi\r\n") == "echo hi"


@pytest.mark.parametrize(
    "command",
    [
        "rm -rf /",
        "sudo rm -fr /",
        "git reset --hard",
        "chmod -R 777 /tmp/project",
    ],
)
def test_prepare_insert_text_blocks_obviously_dangerous_commands(command):
    with pytest.raises(UnsafeInsertError) as exc_info:
        prepare_insert_text(command)

    assert "Insert blocked:" in str(exc_info.value)
    assert "Use Copy" in str(exc_info.value)
    assert unsafe_insert_reason(command)
