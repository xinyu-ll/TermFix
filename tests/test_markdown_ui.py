import asyncio
import logging
import sys
import types
from types import SimpleNamespace

sys.modules.setdefault("iterm2", types.ModuleType("iterm2"))

from termfixlib.config import DEFAULT_BASE_URL, DEFAULT_MAX_TOKENS
from termfixlib import markdown as markdown_rendering
from termfixlib.ui import (
    _CODE_BLOCK_COPY_CSS,
    _CODE_BLOCK_COPY_JS,
    _build_info_html,
    _build_live_html,
    _build_prompt_html,
    _compact_text,
    _conversation_to_html,
    _insert_code_text,
    _markdown_to_html,
    _prompt_context_label,
    _status_badge_text,
    _sync_knobs,
)


class FakeSession:
    def __init__(self, session_id="session-1"):
        self.session_id = session_id
        self.sent = []

    async def async_send_text(self, text, suppress_broadcast=False):
        self.sent.append((text, suppress_broadcast))


def test_ui_keeps_markdown_helper_compatibility_aliases():
    assert _markdown_to_html is markdown_rendering._markdown_to_html
    assert _compact_text is markdown_rendering._compact_text
    assert _CODE_BLOCK_COPY_CSS is markdown_rendering._CODE_BLOCK_COPY_CSS
    assert _CODE_BLOCK_COPY_JS is markdown_rendering._CODE_BLOCK_COPY_JS


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
        'button.removeAttribute("title");',
        'insertButton.textContent = blocked ? "Use Copy" : "Insert failed";',
        '<button class="code-action copy-code" type="button" data-copy-code>Copy</button>',
        '<button class="code-action insert-code" type="button" data-insert-code>Insert</button>',
        "/test-token/insert?session=session-1",
    ]
    for marker in shared_markers:
        assert marker in live_html
        assert marker in prompt_html

    assert 'contentEl.addEventListener("click", handleCodeBlockCopy);' in live_html
    assert "if (handleCodeBlockCopy(event))" in prompt_html


def test_live_popover_includes_error_inbox_navigation():
    state = SimpleNamespace(
        status_server_url="http://127.0.0.1:9",
        status_server_token="test-token",
        prompts=[],
        context_lines=12,
        fix_hotkey="Cmd+J",
    )
    active = SimpleNamespace(
        id="error-1",
        session_id="session-1",
        command="pytest",
        exit_code=1,
        result="Active result",
        status="done",
        handled=True,
    )
    other = SimpleNamespace(
        id="error-2",
        session_id="session-1",
        command="npm test",
        exit_code=2,
        result="Other result",
        status="pending",
        handled=False,
    )
    state.errors = [other, active]

    live_html = _build_live_html(active, state)

    assert 'id="error-list"' in live_html
    assert 'data-entry-id="error-1"' in live_html
    assert 'data-entry-id="error-2"' in live_html
    assert '<span class="error-pill current">current</span>' in live_html
    assert "function setActiveEntry(entryId)" in live_html
    assert 'withEntry(endpoint, activeEntryId) + "&start=1&t="' in live_html


def test_insert_code_text_strips_final_newline_before_sending():
    session = FakeSession()
    state = SimpleNamespace(
        terminal_sessions={"session-1": session},
        prompt_sessions={},
        connection=None,
    )

    result = asyncio.run(_insert_code_text("printf 'hi'\n# no enter\n", state, "session-1"))

    assert result == {"ok": True, "session_id": "session-1"}
    assert session.sent == [("printf 'hi'\n# no enter", True)]


def test_insert_code_text_blocks_dangerous_commands_without_sending():
    session = FakeSession()
    state = SimpleNamespace(
        terminal_sessions={"session-1": session},
        prompt_sessions={},
        connection=None,
    )

    for command in ("rm -rf /", "git reset --hard", "chmod -R 777 /tmp/project"):
        result = asyncio.run(_insert_code_text(command, state, "session-1"))

        assert result["ok"] is False
        assert "Insert blocked" in result["error"]
        assert "Use Copy" in result["error"]

    assert session.sent == []


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

    assert "Context redaction active" in live_html
    assert "let pollDelay = 350;" in live_html
    assert "const nextDelay = data.done ? 2000 : 350;" in live_html
    assert "timer = setInterval(refresh, pollDelay);" in live_html


def test_live_popover_includes_retry_control_for_error_state():
    state = SimpleNamespace(
        status_server_url="http://127.0.0.1:9",
        status_server_token="test-token",
        prompts=[],
        context_lines=12,
    )
    live_entry = SimpleNamespace(
        id="error-1",
        session_id="session-1",
        command="pytest",
        exit_code=1,
        result="Network error.",
        status="error",
        handled=False,
    )

    live_html = _build_live_html(live_entry, state)

    assert 'const dismissEndpoint = "http://127.0.0.1:9/test-token/dismiss";' in live_html
    assert 'const retryEndpoint = "http://127.0.0.1:9/test-token/retry";' in live_html
    assert '<button id="dismiss-button" class="dismiss-button" type="button">Ignore</button>' in live_html
    assert "async function dismissError()" in live_html
    assert '<button id="retry-button" type="button">Retry analysis</button>' in live_html
    assert "async function retryAnalysis()" in live_html
    assert "setRetryVisible(Boolean(data.can_retry));" in live_html
    assert 'fetch(withEntry(retryEndpoint, activeEntryId), {' in live_html


