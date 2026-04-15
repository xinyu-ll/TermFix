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
from .context import collect_context
from .llm_client import stream_analyze_error, stream_user_prompt
from .monitor import PromptEntry

logger = logging.getLogger(__name__)

_INFO_POPOVER_ID = "__info__"


async def register_status_bar(
    connection: iterm2.Connection,
    app: iterm2.App,
) -> "TermFixState":
    """Create and register the status bar component; return shared state."""
    from .monitor import TermFixState

    state = TermFixState()
    state.connection = connection
    state.loop = asyncio.get_running_loop()
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
            _handle_click(connection, session_id, state, toggle=False),
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
    """Listen for TermFix hotkeys in the active session."""
    fix_pattern = _build_command_key_pattern(iterm2.Keycode.ANSI_J)
    prompt_pattern = _build_command_key_pattern(iterm2.Keycode.ANSI_L)

    try:
        logger.info("TermFix hotkey listener registered (Cmd+J, Cmd+L)")
        async with iterm2.KeystrokeFilter(connection, [fix_pattern, prompt_pattern]):
            async with iterm2.KeystrokeMonitor(connection, advanced=True) as mon:
                while True:
                    keystroke = await mon.async_get()
                    hotkey = _termfix_hotkey_kind(keystroke)
                    if hotkey is None:
                        continue
                    session_id = await _get_active_session_id(app)
                    if hotkey == "fix":
                        logger.info("TermFix manual trigger — session=%s", session_id)
                        asyncio.create_task(
                            _handle_click(connection, session_id, state, toggle=True),
                            name="termfix-hotkey",
                        )
                    elif hotkey == "prompt":
                        logger.info("TermFix prompt trigger — session=%s", session_id)
                        asyncio.create_task(
                            _handle_prompt_hotkey(connection, app, session_id, state),
                            name="termfix-prompt-hotkey",
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
    toggle: bool = False,
) -> None:
    """Start analysis if needed and open one live-updating popover."""
    entry = _pick_entry(state, session_id)
    if entry is None:
        if toggle and state.is_popover_open(_INFO_POPOVER_ID):
            state.request_popover_close(_INFO_POPOVER_ID)
            logger.info("TermFix info popover close requested")
            return
        await _open_info_popover(connection, session_id, state)
        return

    if toggle and state.is_popover_open(entry.id):
        state.request_popover_close(entry.id)
        logger.info("TermFix popover close requested — entry=%s", entry.id)
        return

    if not entry.analysis_started:
        _start_analysis_task(entry, state)

    target_session_id = session_id or entry.session_id
    try:
        await _open_popover(connection, target_session_id, state, _build_live_html(entry, state))
        state.mark_popover_seen(entry.id)
        await state.notify_ui_update()
    except Exception as exc:
        logger.error("Failed to open popover: %s", exc)


async def _handle_prompt_hotkey(
    connection: iterm2.Connection,
    app: iterm2.App,
    session_id: Optional[str],
    state: "TermFixState",
) -> None:
    """Open a manual prompt popover for the current session."""
    if not session_id:
        return

    existing = state.latest_prompt(session_id)
    if existing is not None and state.is_popover_open(existing.id):
        state.request_popover_close(existing.id)
        logger.info("TermFix prompt popover close requested — entry=%s", existing.id)
        return

    session = app.get_session_by_id(session_id)
    if session is None:
        logger.debug("Cannot open prompt popover; session disappeared: %s", session_id)
        return

    try:
        entry = PromptEntry(session_id=session_id, context={}, session=session)
        await state.add_prompt(entry)
        await _open_popover(connection, session_id, state, _build_prompt_html(entry, state))
        state.mark_popover_seen(entry.id)
    except Exception as exc:
        logger.error("Failed to open prompt popover: %s", exc, exc_info=True)


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


