import asyncio
import logging
import sys
import types
from types import SimpleNamespace

sys.modules.setdefault("iterm2", types.ModuleType("iterm2"))

from termfixlib.config import DEFAULT_MAX_TOKENS
from termfixlib.ui import (
    _build_info_html,
    _build_live_html,
    _build_prompt_html,
    _compact_text,
    _conversation_to_html,
    _insert_code_text,
    _markdown_to_html,
    _sync_knobs,
)


class FakeSession:
    def __init__(self, session_id="session-1"):
        self.session_id = session_id
        self.sent = []

    async def async_send_text(self, text, suppress_broadcast=False):
        self.sent.append((text, suppress_broadcast))


def test_markdown_to_html_renders_safe_subset_and_escapes_html():
    rendered = _markdown_to_html(
        "### Fix\n"
        "Run `python -m pytest` and use **safe** flags.\n"
        "- Escape <script>alert(1)</script>\n"
        "```sh\n"
        "echo '<unsafe>'\n"
        "```"
    )

    assert "<h3>Fix</h3>" in rendered
    assert "<code>python -m pytest</code>" in rendered
    assert "<strong>safe</strong>" in rendered
    assert "<li>Escape &lt;script&gt;alert(1)&lt;/script&gt;</li>" in rendered
    assert '<button class="code-action copy-code" type="button" data-copy-code>Copy</button>' in rendered
    assert '<button class="code-action insert-code" type="button" data-insert-code>Insert</button>' in rendered
    assert "echo &#x27;&lt;unsafe&gt;&#x27;" in rendered
    assert "<script>" not in rendered


def test_compact_text_removes_markdown_markers_without_destroying_hyphenated_terms():
    compact = _compact_text(
        "### Fix\n"
        "> - Run `apt-get install non-zero --force`\n"
        "1. Keep well-known flags\n"
        "```sh\n"
        "echo done\n"
        "```"
    )

    assert "apt-get install non-zero --force" in compact
    assert "Keep well-known flags" in compact
    assert "```" not in compact


def test_markdown_to_html_keeps_ordered_and_unordered_lists_separate():
    rendered = _markdown_to_html(
        "Before\n"
        "1. First\n"
        "2. Second\n"
        "- Bullet\n"
        "After"
    )

    assert "<p>Before</p>" in rendered
    assert "<ol><li>First</li><li>Second</li></ol>" in rendered
    assert "<ul><li>Bullet</li></ul>" in rendered
    assert rendered.index("</ol>") < rendered.index("<ul>")
    assert rendered.endswith("<p>After</p>")


def test_code_block_copy_controls_are_shared_by_live_and_prompt_popovers():
    state = SimpleNamespace(
        status_server_url="http://127.0.0.1:9",
        status_server_token="test-token",
        prompts=[],
        context_lines=12,
        fix_hotkey="Cmd+J",
        prompt_hotkey="Cmd+L",
    )
    live_entry = SimpleNamespace(
        id="error-1",
        session_id="session-1",
        command="pytest",
        exit_code=1,
        result="```sh\npytest tests\n```",
        status="done",
    )
    prompt_entry = SimpleNamespace(
        id="prompt-1",
        session_id="session-1",
        context={"context_lines": 8},
        messages=[
            {"role": "assistant", "content": "```sh\necho copied\n```"},
        ],
        result="",
        status="done",
        timestamp=1,
    )
    state.prompts = [prompt_entry]

    live_html = _build_live_html(live_entry, state)
    prompt_html = _build_prompt_html(prompt_entry, state)

    shared_markers = [
        ".code-action.copied",
        ".code-action.inserted",
        ".code-action.needs-manual-copy",
        "async function copyText(text)",
        "function selectCodeText(code)",
        "async function insertCodeText(text)",
        "function handleCodeBlockCopy(event)",
        '<button class="code-action copy-code" type="button" data-copy-code>Copy</button>',
        '<button class="code-action insert-code" type="button" data-insert-code>Insert</button>',
        "/test-token/insert?session=session-1",
    ]
    for marker in shared_markers:
        assert marker in live_html
        assert marker in prompt_html

    assert 'contentEl.addEventListener("click", handleCodeBlockCopy);' in live_html
    assert "if (handleCodeBlockCopy(event))" in prompt_html


def test_insert_code_text_sends_exact_text_to_target_session():
    session = FakeSession()
    state = SimpleNamespace(
        terminal_sessions={"session-1": session},
        prompt_sessions={},
        connection=None,
    )

    result = asyncio.run(_insert_code_text("printf 'hi'\n# no enter", state, "session-1"))

    assert result == {"ok": True, "session_id": "session-1"}
    assert session.sent == [("printf 'hi'\n# no enter", True)]


