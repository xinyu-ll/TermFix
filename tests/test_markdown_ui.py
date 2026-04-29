from types import SimpleNamespace

from termfixlib.ui import _build_live_html, _build_prompt_html, _markdown_to_html, _sync_knobs


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
    assert '<button class="copy-code" type="button" data-copy-code>Copy</button>' in rendered
    assert "echo &#x27;&lt;unsafe&gt;&#x27;" in rendered
    assert "<script>" not in rendered


def test_code_block_copy_controls_are_shared_by_live_and_prompt_popovers():
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
        ".copy-code.copied",
        "async function copyText(text)",
        "function handleCodeBlockCopy(event)",
        '<button class="copy-code" type="button" data-copy-code>Copy</button>',
    ]
    for marker in shared_markers:
        assert marker in live_html
        assert marker in prompt_html

    assert 'contentEl.addEventListener("click", handleCodeBlockCopy);' in live_html
    assert "if (handleCodeBlockCopy(event))" in prompt_html


def test_sync_knobs_updates_settings_and_parses_context_lines():
    state = SimpleNamespace(
        base_url="old",
        api_key="",
        model="old-model",
        context_lines=12,
    )

    _sync_knobs(
        state,
        {
            "base_url": " https://api.example.com ",
            "api_key": " sk-test extra-token ",
            "model": " model-x ",
            "context_lines": "8",
        },
    )

    assert state.base_url == "https://api.example.com"
    assert state.api_key == "sk-test"
    assert state.model == "model-x"
    assert state.context_lines == 8

    _sync_knobs(state, {"context_lines": "not-a-number"})

    assert state.context_lines == 8