def _start_prompt_analysis_task(entry: PromptEntry, state: "TermFixState") -> None:
    """Launch one streaming user-prompt task."""
    if entry.id in state.analysis_tasks:
        return

    entry.analysis_started = True
    entry.status = "streaming"
    entry.result = entry.result or "Analyzing..."
    entry.updated_at = time.time()
    state.refresh_analyzing()

    task = asyncio.create_task(
        _run_streaming_prompt(entry, state),
        name=f"termfix-prompt-{entry.id[:8]}",
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


async def _run_streaming_prompt(entry: PromptEntry, state: "TermFixState") -> None:
    """Update a manual prompt entry as provider chunks arrive."""
    try:
        await state.notify_ui_update()
        if entry.session is not None and state.connection is not None:
            try:
                entry.context = await collect_context(
                    state.connection,
                    entry.session,
                    context_lines=DEFAULT_CONTEXT_LINES,
                    command="",
                    exit_code=0,
                )
            except Exception as exc:
                logger.warning("Could not refresh prompt context: %s", exc)
        async for snapshot in stream_user_prompt(
            entry.context,
            entry.user_prompt,
            api_key=state.api_key,
            base_url=state.base_url,
            model=state.model,
            messages=list(entry.messages),
        ):
            entry.result = snapshot or entry.result
            entry.updated_at = time.time()
        if entry.result:
            entry.messages.append({"role": "assistant", "content": entry.result})
            entry.result = None
        entry.status = "done"
        entry.updated_at = time.time()
    except asyncio.CancelledError:
        entry.status = "cancelled"
        entry.updated_at = time.time()
        raise
    except Exception as exc:
        logger.error("LLM prompt failed: %s", exc, exc_info=True)
        entry.result = f"""\
### Cause
Prompt failed.

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

        def do_POST(self):  # noqa: N802 - BaseHTTPRequestHandler API.
            self._handle_request()

        def do_GET(self):  # noqa: N802 - BaseHTTPRequestHandler API.
            self._handle_request()

        def _handle_request(self) -> None:
            parsed = urlparse(self.path)
            if parsed.path == "/closed":
                entry_id = parse_qs(parsed.query).get("entry", [""])[0]
                if entry_id:
                    state.mark_popover_closed(entry_id)
                self._send_json({"ok": True})
                return

            if parsed.path == "/prompt":
                if self.command != "POST":
                    self.send_error(405)
                    return
                entry_id = parse_qs(parsed.query).get("entry", [""])[0]
                try:
                    content_length = int(self.headers.get("Content-Length", "0") or 0)
                except ValueError:
                    content_length = 0
                raw_body = self.rfile.read(min(content_length, 64_000))
                try:
                    payload = json.loads(raw_body.decode("utf-8") or "{}")
                    prompt = str(payload.get("prompt", "")).strip()
                except Exception:
                    self._send_json({"ok": False, "error": "Invalid prompt payload."})
                    return
                self._send_json(_submit_prompt_from_thread(state, entry_id, prompt))
                return

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
            self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
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
    if entry_id == _INFO_POPOVER_ID:
        state.mark_popover_seen(entry_id)
        return {
            "ok": True,
            "status": "info",
            "done": False,
            "should_close": state.consume_popover_close_request(entry_id),
        }

    prompt_entry = state.get_prompt(entry_id)
    if prompt_entry is not None:
        state.mark_popover_seen(entry_id)
        done = prompt_entry.status in ("done", "error", "cancelled")
        return {
            "ok": True,
            "entry_id": prompt_entry.id,
            "status": prompt_entry.status,
            "done": done,
            "should_close": state.consume_popover_close_request(entry_id),
            "updated_at": prompt_entry.updated_at,
            "body_html": _conversation_to_html(
                prompt_entry.messages,
                prompt_entry.result or "",
            ),
        }

    entry = state.get_error(entry_id)
    if entry is None:
        return {
            "ok": False,
            "status": "missing",
            "done": True,
            "should_close": False,
            "body_html": "<p>This TermFix result is no longer available.</p>",
        }

    state.mark_popover_seen(entry_id)
    markdown = entry.result or "Analyzing..."
    done = entry.status in ("done", "error", "cancelled")
    return {
        "ok": True,
        "entry_id": entry.id,
        "status": entry.status,
        "done": done,
        "should_close": state.consume_popover_close_request(entry_id),
        "updated_at": entry.updated_at,
        "body_html": _markdown_to_html(markdown),
    }


def _submit_prompt_from_thread(state: "TermFixState", entry_id: str, prompt: str) -> dict:
    """Submit a prompt from the HTTP handler thread into the asyncio loop."""
    if not entry_id:
        return {"ok": False, "error": "Missing prompt entry."}
    if not prompt:
        return {"ok": False, "error": "Prompt is empty."}
    if state.loop is None:
        return {"ok": False, "error": "TermFix event loop is unavailable."}

    future = asyncio.run_coroutine_threadsafe(
        _submit_prompt_entry(entry_id, prompt[:16_000], state),
        state.loop,
    )
    try:
        return future.result(timeout=2)
    except Exception as exc:
        logger.error("Prompt submit failed: %s", exc, exc_info=True)
        return {"ok": False, "error": str(exc)}


async def _submit_prompt_entry(entry_id: str, prompt: str, state: "TermFixState") -> dict:
    """Record user text and start the model request for a prompt entry."""
    entry = state.get_prompt(entry_id)
    if entry is None:
        return {"ok": False, "error": "Prompt entry expired."}
    if entry.status == "streaming":
        return {"ok": False, "error": "Prompt is already running."}

    entry.user_prompt = prompt
    entry.messages.append({"role": "user", "content": prompt})
    entry.result = "Analyzing..."
    entry.updated_at = time.time()
    _start_prompt_analysis_task(entry, state)
    await state.notify_ui_update()
    return {"ok": True}


def _build_command_key_pattern(keycode) -> iterm2.KeystrokePattern:
    """Build a Command-only keystroke pattern for one ANSI key."""
    pattern = iterm2.KeystrokePattern()
    pattern.required_modifiers = [iterm2.Modifier.COMMAND]
    pattern.forbidden_modifiers = [
        iterm2.Modifier.CONTROL,
        iterm2.Modifier.OPTION,
        iterm2.Modifier.SHIFT,
    ]
    pattern.keycodes = [keycode]
    return pattern


def _termfix_hotkey_kind(keystroke) -> Optional[str]:
    """Return the TermFix action for supported key-down events."""
    if keystroke.action not in (
        iterm2.Keystroke.Action.NA,
        iterm2.Keystroke.Action.KEY_DOWN,
    ):
        return None
    modifiers = set(keystroke.modifiers)
    if modifiers != {iterm2.Modifier.COMMAND}:
        return None
    if keystroke.keycode == iterm2.Keycode.ANSI_J:
        return "fix"
    if keystroke.keycode == iterm2.Keycode.ANSI_L:
        return "prompt"
    return None


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
    close_endpoint = json.dumps(f"{state.status_server_url}/closed?entry={entry.id}")

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
    const closeEndpoint = {close_endpoint};
    const contentEl = document.getElementById("content");
    const statusEl = document.getElementById("status");
    let lastHtml = contentEl.innerHTML;
    let timer = null;
    let closing = false;

    function reportClosed() {{
      try {{
        if (navigator.sendBeacon) {{
          navigator.sendBeacon(closeEndpoint);
        }} else {{
          fetch(closeEndpoint, {{ method: "POST", keepalive: true }});
        }}
      }} catch (error) {{
        // Best effort only.
      }}
    }}

    function closePopover() {{
      closing = true;
      if (timer) {{
        clearInterval(timer);
        timer = null;
      }}
      reportClosed();
      window.close();
    }}

    async function refresh() {{
      try {{
        const sep = endpoint.includes("?") ? "&" : "?";
        const response = await fetch(endpoint + sep + "t=" + Date.now(), {{
          cache: "no-store"
        }});
        const data = await response.json();
        if (data.should_close) {{
          closePopover();
          return;
        }}
        if (data.body_html && data.body_html !== lastHtml) {{
          lastHtml = data.body_html;
          contentEl.innerHTML = data.body_html;
        }}
        if (data.status) {{
          statusEl.textContent = data.status;
          statusEl.className = "status " + data.status;
        }}
      }} catch (error) {{
        statusEl.textContent = "offline";
        statusEl.className = "status error";
      }}
    }}

    document.addEventListener("keydown", (event) => {{
      if (event.metaKey && !event.ctrlKey && !event.altKey && !event.shiftKey &&
          event.key && event.key.toLowerCase() === "j") {{
        event.preventDefault();
        closePopover();
      }}
    }});

    window.addEventListener("pagehide", () => {{
      if (!closing) {{
        reportClosed();
      }}
    }});

    refresh();
    timer = setInterval(refresh, 350);
  </script>
</body>
</html>"""


def _build_prompt_html(entry: PromptEntry, state: "TermFixState") -> str:
    """Build the manual prompt popover."""
    _ensure_status_server(state)
    endpoint = json.dumps(f"{state.status_server_url}/state?entry={entry.id}")
    submit_endpoint = json.dumps(f"{state.status_server_url}/prompt?entry={entry.id}")
    close_endpoint = json.dumps(f"{state.status_server_url}/closed?entry={entry.id}")
    body = _conversation_to_html(entry.messages, entry.result or "")

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
    header h1 {{ font-size: 14px; font-weight: 600; color: #0f766e; }}
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
    form {{
      display: flex;
      gap: 8px;
      align-items: flex-start;
      margin-bottom: 12px;
    }}
    textarea {{
      flex: 1;
      min-height: 64px;
      max-height: 112px;
      resize: vertical;
      border: 1px solid #d1d1d6;
      border-radius: 6px;
      padding: 8px 10px;
      font: inherit;
      line-height: 1.4;
      color: #1c1c1e;
      outline: none;
    }}
    textarea:focus {{
      border-color: #0f766e;
      box-shadow: 0 0 0 2px rgba(15, 118, 110, 0.12);
    }}
    textarea:disabled {{
      color: #636366;
      background: #f8f8fa;
    }}
    button {{
      min-width: 56px;
      height: 32px;
      border: 0;
      border-radius: 6px;
      background: #0f766e;
      color: #ffffff;
      font: inherit;
      font-weight: 600;
      cursor: pointer;
    }}
    button:disabled {{
      background: #c7c7cc;
      cursor: default;
    }}
    .conversation {{
      display: flex;
      flex-direction: column;
      gap: 12px;
      padding-bottom: 4px;
    }}
    .turn {{
      display: flex;
      flex-direction: column;
      gap: 4px;
    }}
    .role {{
      font-size: 10px;
      font-weight: 700;
      letter-spacing: 0;
      text-transform: uppercase;
      color: #8e8e93;
    }}
    .bubble {{
      border-radius: 6px;
      padding: 8px 10px;
      overflow-x: auto;
    }}
    .turn.user {{
      align-items: flex-end;
    }}
    .turn.user .bubble {{
      max-width: 92%;
      background: #e6f4f1;
      color: #163f3a;
    }}
    .turn.assistant .bubble {{
      background: #f8f8fa;
    }}
    .markdown h1,
    .markdown h2,
    .markdown h3 {{
      font-size: 13px;
      color: #0f766e;
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
      color: #30d158;
      background: transparent;
      border-radius: 0;
      padding: 0;
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
    <h1>TermFix Prompt</h1>
    <span id="status" class="status {html.escape(entry.status)}">{html.escape(entry.status)}</span>
  </header>

  <form id="prompt-form">
    <textarea id="prompt-input" placeholder="Ask about this terminal session" autofocus></textarea>
    <button id="send-button" type="submit">Send</button>
  </form>
  <div id="content" class="conversation">{body}</div>

  <script>
    const endpoint = {endpoint};
    const submitEndpoint = {submit_endpoint};
    const closeEndpoint = {close_endpoint};
    const formEl = document.getElementById("prompt-form");
    const promptEl = document.getElementById("prompt-input");
    const sendButton = document.getElementById("send-button");
    const contentEl = document.getElementById("content");
    const statusEl = document.getElementById("status");
    let lastHtml = contentEl.innerHTML;
    let timer = null;
    let closing = false;
    let busy = statusEl.textContent === "streaming";
    let composing = false;
    promptEl.disabled = busy;
    sendButton.disabled = busy;

    function reportClosed() {{
      try {{
        if (navigator.sendBeacon) {{
          navigator.sendBeacon(closeEndpoint);
        }} else {{
          fetch(closeEndpoint, {{ method: "POST", keepalive: true }});
        }}
      }} catch (error) {{
        // Best effort only.
      }}
    }}

    function closePopover() {{
      closing = true;
      if (timer) {{
        clearInterval(timer);
        timer = null;
      }}
      reportClosed();
      window.close();
    }}

    function setStatus(status) {{
      if (!status) {{
        return;
      }}
      statusEl.textContent = status;
      statusEl.className = "status " + status;
    }}

    function escapeHtml(text) {{
      return String(text)
        .replace(/&/g, "&amp;")
        .replace(/</g, "&lt;")
        .replace(/>/g, "&gt;")
        .replace(/"/g, "&quot;")
        .replace(/'/g, "&#39;");
    }}

    async function refresh() {{
      try {{
        const sep = endpoint.includes("?") ? "&" : "?";
        const response = await fetch(endpoint + sep + "t=" + Date.now(), {{
          cache: "no-store"
        }});
        const data = await response.json();
        if (data.should_close) {{
          closePopover();
          return;
        }}
        if (typeof data.body_html === "string" && data.body_html !== lastHtml) {{
          lastHtml = data.body_html;
          contentEl.innerHTML = data.body_html;
        }}
        setStatus(data.status);
        if (data.done && timer) {{
          clearInterval(timer);
          timer = null;
        }}
        if (data.done) {{
          busy = false;
          promptEl.disabled = false;
          sendButton.disabled = false;
          promptEl.focus();
        }}
      }} catch (error) {{
        setStatus("offline");
      }}
    }}

    async function submitPrompt() {{
      const prompt = promptEl.value.trim();
      if (!prompt || busy) {{
        return;
      }}
      busy = true;
      promptEl.disabled = true;
      sendButton.disabled = true;
      setStatus("streaming");
      promptEl.value = "";

      try {{
        const response = await fetch(submitEndpoint, {{
          method: "POST",
          headers: {{ "Content-Type": "application/json" }},
          body: JSON.stringify({{ prompt }})
        }});
        const data = await response.json();
        if (!data.ok) {{
          throw new Error(data.error || "Prompt failed.");
        }}
        refresh();
        if (!timer) {{
          timer = setInterval(refresh, 350);
        }}
      }} catch (error) {{
        setStatus("error");
        contentEl.innerHTML = "<p>" + escapeHtml(error.message || error) + "</p>";
        busy = false;
        promptEl.disabled = false;
        sendButton.disabled = false;
        promptEl.focus();
      }}
    }}

    formEl.addEventListener("submit", (event) => {{
      event.preventDefault();
      submitPrompt();
    }});

    promptEl.addEventListener("compositionstart", () => {{
      composing = true;
    }});

    promptEl.addEventListener("compositionend", () => {{
      composing = false;
    }});

    promptEl.addEventListener("keydown", (event) => {{
      const isComposing = composing || event.isComposing || event.keyCode === 229;
      if (event.key === "Enter" && !event.shiftKey && !isComposing) {{
        event.preventDefault();
        submitPrompt();
      }}
    }});

    document.addEventListener("keydown", (event) => {{
      if (event.metaKey && !event.ctrlKey && !event.altKey && !event.shiftKey &&
          event.key && event.key.toLowerCase() === "l") {{
        event.preventDefault();
        closePopover();
      }}
    }});

    window.addEventListener("pagehide", () => {{
      if (!closing) {{
        reportClosed();
      }}
    }});

    refresh();
    promptEl.focus();
  </script>
</body>
</html>"""


def _build_info_html(state: "TermFixState") -> str:
    """Build a small informational popover shown when there is no error yet."""
    base_url = html.escape(state.base_url or DEFAULT_BASE_URL)
    model = html.escape(state.model or DEFAULT_MODEL)
    api_key_status = "Configured" if state.api_key else "Missing"
    endpoint = json.dumps(f"{state.status_server_url}/state?entry={_INFO_POPOVER_ID}")
    close_endpoint = json.dumps(f"{state.status_server_url}/closed?entry={_INFO_POPOVER_ID}")

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
  <script>
    const endpoint = {endpoint};
    const closeEndpoint = {close_endpoint};
    let timer = null;
    let closing = false;

    function reportClosed() {{
      try {{
        if (navigator.sendBeacon) {{
          navigator.sendBeacon(closeEndpoint);
        }} else {{
          fetch(closeEndpoint, {{ method: "POST", keepalive: true }});
        }}
      }} catch (error) {{
        // Best effort only.
      }}
    }}

    function closePopover() {{
      closing = true;
      if (timer) {{
        clearInterval(timer);
        timer = null;
      }}
      reportClosed();
      window.close();
    }}

    async function refresh() {{
      try {{
        const sep = endpoint.includes("?") ? "&" : "?";
        const response = await fetch(endpoint + sep + "t=" + Date.now(), {{
          cache: "no-store"
        }});
        const data = await response.json();
        if (data.should_close) {{
          closePopover();
        }}
      }} catch (error) {{
        // Ignore transient errors.
      }}
    }}

    document.addEventListener("keydown", (event) => {{
      if (event.metaKey && !event.ctrlKey && !event.altKey && !event.shiftKey &&
          event.key && event.key.toLowerCase() === "j") {{
        event.preventDefault();
        closePopover();
      }}
    }});

    window.addEventListener("pagehide", () => {{
      if (!closing) {{
        reportClosed();
      }}
    }});

    refresh();
    timer = setInterval(refresh, 350);
  </script>
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
    _ensure_status_server(state)
    await _open_popover(connection, session_id, state, _build_info_html(state))
    state.mark_popover_seen(_INFO_POPOVER_ID)


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


def _conversation_to_html(messages: list[dict], current_response: str = "") -> str:
    """Render a prompt conversation transcript."""
    blocks: list[str] = []

    for message in messages:
        if not isinstance(message, dict):
            continue
        role = message.get("role")
        content = str(message.get("content") or "")
        if role == "user":
            blocks.append(
                '<section class="turn user">'
                '<div class="role">You</div>'
                f'<div class="bubble">{_plain_text_to_html(content)}</div>'
                "</section>"
            )
        elif role == "assistant":
            blocks.append(
                '<section class="turn assistant">'
                '<div class="role">TermFix</div>'
                f'<div class="bubble markdown">{_markdown_to_html(content)}</div>'
                "</section>"
            )

    if current_response:
        blocks.append(
            '<section class="turn assistant">'
            '<div class="role">TermFix</div>'
            f'<div class="bubble markdown">{_markdown_to_html(current_response)}</div>'
            "</section>"
        )

    return "\n".join(blocks)


def _plain_text_to_html(text: str) -> str:
    """Render user-authored prompt text without interpreting Markdown."""
    return html.escape(text).replace("\n", "<br>")


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
    """Render inline code and bold markers."""
    parts = text.split("`")
    rendered: list[str] = []
    for i, part in enumerate(parts):
        if i % 2 == 1:
            rendered.append(f"<code>{html.escape(part)}</code>")
        else:
            rendered.append(_inline_bold_to_html(part))
    return "".join(rendered)


def _inline_bold_to_html(text: str) -> str:
    """Render non-empty **bold** spans while preserving literal asterisks."""
    rendered: list[str] = []
    pos = 0

    while pos < len(text):
        start = text.find("**", pos)
        if start == -1:
            rendered.append(html.escape(text[pos:]))
            break

        end = text.find("**", start + 2)
        if end == -1:
            rendered.append(html.escape(text[pos:]))
            break

        content = text[start + 2:end]
        rendered.append(html.escape(text[pos:start]))
        if content.strip():
            rendered.append(f"<strong>{html.escape(content)}</strong>")
        else:
            rendered.append(html.escape(text[start:end + 2]))
        pos = end + 2

    return "".join(rendered)
