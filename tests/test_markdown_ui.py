from types import SimpleNamespace

from termfixlib.ui import _markdown_to_html, _sync_knobs


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
    assert "echo &#x27;&lt;unsafe&gt;&#x27;" in rendered
    assert "<script>" not in rendered


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