def test_live_popover_shows_terminal_context_metadata():
    state = SimpleNamespace(
        status_server_url="http://127.0.0.1:9",
        status_server_token="test-token",
        prompts=[],
        context_lines=12,
    )
    live_entry = SimpleNamespace(
        id="error-1",
        session_id="session-1",
        command="git push",
        exit_code=128,
        result="Analyzing...",
        status="streaming",
        handled=False,
        context={
            "terminal_output_line_count": 50,
            "shell": "zsh",
            "os_name": "macOS",
            "os_version": "14.5",
        },
    )

    live_html = _build_live_html(live_entry, state)

    assert '<span id="context-label" class="context-label">50 lines context · zsh · macOS 14.5</span>' in live_html
    assert 'const contextLabelEl = document.getElementById("context-label");' in live_html
    assert 'Object.prototype.hasOwnProperty.call(data, "context_label")' in live_html


def test_prompt_context_label_mentions_redaction_status():
    entry = SimpleNamespace(session_id="session-1", context={"context_lines": 8}, messages=[])
    state = SimpleNamespace(context_lines=12)

    assert _prompt_context_label(entry, state, "session-1") == (
        "Current session context - last 8 lines of output, context redaction active"
    )


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


def test_prompt_popover_includes_history_search_filter():
    state = SimpleNamespace(
        status_server_url="http://127.0.0.1:9",
        status_server_token="test-token",
        prompts=[],
        context_lines=12,
        prompt_hotkey="Cmd+L",
    )
    prompt_entry = SimpleNamespace(
        id="prompt-1",
        session_id="session-1",
        context={"context_lines": 8},
        messages=[
            {"role": "user", "content": "git push failed"},
            {"role": "assistant", "content": "Auth failed. Refresh credentials."},
        ],
        result="",
        status="done",
        timestamp=1,
        restored=False,
    )
    state.prompts = [prompt_entry]

    prompt_html = _build_prompt_html(prompt_entry, state)

    assert 'id="history-search"' in prompt_html
    assert 'placeholder="Search history..."' in prompt_html
    assert 'data-search="git push failed Auth failed. Refresh credentials."' in prompt_html
    assert "function applyHistoryFilter()" in prompt_html
    assert 'historySearchEl.addEventListener("input", applyHistoryFilter);' in prompt_html
    assert "applyHistoryFilter();" in prompt_html


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

    _sync_knobs(state, {"api_key": "DeepSeek API Key: sk-deepseek"})

    assert state.api_key == "sk-deepseek"
    assert state.api_key_error == ""

    _sync_knobs(state, {"api_key": 'export DEEPSEEK_API_KEY="sk-exported"'})

    assert state.api_key == "sk-exported"
    assert state.api_key_error == ""

    _sync_knobs(state, {"api_key": "OPENAI_API_KEY='sk-openai'"})

    assert state.api_key == "sk-openai"
    assert state.api_key_error == ""

    _sync_knobs(state, {"context_lines": "not-a-number"})

    assert state.context_lines == 8


def test_sync_knobs_accepts_display_name_knob_keys():
    state = SimpleNamespace(
        base_url="old",
        api_key="",
        api_key_error="",
        model="old-model",
        context_lines=12,
        max_tokens=DEFAULT_MAX_TOKENS,
        fix_hotkey="Cmd+J",
        prompt_hotkey="Cmd+L",
    )

    _sync_knobs(
        state,
        {
            "Base URL": " https://api.example.com ",
            "API Key": " sk-configured ",
            "Model": " model-x ",
            "Context Lines (1-500)": "42",
            "Max Tokens (1-200000)": "4096",
            "Fix Hotkey": "Cmd+K",
            "Prompt Hotkey": "Cmd+P",
        },
    )

    assert state.base_url == "https://api.example.com"
    assert state.api_key == "sk-configured"
    assert state.api_key_error == ""
    assert state.model == "model-x"
    assert state.context_lines == 42
    assert state.max_tokens == 4096
    assert state.fix_hotkey == "Cmd+K"
    assert state.prompt_hotkey == "Cmd+P"


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


def test_sync_knobs_preserves_api_key_when_knob_payload_omits_it():
    state = SimpleNamespace(
        base_url="old",
        api_key="sk-existing",
        api_key_error="",
        model="old-model",
        context_lines=12,
    )

    _sync_knobs(state, {"model": "new-model"})

    assert state.api_key == "sk-existing"
    assert state.api_key_error == ""
    assert state.model == "new-model"


