"""
StatusBar component and popover UI for TermFix.

AutoLaunch only loads the top-level termfix.py script. This module stays in a
support package so iTerm2 does not try to execute it as a standalone script.
"""

from __future__ import annotations

import asyncio
import html
import json
import logging
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Optional
from urllib.parse import parse_qs, urlparse

import iterm2

from .config import (
    DEFAULT_BASE_URL,
    DEFAULT_CONTEXT_LINES,
    DEFAULT_MODEL,
    POPOVER_HEIGHT,
    POPOVER_WIDTH,
    STATUS_ERROR_FMT,
    STATUS_IDENTIFIER,
    STATUS_NORMAL,
)
from .llm_client import stream_analyze_error

logger = logging.getLogger(__name__)


async def register_status_bar(
    connection: iterm2.Connection,
    app: iterm2.App,
) -> "TermFixState":
    """Create and register the status bar component; return shared state."""
    from .monitor import TermFixState

    state = TermFixState()
    state.connection = connection
    _ensure_status_server(state)

    knobs = [
        iterm2.StringKnob(
            name="Base URL",
            placeholder=DEFAULT_BASE_URL,
            default_value=DEFAULT_BASE_URL,
            key="base_url",
        ),
        iterm2.StringKnob(
            name="API Key",
            placeholder="sk-xxxx",
            default_value="",
            key="api_key",
        ),
        iterm2.StringKnob(
            name="Model",
            placeholder=DEFAULT_MODEL,
            default_value=DEFAULT_MODEL,
            key="model",
        ),
        iterm2.StringKnob(
            name="Context Lines",
            placeholder=str(DEFAULT_CONTEXT_LINES),
            default_value=str(DEFAULT_CONTEXT_LINES),
            key="context_lines",
        ),
    ]

    component = iterm2.StatusBarComponent(
        short_description="TermFix",
        detailed_description=(
            "Analyses failed commands and shows fix suggestions via an OpenAI-compatible API."
        ),
        knobs=knobs,
        icons=[],
        exemplar=STATUS_NORMAL,
        update_cadence=1.0,
        identifier=STATUS_IDENTIFIER,
    )

    state.component = component

    @iterm2.StatusBarRPC
    async def _status_coro(knobs):
        _sync_knobs(state, knobs)
        if state.analyzing:
            return "⏳ Analyzing..."
        if state.error_count == 0:
            return STATUS_NORMAL
        return STATUS_ERROR_FMT.format(count=state.error_count)

    @iterm2.RPC
    async def _on_click(session_id):
        asyncio.create_task(
            _handle_click(connection, session_id, state),
            name="termfix-click",
        )

    await component.async_register(
        connection,
        _status_coro,
        timeout=None,
        onclick=_on_click,
    )

    logger.info("TermFix status bar component registered (%s)", STATUS_IDENTIFIER)
    return state


async def start_hotkey_listener(
    connection: iterm2.Connection,
    app: iterm2.App,
    state: "TermFixState",
) -> None:
    """Listen for Cmd+J and manually open TermFix for the active session."""
    pattern = iterm2.KeystrokePattern()
    pattern.required_modifiers = [iterm2.Modifier.COMMAND]
    pattern.forbidden_modifiers = [
        iterm2.Modifier.CONTROL,
        iterm2.Modifier.OPTION,
        iterm2.Modifier.SHIFT,
    ]
    pattern.keycodes = [iterm2.Keycode.ANSI_J]

    try:
        logger.info("TermFix hotkey listener registered (Cmd+J)")
        async with iterm2.KeystrokeFilter(connection, [pattern]):
            async with iterm2.KeystrokeMonitor(connection, advanced=True) as mon:
                while True:
                    keystroke = await mon.async_get()
                    if not _is_termfix_hotkey(keystroke):
                        continue
                    session_id = await _get_active_session_id(app)
                    logger.info("TermFix manual trigger — session=%s", session_id)
                    asyncio.create_task(
                        _handle_click(connection, session_id, state),
                        name="termfix-hotkey",
                    )
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        logger.error("TermFix hotkey listener failed: %s", exc, exc_info=True)
        await asyncio.Event().wait()