def test_insert_code_text_reports_missing_session():
    state = SimpleNamespace(terminal_sessions={}, prompt_sessions={}, connection=None)

    result = asyncio.run(_insert_code_text("echo hi", state, "missing"))

    assert result["ok"] is False
    assert "unavailable" in result["error"]


def test_live_popover_slows_polling_after_analysis_done():
    state = SimpleNamespace(
        status_server_url="http://127.0.0.1:9",
        status_server_token="test-token",
        prompts=[],
        context_lines=12,
    )
    live_entry = SimpleNamespace(
        id="error-1",
        command="pytest",
        exit_code=1,
        result="Done",
        status="done",
    )

    live_html = _build_live_html(live_entry, state)

    assert "let pollDelay = 350;" in live_html
    assert "const nextDelay = data.done ? 2000 : 350;" in live_html
    assert "timer = setInterval(refresh, pollDelay);" in live_html


def test_live_and_prompt_popovers_include_dark_mode_variables():
    state = SimpleNamespace(
        status_server_url="http://127.0.0.1:9",
        status_server_token="test-token",
        prompts=[],
        context_lines=12,
    )
    live_entry = SimpleNamespace(
        id="error-1",
        command="pytest",
        exit_code=1,
        result="Analyzing...",
        status="streaming",
    )
    prompt_entry = SimpleNamespace(
        id="prompt-1",
        session_id="session-1",
        context={"context_lines": 8},
        messages=[],
        result="",
        status="input",
        timestamp=1,
    )
    state.prompts = [prompt_entry]

    live_html = _build_live_html(live_entry, state)
    prompt_html = _build_prompt_html(prompt_entry, state)

    for rendered in (live_html, prompt_html):
        assert "@media (prefers-color-scheme: dark)" in rendered
        assert "--paper:" in rendered
        assert "--ink:" in rendered
        assert "--field:" in rendered


def test_prompt_popover_removes_faux_chrome_and_scanline_noise():
    state = SimpleNamespace(
        status_server_url="http://127.0.0.1:9",
        status_server_token="test-token",
        prompts=[],
        context_lines=12,
    )
    prompt_entry = SimpleNamespace(
        id="prompt-1",
        session_id="session-1",
        context={"context_lines": 8},
        messages=[],
        result="",
        status="input",
        timestamp=1,
    )
    state.prompts = [prompt_entry]

    prompt_html = _build_prompt_html(prompt_entry, state)

    assert "traffic" not in prompt_html
    assert "#ff5f57" not in prompt_html
    assert "linear-gradient" not in prompt_html
    assert "Avenir" not in prompt_html
    assert '-apple-system, BlinkMacSystemFont, "SF Pro Text"' in prompt_html


def test_prompt_popover_shows_streaming_feedback_and_button_state():
    state = SimpleNamespace(
        status_server_url="http://127.0.0.1:9",
        status_server_token="test-token",
        prompts=[],
        context_lines=12,
    )
    prompt_entry = SimpleNamespace(
        id="prompt-1",
        session_id="session-1",
        context={"context_lines": 8},
        messages=[{"role": "user", "content": "help"}],
        result="Thinking",
        status="streaming",
        timestamp=1,
    )
    state.prompts = [prompt_entry]

    prompt_html = _build_prompt_html(prompt_entry, state)

    assert 'const cancelEndpoint = "http://127.0.0.1:9/test-token/cancel";' in prompt_html
    assert '<button id="stop-button" type="button">Stop</button>' in prompt_html
    assert "async function cancelPrompt()" in prompt_html
    assert 'fetch(withEntry(cancelEndpoint), { method: "POST" })' in prompt_html
    assert 'inputActionsEl.className = busy ? "input-actions streaming" : "input-actions";' in prompt_html
    assert 'id="streaming-note"' in prompt_html
    assert 'aria-live="polite"' in prompt_html
    assert "TermFix is responding" in prompt_html
    assert 'chatPaneEl.classList.toggle("busy", busy);' in prompt_html
    assert 'sendButton.textContent = busy ? "Sending..." : "Send";' in prompt_html
    assert "textarea {\n      flex: 1;\n      min-height: 42px;\n      max-height: 180px;" in prompt_html