def test_sync_knobs_uses_environment_api_key_when_knob_is_blank(monkeypatch):
    monkeypatch.delenv("TERMFIX_API_KEY", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.setenv("DEEPSEEK_API_KEY", "Bearer sk-env")
    state = SimpleNamespace(
        base_url=DEFAULT_BASE_URL,
        api_key="",
        api_key_error="",
        model="old-model",
        context_lines=12,
    )

    _sync_knobs(state, {"api_key": ""})

    assert state.api_key == "sk-env"
    assert state.api_key_error == ""


def test_sync_knobs_ignores_provider_env_keys_for_other_base_urls(monkeypatch):
    monkeypatch.delenv("TERMFIX_API_KEY", raising=False)
    monkeypatch.setenv("OPENAI_API_KEY", "sk-openai")
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-deepseek")
    state = SimpleNamespace(
        base_url="https://api.example.com/v1",
        api_key="",
        api_key_error="",
        model="old-model",
        context_lines=12,
    )

    _sync_knobs(state, {"api_key": ""})

    assert state.api_key == ""
    assert state.api_key_error == ""


def test_sync_knobs_uses_matching_provider_env_key_for_base_url(monkeypatch):
    monkeypatch.delenv("TERMFIX_API_KEY", raising=False)
    monkeypatch.setenv("OPENAI_API_KEY", "sk-openai")
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-deepseek")
    state = SimpleNamespace(
        base_url=DEFAULT_BASE_URL,
        api_key="",
        api_key_error="",
        model="old-model",
        context_lines=12,
    )

    _sync_knobs(state, {"api_key": ""})
    assert state.api_key == "sk-deepseek"

    state.base_url = "https://api.openai.com/v1"
    _sync_knobs(state, {"api_key": ""})
    assert state.api_key == "sk-openai"


def test_sync_knobs_prefers_termfix_env_key_over_provider_env_key(monkeypatch):
    monkeypatch.setenv("TERMFIX_API_KEY", "sk-termfix")
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-deepseek")
    state = SimpleNamespace(
        base_url=DEFAULT_BASE_URL,
        api_key="",
        api_key_error="",
        model="old-model",
        context_lines=12,
    )

    _sync_knobs(state, {"api_key": ""})

    assert state.api_key == "sk-termfix"
    assert state.api_key_error == ""


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


def test_sync_knobs_flags_hotkey_conflicts_and_clears_after_change():
    state = SimpleNamespace(
        base_url="https://api.example.com/v1",
        api_key="",
        model="model-x",
        context_lines=12,
        max_tokens=4096,
        fix_hotkey="Cmd+J",
        fix_hotkey_error="",
        prompt_hotkey="Cmd+L",
        prompt_hotkey_error="",
        analyzing=False,
        unhandled_error_count=0,
    )

    _sync_knobs(state, {"fix_hotkey": "Cmd+J", "prompt_hotkey": "Cmd+J"})

    assert state.fix_hotkey == "Cmd+J"
    assert state.prompt_hotkey == "Cmd+J"
    assert state.fix_hotkey_error == "Fix Hotkey conflicts with Prompt Hotkey (Cmd+J)."
    assert state.prompt_hotkey_error == "Prompt Hotkey conflicts with Fix Hotkey (Cmd+J)."
    assert _status_badge_text(state) == "⚠ Hotkey config"

    _sync_knobs(state, {"prompt_hotkey": "Cmd+L"})

    assert state.prompt_hotkey == "Cmd+L"
    assert state.fix_hotkey_error == ""
    assert state.prompt_hotkey_error == ""


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
    assert "Context redaction active" in rendered
    assert "Base URL must include a host name." in rendered
    assert '<button id="test-connection" type="button">Test connection</button>' in rendered
    assert 'const testEndpoint = "http://127.0.0.1:9/test-token/test-connection";' in rendered
    assert "const hasApiKey = false;" in rendered
    assert "API key is missing. Configure the API Key knob before testing." in rendered
    assert "if (!hasApiKey) {" in rendered
    assert "fetch(testEndpoint, {" in rendered


def test_info_popover_marks_connection_test_available_when_api_key_configured():
    state = SimpleNamespace(
        status_server_url="http://127.0.0.1:9",
        status_server_token="test-token",
        api_key="sk-secret",
        api_key_error="",
        base_url="https://api.example.com",
        base_url_error="",
        model="model-x",
        max_tokens=4096,
        max_tokens_error="",
        fix_hotkey="Cmd+K",
        fix_hotkey_error="",
        prompt_hotkey="Cmd+P",
        prompt_hotkey_error="",
    )

    rendered = _build_info_html(state)

    assert "<code>Configured</code>" in rendered
    assert "const hasApiKey = true;" in rendered
    assert "sk-secret" not in rendered