async def _handle_click(
    connection: iterm2.Connection,
    session_id: Optional[str],
    state: "TermFixState",
) -> None:
    """Start analysis if needed and open one live-updating popover."""
    entry = _pick_entry(state, session_id)
    if entry is None:
        await _open_info_popover(connection, session_id, state)
        return

    if not entry.analysis_started:
        _start_analysis_task(entry, state)

    target_session_id = session_id or entry.session_id
    try:
        await _open_popover(connection, target_session_id, state, _build_live_html(entry, state))
        await state.notify_ui_update()
    except Exception as exc:
        logger.error("Failed to open popover: %s", exc)


def _start_analysis_task(entry, state: "TermFixState") -> None:
    """Launch one streaming analysis task for an entry."""
    if entry.id in state.analysis_tasks:
        return

    entry.analysis_started = True
    entry.status = "streaming"
    entry.result = entry.result or "Analyzing..."
    entry.updated_at = time.time()
    state.refresh_analyzing()

    task = asyncio.create_task(
        _run_streaming_analysis(entry, state),
        name=f"termfix-stream-{entry.id[:8]}",
    )
    state.analysis_tasks[entry.id] = task


async def _run_streaming_analysis(entry, state: "TermFixState") -> None:
    """Update entry.result as provider chunks arrive."""
    try:
        await state.notify_ui_update()
        async for snapshot in stream_analyze_error(
            entry.context,
            api_key=state.api_key,
            base_url=state.base_url,
            model=state.model,
        ):
            entry.result = snapshot or entry.result
            entry.updated_at = time.time()
        entry.analyzed = True
        entry.status = "done"
        entry.updated_at = time.time()
    except asyncio.CancelledError:
        entry.status = "cancelled"
        entry.updated_at = time.time()
        raise
    except Exception as exc:
        logger.error("LLM analysis failed: %s", exc, exc_info=True)
        entry.result = f"""\
### Cause
Analysis error.

### Fix
Check the TermFix script console for details.

### Details
{exc}
"""
        entry.status = "error"
        entry.updated_at = time.time()
    finally:
        state.analysis_tasks.pop(entry.id, None)
        state.refresh_analyzing()
        await state.notify_ui_update()


def _pick_entry(state: "TermFixState", session_id: Optional[str]):
    """Prefer the latest error from the clicked session; fall back globally."""
    if session_id:
        for entry in reversed(state.errors):
            if entry.session_id == session_id:
                return entry
    return state.latest_error()


def _ensure_status_server(state: "TermFixState") -> None:
    """Start a local JSON endpoint for popovers to poll live analysis state."""
    if state.status_server_url:
        return

    class Handler(BaseHTTPRequestHandler):
        def do_OPTIONS(self):  # noqa: N802 - BaseHTTPRequestHandler API.
            self._send_json({})

        def do_GET(self):  # noqa: N802 - BaseHTTPRequestHandler API.
            parsed = urlparse(self.path)
            if parsed.path != "/state":
                self.send_error(404)
                return

            entry_id = parse_qs(parsed.query).get("entry", [""])[0]
            self._send_json(_entry_payload(state, entry_id))

        def _send_json(self, payload: dict) -> None:
            body = json.dumps(payload).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Access-Control-Allow-Methods", "GET, OPTIONS")
            self.send_header("Access-Control-Allow-Headers", "Content-Type")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, fmt, *args):  # noqa: ANN001
            logger.debug("Popover status server: " + fmt, *args)

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(
        target=server.serve_forever,
        name="termfix-popover-status",
        daemon=True,
    )
    thread.start()

    state.status_server = server
    state.status_server_url = f"http://127.0.0.1:{server.server_port}"
    logger.info("TermFix popover status server listening on %s", state.status_server_url)