def test_prompt_empty_state_uses_recent_context_for_starters():
    rendered = _conversation_to_html(
        [],
        "",
        {"terminal_output": "npm test\nERROR failed to resolve package"},
    )

    assert "ERROR failed to resolve package" in rendered
    assert "Explain the recent output" in rendered
    assert "Suggest the safest next command based on the recent terminal context." in rendered


def test_sync_knobs_updates_settings_and_parses_context_lines():
    state = SimpleNamespace(
        base_url="old",
        api_key="",
        model="old-model",
        context_lines=12,
        max_tokens=DEFAULT_MAX_TOKENS,
        fix_hotkey="Cmd+J",
        prompt_hotkey="Cmd+L",
    )

    _sync_knobs(
        state,
        {
            "base_url": " https://api.example.com ",
            "api_key": " sk-test \n",
            "model": " model-x ",
            "context_lines": "8",
            "max_tokens": "4096",
            "fix_hotkey": "Command+K",
            "prompt_hotkey": "cmd+p",
        },
    )

    assert state.base_url == "https://api.example.com"
    assert state.api_key == "sk-test"
    assert state.model == "model-x"
    assert state.context_lines == 8
    assert state.max_tokens == 4096
    assert state.fix_hotkey == "Cmd+K"
    assert state.prompt_hotkey == "Cmd+P"

    _sync_knobs(state, {"api_key": " sk-test extra-token "})

    assert state.api_key == ""
    assert state.api_key_error == "API key contains internal whitespace; paste only the key."

    _sync_knobs(state, {"api_key": "API Key: sk-labeled"})

    assert state.api_key == "sk-labeled"
    assert state.api_key_error == ""

    _sync_knobs(state, {"api_key": "Bearer sk-bearer"})

    assert state.api_key == "sk-bearer"
    assert state.api_key_error == ""

    _sync_knobs(state, {"api_key": "Authorization: Bearer sk-auth"})

    assert state.api_key == "sk-auth"
    assert state.api_key_error == ""

    _sync_knobs(state, {"context_lines": "not-a-number"})

    assert state.context_lines == 8


def test_sync_knobs_logs_repeated_api_key_validation_error_once(caplog):
    state = SimpleNamespace(
        base_url="old",
        api_key="",
        api_key_error="",
        model="old-model",
        context_lines=12,
    )

    with caplog.at_level(logging.WARNING, logger="termfixlib.ui"):
        _sync_knobs(state, {"api_key": "sk-test extra-token"})
        _sync_knobs(state, {"api_key": "sk-test extra-token"})

    messages = [
        record.getMessage()
        for record in caplog.records
        if "Ignoring API key setting" in record.getMessage()
    ]
    assert messages == [
        "Ignoring API key setting: API key contains internal whitespace; paste only the key."
    ]


def test_sync_knobs_preserves_previous_values_for_invalid_runtime_config():
    state = SimpleNamespace(
        base_url="https://api.example.com/v1",
        api_key="",
        model="model-x",
        context_lines=12,
        max_tokens=4096,
        fix_hotkey="Cmd+K",
        prompt_hotkey="Cmd+P",
    )

    _sync_knobs(
        state,
        {
            "base_url": "ftp://example.com",
            "max_tokens": "many",
            "fix_hotkey": "Ctrl+J",
            "prompt_hotkey": "Command+Enter",
        },
    )

    assert state.base_url == "https://api.example.com/v1"
    assert state.base_url_error == "Base URL must start with http:// or https://."
    assert state.max_tokens == 4096
    assert state.max_tokens_error == "Max tokens must be a whole number."
    assert state.fix_hotkey == "Cmd+K"
    assert state.fix_hotkey_error == "Hotkey must be Command plus one ANSI letter, such as Cmd+J."
    assert state.prompt_hotkey == "Cmd+P"
    assert state.prompt_hotkey_error == "Hotkey must be Command plus one ANSI letter, such as Cmd+J."


def test_info_popover_exposes_validation_errors_and_current_knobs():
    state = SimpleNamespace(
        status_server_url="http://127.0.0.1:9",
        status_server_token="test-token",
        api_key="",
        api_key_error="",
        base_url="https://api.example.com",
        base_url_error="Base URL must include a host name.",
        model="model-x",
        max_tokens=4096,
        max_tokens_error="",
        fix_hotkey="Cmd+K",
        fix_hotkey_error="",
        prompt_hotkey="Cmd+P",
        prompt_hotkey_error="",
    )

    rendered = _build_info_html(state)

    assert "https://api.example.com" in rendered
    assert "<code>4096</code>" in rendered
    assert "<code>Cmd+K</code> / <code>Cmd+P</code>" in rendered
    assert "Base URL must include a host name." in rendered