def _entry_payload(state: "TermFixState", entry_id: str) -> dict:
    """Return a JSON-safe snapshot of the current analysis state."""
    entry = state.get_error(entry_id)
    if entry is None:
        return {
            "ok": False,
            "status": "missing",
            "done": True,
            "body_html": "<p>This TermFix result is no longer available.</p>",
        }

    markdown = entry.result or "Analyzing..."
    done = entry.status in ("done", "error", "cancelled")
    return {
        "ok": True,
        "entry_id": entry.id,
        "status": entry.status,
        "done": done,
        "updated_at": entry.updated_at,
        "body_html": _markdown_to_html(markdown),
    }


def _is_termfix_hotkey(keystroke) -> bool:
    """Return true for Cmd+J key-down events."""
    if keystroke.action not in (
        iterm2.Keystroke.Action.NA,
        iterm2.Keystroke.Action.KEY_DOWN,
    ):
        return False
    if keystroke.keycode != iterm2.Keycode.ANSI_J:
        return False
    modifiers = set(keystroke.modifiers)
    return modifiers == {iterm2.Modifier.COMMAND}


async def _get_active_session_id(app: iterm2.App) -> Optional[str]:
    """Find the currently active session id, refreshing focus first."""
    try:
        await app.async_refresh_focus()
    except Exception as exc:
        logger.debug("Could not refresh focus before hotkey handling: %s", exc)

    window = app.current_window
    if window is None:
        return None
    tab = window.current_tab
    if tab is None:
        return None
    session = tab.current_session
    if session is None:
        return None
    return session.session_id


def _build_live_html(entry, state: "TermFixState") -> str:
    """Build a popover that polls local state and updates in place."""
    _ensure_status_server(state)
    failed_cmd = html.escape(entry.command or "(unknown command)")
    exit_code = entry.exit_code
    body = _markdown_to_html(entry.result or "Analyzing...")
    endpoint = json.dumps(f"{state.status_server_url}/state?entry={entry.id}")

    return f"""\
<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <style>
    *, *::before, *::after {{ box-sizing: border-box; margin: 0; padding: 0; }}
    body {{
      font-family: -apple-system, BlinkMacSystemFont, "Helvetica Neue", sans-serif;
      font-size: 13px;
      color: #1c1c1e;
      background: #ffffff;
      padding: 14px 16px;
      line-height: 1.45;
    }}
    header {{
      display: flex;
      align-items: center;
      gap: 8px;
      margin-bottom: 12px;
      padding-bottom: 10px;
      border-bottom: 1px solid #e5e5ea;
    }}
    header h1 {{ font-size: 14px; font-weight: 600; color: #c0392b; }}
    .badge {{
      font-size: 11px;
      font-weight: 500;
      background: #f2f2f7;
      border-radius: 4px;
      padding: 1px 6px;
      color: #636366;
      font-family: "Menlo", monospace;
    }}
    .markdown h1,
    .markdown h2,
    .markdown h3 {{
      font-size: 13px;
      color: #c0392b;
      margin: 12px 0 6px;
    }}
    .markdown h1:first-child,
    .markdown h2:first-child,
    .markdown h3:first-child {{ margin-top: 0; }}
    .markdown p {{ margin: 0 0 8px; color: #3c3c43; }}
    .markdown ul,
    .markdown ol {{ margin: 0 0 8px 18px; padding: 0; }}
    .markdown li {{ margin-bottom: 4px; }}
    .markdown pre {{
      background: #1c1c1e;
      border-radius: 6px;
      padding: 8px 12px;
      overflow-x: auto;
      margin: 6px 0 10px;
    }}
    .markdown code {{
      font-family: "Menlo", "Monaco", "Courier New", monospace;
      font-size: 12px;
      background: #f2f2f7;
      border-radius: 4px;
      padding: 1px 4px;
    }}
    .markdown pre code {{
      font-family: "Menlo", "Monaco", "Courier New", monospace;
      font-size: 12px;
      color: #30d158;
      background: transparent;
      border-radius: 0;
      padding: 0;
    }}
    .failed-cmd {{
      font-family: "Menlo", monospace;
      font-size: 11px;
      color: #636366;
      background: #f2f2f7;
      border-radius: 4px;
      padding: 1px 6px;
    }}
    .status {{
      margin-left: auto;
      font-size: 11px;
      color: #8e8e93;
    }}
    .status.streaming::after {{
      content: "";
      display: inline-block;
      width: 1em;
      animation: dots 1.1s steps(4, end) infinite;
    }}
    @keyframes dots {{
      0% {{ content: ""; }}
      25% {{ content: "."; }}
      50% {{ content: ".."; }}
      75%, 100% {{ content: "..."; }}
    }}
  </style>
</head>
<body>
  <header>
    <h1>🔍 TermFix</h1>
    <span class="failed-cmd">{failed_cmd}</span>
    <span class="badge">exit {exit_code}</span>
    <span id="status" class="status {html.escape(entry.status)}">{html.escape(entry.status)}</span>
  </header>

  <div id="content" class="markdown">{body}</div>
  <script>
    const endpoint = {endpoint};
    const contentEl = document.getElementById("content");
    const statusEl = document.getElementById("status");
    let lastHtml = contentEl.innerHTML;
    let timer = null;

    async function refresh() {{
      try {{
        const sep = endpoint.includes("?") ? "&" : "?";
        const response = await fetch(endpoint + sep + "t=" + Date.now(), {{
          cache: "no-store"
        }});
        const data = await response.json();
        if (data.body_html && data.body_html !== lastHtml) {{
          lastHtml = data.body_html;
          contentEl.innerHTML = data.body_html;
        }}
        if (data.status) {{
          statusEl.textContent = data.status;
          statusEl.className = "status " + data.status;
        }}
        if (data.done && timer) {{
          clearInterval(timer);
          timer = null;
        }}
      }} catch (error) {{
        statusEl.textContent = "offline";
        statusEl.className = "status error";
      }}
    }}

    refresh();
    timer = setInterval(refresh, 350);
  </script>
</body>
</html>"""


def _build_info_html(state: "TermFixState") -> str:
    """Build a small informational popover shown when there is no error yet."""
    base_url = html.escape(state.base_url or DEFAULT_BASE_URL)
    model = html.escape(state.model or DEFAULT_MODEL)
    api_key_status = "Configured" if state.api_key else "Missing"

    return f"""\
<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <style>
    body {{
      font-family: -apple-system, BlinkMacSystemFont, "Helvetica Neue", sans-serif;
      font-size: 13px;
      color: #1c1c1e;
      background: #ffffff;
      padding: 14px 16px;
      line-height: 1.5;
    }}
    h1 {{ font-size: 14px; margin: 0 0 10px 0; }}
    p {{ margin: 0 0 10px 0; color: #3c3c43; }}
    .item {{ margin: 0 0 6px 0; }}
    .label {{ color: #8e8e93; font-weight: 600; }}
    code {{
      font-family: "Menlo", monospace;
      background: #f2f2f7;
      border-radius: 4px;
      padding: 1px 6px;
    }}
  </style>
</head>
<body>
  <h1>TermFix</h1>
  <p>No failed command is waiting for analysis.</p>
  <p>Run a command that exits non-zero, then click TermFix again.</p>
  <div class="item"><span class="label">Base URL:</span> <code>{base_url}</code></div>
  <div class="item"><span class="label">Model:</span> <code>{model}</code></div>
  <div class="item"><span class="label">API Key:</span> <code>{api_key_status}</code></div>
</body>
</html>"""


async def _open_info_popover(
    connection: iterm2.Connection,
    session_id: Optional[str],
    state: "TermFixState",
) -> None:
    """Show a helpful popover when there is no pending error entry."""
    if not session_id:
        return
    await _open_popover(connection, session_id, state, _build_info_html(state))


async def _open_popover(
    connection: iterm2.Connection,
    session_id: str,
    state: "TermFixState",
    html_content: str,
) -> None:
    """Open a popover using the registered component when available."""
    if state.component is not None:
        await state.component.async_open_popover(
            session_id=session_id,
            html=html_content,
            size=iterm2.Size(POPOVER_WIDTH, POPOVER_HEIGHT),
        )
        return

    await iterm2.async_open_popover(
        connection=connection,
        session_id=session_id,
        html=html_content,
        size=iterm2.Size(POPOVER_WIDTH, POPOVER_HEIGHT),
    )


def _sync_knobs(state: "TermFixState", knobs: dict) -> None:
    """Push current knob values into shared state."""
    if not isinstance(knobs, dict):
        return

    base_url = knobs.get("base_url", "").strip()
    if base_url:
        state.base_url = base_url

    api_key_raw = knobs.get("api_key", "").strip()
    state.api_key = api_key_raw.split()[0] if api_key_raw else ""

    model = knobs.get("model", "").strip()
    if model:
        state.model = model

    ctx_raw = knobs.get("context_lines", "").strip()
    if ctx_raw.isdigit():
        state.context_lines = int(ctx_raw)


def _markdown_to_html(markdown: str) -> str:
    """Render a small, safe Markdown subset to HTML."""
    lines = markdown.splitlines()
    blocks: list[str] = []
    paragraph: list[str] = []
    list_items: list[str] = []
    in_code = False
    code_lines: list[str] = []

    def flush_paragraph() -> None:
        nonlocal paragraph
        if paragraph:
            blocks.append(f"<p>{_inline_markdown(' '.join(paragraph))}</p>")
            paragraph = []

    def flush_list() -> None:
        nonlocal list_items
        if list_items:
            blocks.append("<ul>" + "".join(f"<li>{item}</li>" for item in list_items) + "</ul>")
            list_items = []

    for line in lines:
        stripped = line.strip()

        if stripped.startswith("```"):
            if in_code:
                blocks.append(
                    "<pre><code>"
                    + html.escape("\n".join(code_lines))
                    + "</code></pre>"
                )
                code_lines = []
                in_code = False
            else:
                flush_paragraph()
                flush_list()
                in_code = True
            continue

        if in_code:
            code_lines.append(line)
            continue

        if not stripped:
            flush_paragraph()
            flush_list()
            continue

        if stripped.startswith("### "):
            flush_paragraph()
            flush_list()
            blocks.append(f"<h3>{html.escape(stripped[4:].strip())}</h3>")
        elif stripped.startswith("## "):
            flush_paragraph()
            flush_list()
            blocks.append(f"<h2>{html.escape(stripped[3:].strip())}</h2>")
        elif stripped.startswith("# "):
            flush_paragraph()
            flush_list()
            blocks.append(f"<h1>{html.escape(stripped[2:].strip())}</h1>")
        elif stripped.startswith(("- ", "* ")):
            flush_paragraph()
            list_items.append(_inline_markdown(stripped[2:].strip()))
        else:
            paragraph.append(stripped)

    if in_code:
        blocks.append(
            "<pre><code>" + html.escape("\n".join(code_lines)) + "</code></pre>"
        )
    flush_paragraph()
    flush_list()

    return "\n".join(blocks)


def _inline_markdown(text: str) -> str:
    """Render inline code and bold markers in escaped text."""
    escaped = html.escape(text)
    parts = escaped.split("`")
    for i in range(1, len(parts), 2):
        parts[i] = f"<code>{parts[i]}</code>"
    escaped = "".join(parts)
    escaped = escaped.replace("**", "")
    return escaped
