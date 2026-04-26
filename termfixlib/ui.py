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
import os
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
    PROMPT_POPOVER_HEIGHT,
    PROMPT_POPOVER_WIDTH,
    STATUS_ERROR_FMT,
    STATUS_IDENTIFIER,
    STATUS_NORMAL,
)
from .context import collect_context
from .llm_client import stream_analyze_error, stream_user_prompt
from .monitor import PromptEntry

logger = logging.getLogger(__name__)

_INFO_POPOVER_ID = "__info__"
_PROMPT_POPOVER_ID = "__prompt__"


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

    if state.is_popover_open(_PROMPT_POPOVER_ID):
        state.request_popover_close(_PROMPT_POPOVER_ID)
        logger.info("TermFix prompt popover close requested")
        return

    session = app.get_session_by_id(session_id)
    if session is None:
        logger.debug("Cannot open prompt popover; session disappeared: %s", session_id)
        return

    try:
        _attach_prompt_history_to_session(state, session_id, session)
        entry = await _pick_or_create_prompt_entry(state, session_id, session)
        await _open_popover(
            connection,
            session_id,
            state,
            _build_prompt_html(entry, state),
            size=iterm2.Size(PROMPT_POPOVER_WIDTH, PROMPT_POPOVER_HEIGHT),
        )
        state.mark_popover_seen(_PROMPT_POPOVER_ID)
        state.mark_popover_seen(entry.id)
    except Exception as exc:
        logger.error("Failed to open prompt popover: %s", exc, exc_info=True)


async def _pick_or_create_prompt_entry(
    state: "TermFixState",
    session_id: str,
    session,
) -> PromptEntry:
    """Reuse the latest empty prompt; otherwise start a new conversation."""
    latest = state.latest_prompt(session_id)
    if latest is not None and not latest.messages and latest.status == "input":
        latest.session = session
        return latest

    entry = PromptEntry(session_id=session_id, context={}, session=session)
    await state.add_prompt(entry)
    return entry


def _attach_prompt_history_to_session(
    state: "TermFixState",
    session_id: str,
    session,
) -> None:
    """Make persisted prompt history resumable from the current iTerm2 session."""
    for entry in state.prompts:
        if entry.status != "streaming":
            entry.session_id = session_id
            entry.session = session


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
                    context_lines=state.context_lines,
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
        state.save_prompt_history()
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
                    if state.get_prompt(entry_id) is not None:
                        state.mark_popover_closed(_PROMPT_POPOVER_ID)
                    state.mark_popover_closed(entry_id)
                self._send_json({"ok": True})
                return

            if parsed.path == "/prompt/new":
                if self.command != "POST":
                    self.send_error(405)
                    return
                entry_id = parse_qs(parsed.query).get("entry", [""])[0]
                self._send_json(_create_prompt_from_thread(state, entry_id))
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
        state.mark_popover_seen(_PROMPT_POPOVER_ID)
        state.mark_popover_seen(entry_id)
        done = prompt_entry.status in ("done", "error", "cancelled")
        return {
            "ok": True,
            "entry_id": prompt_entry.id,
            "session_id": prompt_entry.session_id,
            "status": prompt_entry.status,
            "done": done,
            "should_close": (
                state.consume_popover_close_request(_PROMPT_POPOVER_ID)
                or state.consume_popover_close_request(entry_id)
            ),
            "updated_at": prompt_entry.updated_at,
            "history_html": _prompt_history_to_html(state, prompt_entry.session_id, entry_id),
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


def _create_prompt_from_thread(state: "TermFixState", entry_id: str) -> dict:
    """Create a new prompt conversation based on an existing prompt's session."""
    if not entry_id:
        return {"ok": False, "error": "Missing prompt entry."}
    if state.loop is None:
        return {"ok": False, "error": "TermFix event loop is unavailable."}

    future = asyncio.run_coroutine_threadsafe(
        _create_prompt_entry(entry_id, state),
        state.loop,
    )
    try:
        return future.result(timeout=2)
    except Exception as exc:
        logger.error("Prompt creation failed: %s", exc, exc_info=True)
        return {"ok": False, "error": str(exc)}


async def _create_prompt_entry(entry_id: str, state: "TermFixState") -> dict:
    """Create an empty prompt conversation for the same session as entry_id."""
    current = state.get_prompt(entry_id)
    if current is None:
        return {"ok": False, "error": "Prompt entry expired."}
    if not current.messages and current.status == "input":
        return {"ok": True, "entry_id": current.id}

    entry = PromptEntry(
        session_id=current.session_id,
        context={},
        session=current.session,
    )
    await state.add_prompt(entry)
    state.mark_popover_seen(_PROMPT_POPOVER_ID)
    state.mark_popover_seen(entry.id)
    return {"ok": True, "entry_id": entry.id}


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
    state.save_prompt_history()
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
    state_endpoint = json.dumps(f"{state.status_server_url}/state")
    submit_endpoint = json.dumps(f"{state.status_server_url}/prompt")
    new_endpoint = json.dumps(f"{state.status_server_url}/prompt/new")
    close_endpoint = json.dumps(f"{state.status_server_url}/closed")
    active_entry_id = json.dumps(entry.id)
    history = _prompt_history_to_html(state, entry.session_id, entry.id)
    body = _conversation_to_html(entry.messages, entry.result or "")
    cwd_label = html.escape(_prompt_cwd_label(entry.context))
    title_suffix = f' <span class="title-dot">&middot;</span> {cwd_label}' if cwd_label else ""
    context_label = html.escape(
        f"Context attached · last {state.context_lines} lines of output"
    )

    return f"""\
<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <style>
    *, *::before, *::after {{ box-sizing: border-box; margin: 0; padding: 0; }}
    :root {{
      --paper: #f6f4ed;
      --panel: #f0eee7;
      --line: #d8d5cb;
      --line-strong: #c7c3b8;
      --ink: #161616;
      --soft-ink: #3b3a36;
      --muted: #77756d;
      --field: #f7f5ee;
      --green: #24a579;
      --shadow: 0 12px 35px rgba(40, 39, 34, 0.12);
      --mono: "SF Mono", "Menlo", "Monaco", "Cascadia Mono", monospace;
      --sans: "Avenir Next", "PingFang SC", "Hiragino Sans GB", "Helvetica Neue", sans-serif;
    }}
    html, body {{
      height: 100%;
    }}
    body {{
      min-height: 100%;
      padding: 14px;
      overflow: hidden;
      color: var(--ink);
      background:
        linear-gradient(rgba(23, 22, 18, 0.018) 50%, transparent 50%) 0 0 / 100% 4px,
        var(--paper);
      font-family: var(--sans);
      font-size: 13px;
      line-height: 1.45;
    }}
    .window {{
      height: 100%;
      display: flex;
      flex-direction: column;
      overflow: hidden;
      border: 1px solid var(--line);
      border-radius: 14px;
      background: #fbfbf8;
      box-shadow: var(--shadow);
    }}
    .window-bar {{
      position: relative;
      height: 52px;
      flex: 0 0 52px;
      display: flex;
      align-items: center;
      justify-content: center;
      border-bottom: 1px solid var(--line);
      background: #f2f0e8;
    }}
    .traffic {{
      position: absolute;
      left: 18px;
      display: flex;
      gap: 9px;
    }}
    .traffic-dot {{
      width: 12px;
      height: 12px;
      border-radius: 50%;
    }}
    .traffic-dot.red {{ background: #ff5f57; }}
    .traffic-dot.yellow {{ background: #ffbd2e; }}
    .traffic-dot.green {{ background: #28c840; }}
    .window-title {{
      max-width: 62%;
      overflow: hidden;
      color: #3d3c38;
      font-family: var(--mono);
      font-size: 14px;
      font-weight: 700;
      letter-spacing: 0.02em;
      text-align: center;
      text-overflow: ellipsis;
      white-space: nowrap;
    }}
    .title-dot {{
      color: var(--muted);
      padding: 0 4px;
    }}
    .shortcut {{
      position: absolute;
      right: 18px;
      min-width: 30px;
      height: 22px;
      display: inline-flex;
      align-items: center;
      justify-content: center;
      border: 1px solid var(--line);
      border-radius: 5px;
      background: rgba(255, 255, 255, 0.7);
      color: var(--muted);
      font-family: var(--mono);
      font-size: 12px;
      font-weight: 700;
    }}
    .status {{
      display: none;
    }}
    .prompt-shell {{
      flex: 1;
      min-height: 0;
      display: flex;
    }}
    .history-pane {{
      width: 206px;
      flex: 0 0 206px;
      min-height: 0;
      display: flex;
      flex-direction: column;
      padding: 12px 10px;
      border-right: 1px solid var(--line);
      background: var(--panel);
    }}
    .new-chat {{
      width: 100%;
      height: 36px;
      flex: 0 0 36px;
      margin: 0 0 18px;
      border: 1px solid var(--line);
      border-radius: 8px;
      background: #ffffff;
      color: var(--ink);
      font: 800 14px var(--sans);
      cursor: pointer;
    }}
    .new-chat::before {{
      content: "+";
      margin-right: 10px;
      font-family: var(--mono);
      font-weight: 800;
    }}
    .history-list {{
      flex: 1;
      min-height: 0;
      overflow-y: auto;
      padding: 0 2px 8px;
    }}
    .history-group {{
      margin: 16px 0 7px 2px;
      color: var(--muted);
      font-size: 11px;
      font-weight: 800;
      letter-spacing: 0.06em;
      text-transform: uppercase;
    }}
    .history-group:first-child {{
      margin-top: 10px;
    }}
    .history-item {{
      width: 100%;
      min-height: 50px;
      display: block;
      margin-bottom: 7px;
      padding: 9px 10px;
      border: 1px solid transparent;
      border-radius: 8px;
      background: transparent;
      color: var(--ink);
      text-align: left;
      cursor: pointer;
    }}
    .history-item:hover {{
      background: rgba(255, 255, 255, 0.55);
    }}
    .history-item.active {{
      border-color: var(--line);
      background: #ffffff;
    }}
    .history-main {{
      display: flex;
      align-items: baseline;
      gap: 8px;
    }}
    .history-title {{
      min-width: 0;
      flex: 1;
      overflow: hidden;
      font-size: 13px;
      font-weight: 800;
      line-height: 1.25;
      text-overflow: ellipsis;
      white-space: nowrap;
    }}
    .history-time {{
      flex: 0 0 auto;
      color: var(--muted);
      font-family: var(--mono);
      font-size: 11px;
      font-weight: 700;
    }}
    .history-preview {{
      display: block;
      margin-top: 3px;
      overflow: hidden;
      color: var(--soft-ink);
      font-size: 12px;
      line-height: 1.25;
      text-overflow: ellipsis;
      white-space: nowrap;
    }}
    .chat-pane {{
      flex: 1;
      min-width: 0;
      min-height: 0;
      display: flex;
      flex-direction: column;
      background: #ffffff;
    }}
    .conversation {{
      flex: 1;
      min-height: 0;
      display: flex;
      flex-direction: column;
      gap: 14px;
      overflow-y: auto;
      padding: 34px 34px 26px;
      background: #ffffff;
    }}
    .empty-state {{
      width: min(420px, 88%);
      margin: auto;
      color: var(--soft-ink);
      text-align: center;
    }}
    .terminal-glyph {{
      width: 46px;
      height: 46px;
      display: flex;
      align-items: center;
      justify-content: center;
      margin: 0 auto 18px;
      border: 1px solid var(--line);
      border-radius: 12px;
      background: var(--field);
      color: #67645d;
      font-family: var(--mono);
      font-size: 19px;
      font-weight: 800;
    }}
    .empty-state h2 {{
      margin-bottom: 4px;
      color: var(--ink);
      font-size: 17px;
      font-weight: 850;
      letter-spacing: -0.02em;
    }}
    .empty-state p {{
      margin-bottom: 22px;
      font-size: 14px;
    }}
    .starter-list {{
      display: grid;
      gap: 8px;
      margin-bottom: 24px;
    }}
    .starter {{
      height: 36px;
      display: flex;
      align-items: center;
      gap: 12px;
      padding: 0 14px;
      border: 1px solid var(--line);
      border-radius: 8px;
      background: #ffffff;
      color: var(--ink);
      font-family: var(--mono);
      font-size: 13px;
      font-weight: 700;
      text-align: left;
      cursor: pointer;
    }}
    .starter:hover {{
      border-color: var(--line-strong);
      background: #fbfaf6;
    }}
    .starter-arrow {{
      color: var(--muted);
      font-family: var(--sans);
      font-size: 17px;
      line-height: 1;
    }}
    .tip {{
      color: var(--muted);
      font-size: 12px;
      font-weight: 700;
    }}
    kbd {{
      display: inline-flex;
      min-width: 27px;
      height: 20px;
      align-items: center;
      justify-content: center;
      margin: 0 5px;
      border: 1px solid var(--line);
      border-radius: 4px;
      background: var(--field);
      color: var(--soft-ink);
      font-family: var(--mono);
      font-size: 11px;
      font-weight: 700;
    }}
    .turn {{
      display: flex;
      flex-direction: column;
      gap: 5px;
    }}
    .role {{
      color: var(--muted);
      font-size: 10px;
      font-weight: 800;
      letter-spacing: 0.08em;
      text-transform: uppercase;
    }}
    .bubble {{
      max-width: 86%;
      padding: 10px 12px;
      border: 1px solid var(--line);
      border-radius: 10px;
      overflow-x: auto;
      background: #fbfaf6;
      color: var(--ink);
      line-height: 1.55;
    }}
    .turn.user {{
      align-items: flex-end;
    }}
    .turn.user .bubble {{
      border-color: #111111;
      background: #111111;
      color: #ffffff;
    }}
    .turn.user .role {{
      margin-right: 4px;
    }}
    .turn.assistant .bubble {{
      border-color: var(--line);
      background: #fbfaf6;
    }}
    form {{
      flex: 0 0 auto;
      padding: 12px 12px 14px;
      border-top: 1px solid var(--line);
      background: #ffffff;
    }}
    .context-line {{
      display: flex;
      align-items: center;
      gap: 7px;
      margin: 0 0 7px 4px;
      color: var(--muted);
      font-family: var(--mono);
      font-size: 12px;
      font-weight: 700;
    }}
    .context-dot {{
      width: 7px;
      height: 7px;
      border-radius: 50%;
      background: var(--green);
    }}
    .input-row {{
      display: flex;
      gap: 8px;
      align-items: stretch;
    }}
    textarea {{
      flex: 1;
      min-height: 42px;
      max-height: 112px;
      resize: vertical;
      padding: 12px 14px;
      border: 1px solid var(--line);
      border-radius: 10px;
      outline: none;
      background: var(--field);
      color: var(--ink);
      font: 13px/1.35 var(--mono);
    }}
    textarea::placeholder {{
      color: #77756d;
    }}
    textarea:focus {{
      border-color: var(--line-strong);
      box-shadow: 0 0 0 3px rgba(216, 213, 203, 0.35);
    }}
    textarea:disabled {{
      color: var(--muted);
      background: #ece9df;
    }}
    #send-button {{
      min-width: 78px;
      border: 0;
      border-radius: 10px;
      background: #111111;
      color: #ffffff;
      font: 800 13px var(--sans);
      cursor: pointer;
    }}
    #send-button kbd {{
      margin-left: 7px;
      border-color: rgba(255, 255, 255, 0.2);
      background: transparent;
      color: rgba(255, 255, 255, 0.72);
    }}
    button:disabled,
    #send-button:disabled {{
      opacity: 0.45;
      cursor: default;
    }}
    .markdown h1,
    .markdown h2,
    .markdown h3 {{
      margin: 11px 0 6px;
      color: var(--ink);
      font-size: 13px;
    }}
    .markdown h1:first-child,
    .markdown h2:first-child,
    .markdown h3:first-child {{ margin-top: 0; }}
    .markdown p {{ margin: 0 0 8px; color: var(--soft-ink); }}
    .markdown ul,
    .markdown ol {{ margin: 0 0 8px 18px; padding: 0; }}
    .markdown li {{ margin-bottom: 4px; }}
    .markdown pre {{
      margin: 7px 0 10px;
      padding: 9px 11px;
      overflow-x: auto;
      border-radius: 8px;
      background: #1d1d1b;
    }}
    .markdown code {{
      padding: 1px 4px;
      border-radius: 4px;
      background: #eeebe2;
      font-family: var(--mono);
      font-size: 12px;
    }}
    .markdown pre code {{
      padding: 0;
      border-radius: 0;
      background: transparent;
      color: #f4f1e7;
    }}
    ::-webkit-scrollbar {{ width: 10px; height: 10px; }}
    ::-webkit-scrollbar-thumb {{
      border: 3px solid transparent;
      border-radius: 999px;
      background-clip: padding-box;
      background-color: rgba(80, 77, 68, 0.24);
    }}
    ::-webkit-scrollbar-track {{ background: transparent; }}
    @keyframes dots {{
      0% {{ content: ""; }}
      25% {{ content: "."; }}
      50% {{ content: ".."; }}
      75%, 100% {{ content: "..."; }}
    }}
  </style>
</head>
<body>
  <div class="window">
    <header class="window-bar">
      <div class="traffic" aria-hidden="true">
        <span class="traffic-dot red"></span>
        <span class="traffic-dot yellow"></span>
        <span class="traffic-dot green"></span>
      </div>
      <h1 class="window-title">TermFix{title_suffix}</h1>
      <kbd class="shortcut">&#8984;L</kbd>
      <span id="status" class="status {html.escape(entry.status)}">{html.escape(entry.status)}</span>
    </header>

    <div class="prompt-shell">
      <aside class="history-pane">
        <button id="new-chat-button" class="new-chat" type="button">New chat</button>
        <div id="history-list" class="history-list">{history}</div>
      </aside>
      <section class="chat-pane">
        <div id="content" class="conversation">{body}</div>
        <form id="prompt-form">
          <div class="context-line">
            <span class="context-dot" aria-hidden="true"></span>
            <span>{context_label}</span>
          </div>
          <div class="input-row">
            <textarea id="prompt-input" placeholder="Ask about this terminal session..." autofocus></textarea>
            <button id="send-button" type="submit">Send <kbd>&#8984;&#8617;</kbd></button>
          </div>
        </form>
      </section>
    </div>
  </div>

  <script>
    const stateEndpoint = {state_endpoint};
    const submitEndpoint = {submit_endpoint};
    const newEndpoint = {new_endpoint};
    const closeEndpoint = {close_endpoint};
    let activeEntryId = {active_entry_id};
    const formEl = document.getElementById("prompt-form");
    const promptEl = document.getElementById("prompt-input");
    const sendButton = document.getElementById("send-button");
    const newChatButton = document.getElementById("new-chat-button");
    const historyListEl = document.getElementById("history-list");
    const contentEl = document.getElementById("content");
    const statusEl = document.getElementById("status");
    let lastHtml = contentEl.innerHTML;
    let timer = null;
    let closing = false;
    let busy = statusEl.textContent === "streaming";
    let composing = false;
    promptEl.disabled = busy;
    sendButton.disabled = busy;

    function withEntry(url) {{
      return url + "?entry=" + encodeURIComponent(activeEntryId);
    }}

    function reportClosed() {{
      try {{
        if (navigator.sendBeacon) {{
          navigator.sendBeacon(withEntry(closeEndpoint));
        }} else {{
          fetch(withEntry(closeEndpoint), {{ method: "POST", keepalive: true }});
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

    function scrollContentToBottom() {{
      contentEl.scrollTop = contentEl.scrollHeight;
    }}

    async function refresh() {{
      try {{
        const response = await fetch(withEntry(stateEndpoint) + "&t=" + Date.now(), {{
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
          scrollContentToBottom();
        }}
        if (typeof data.history_html === "string") {{
          historyListEl.innerHTML = data.history_html;
        }}
        setStatus(data.status);
        busy = data.status === "streaming";
        promptEl.disabled = busy;
        sendButton.disabled = busy;
        if (busy && !timer) {{
          timer = setInterval(refresh, 350);
        }}
        if (!busy && timer) {{
          clearInterval(timer);
          timer = null;
        }}
        if (!busy) {{
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
        const response = await fetch(withEntry(submitEndpoint), {{
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

    function setActiveEntry(entryId) {{
      if (!entryId || entryId === activeEntryId) {{
        return;
      }}
      activeEntryId = entryId;
      lastHtml = "";
      promptEl.value = "";
      refresh();
    }}

    formEl.addEventListener("submit", (event) => {{
      event.preventDefault();
      submitPrompt();
    }});

    newChatButton.addEventListener("click", async () => {{
      try {{
        const response = await fetch(withEntry(newEndpoint), {{ method: "POST" }});
        const data = await response.json();
        if (!data.ok) {{
          throw new Error(data.error || "Could not create conversation.");
        }}
        setActiveEntry(data.entry_id);
      }} catch (error) {{
        setStatus("error");
        contentEl.innerHTML = "<p>" + escapeHtml(error.message || error) + "</p>";
      }}
    }});

    historyListEl.addEventListener("click", (event) => {{
      const item = event.target.closest("[data-entry-id]");
      if (!item) {{
        return;
      }}
      setActiveEntry(item.getAttribute("data-entry-id"));
    }});

    contentEl.addEventListener("click", (event) => {{
      const starter = event.target.closest("[data-prompt]");
      if (!starter || busy) {{
        return;
      }}
      promptEl.value = starter.getAttribute("data-prompt") || "";
      promptEl.focus();
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
    scrollContentToBottom();
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
    size: Optional[iterm2.Size] = None,
) -> None:
    """Open a popover using the registered component when available."""
    popover_size = size or iterm2.Size(POPOVER_WIDTH, POPOVER_HEIGHT)
    if state.component is not None:
        await state.component.async_open_popover(
            session_id=session_id,
            html=html_content,
            size=popover_size,
        )
        return

    await iterm2.async_open_popover(
        connection=connection,
        session_id=session_id,
        html=html_content,
        size=popover_size,
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


def _prompt_history_to_html(
    state: "TermFixState",
    session_id: str,
    active_entry_id: str,
) -> str:
    """Render prompt history buttons for the current terminal session."""
    entries = [
        entry for entry in state.prompts if entry.session_id == session_id and entry.messages
    ]
    if not entries:
        return ""

    blocks: list[str] = []
    last_group = ""
    for entry in reversed(entries[-20:]):
        group = _prompt_history_group(entry.timestamp)
        if group != last_group:
            blocks.append(f'<div class="history-group">{html.escape(group)}</div>')
            last_group = group

        active = " active" if entry.id == active_entry_id else ""
        status = " running" if entry.status == "streaming" else ""
        title = html.escape(_prompt_history_title(entry, limit=30))
        full_title = html.escape(_prompt_history_title(entry, limit=None))
        preview = html.escape(_prompt_history_preview(entry))
        timestamp = html.escape(time.strftime("%H:%M", time.localtime(entry.timestamp)))
        blocks.append(
            f'<button class="history-item{active}{status}" '
            f'data-entry-id="{html.escape(entry.id)}" title="{full_title}" type="button">'
            '<span class="history-main">'
            f'<span class="history-title">{title}</span>'
            f'<span class="history-time">{timestamp}</span>'
            "</span>"
            f'<span class="history-preview">{preview}</span>'
            "</button>"
        )
    return "\n".join(blocks)


def _prompt_cwd_label(context: dict) -> str:
    """Return a compact cwd label for the prompt window title."""
    cwd = str((context or {}).get("cwd") or "").strip()
    if not cwd:
        return ""

    home = os.path.expanduser("~")
    if cwd == home:
        return "~"
    if cwd.startswith(home + os.sep):
        return "~" + cwd[len(home) :]
    return cwd


def _prompt_history_group(timestamp: float) -> str:
    day = time.strftime("%Y-%m-%d", time.localtime(timestamp))
    today = time.strftime("%Y-%m-%d", time.localtime())
    yesterday = time.strftime("%Y-%m-%d", time.localtime(time.time() - 86400))

    if day == today:
        return "TODAY"
    if day == yesterday:
        return "YESTERDAY"
    return time.strftime("%b %d", time.localtime(timestamp)).upper()


def _prompt_history_title(entry: PromptEntry, limit: Optional[int] = 48) -> str:
    """Return a short title for a prompt conversation."""
    for message in entry.messages:
        if not isinstance(message, dict) or message.get("role") != "user":
            continue
        content = str(message.get("content") or "").strip().splitlines()
        if content and content[0].strip():
            title = content[0].strip()
            if limit is not None and len(title) > limit:
                return title[:limit] + "..."
            return title
    return "New chat"


def _prompt_history_preview(entry: PromptEntry, limit: int = 36) -> str:
    """Return one secondary line for the history list."""
    for message in reversed(entry.messages):
        if not isinstance(message, dict):
            continue
        if message.get("role") != "assistant":
            continue
        preview = _compact_text(str(message.get("content") or ""))
        if preview:
            return _truncate_text(preview, limit)

    if entry.result:
        preview = _compact_text(entry.result)
        if preview:
            return _truncate_text(preview, limit)

    title = _compact_text(_prompt_history_title(entry, limit=None))
    return _truncate_text(title, limit)


def _compact_text(text: str) -> str:
    """Collapse markdown-ish text into a single plain preview line."""
    cleaned = (
        text.replace("`", "")
        .replace("#", "")
        .replace("*", "")
        .replace(">", "")
        .replace("-", " ")
    )
    return " ".join(cleaned.split())


def _truncate_text(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    return text[:limit].rstrip() + "..."


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

    if blocks:
        return "\n".join(blocks)

    return """\
<div class="empty-state">
  <div class="terminal-glyph">$_</div>
  <h2>Stuck on a command?</h2>
  <p>Ask about the last error, or paste a command for help.</p>
  <div class="starter-list">
    <button class="starter" type="button" data-prompt="Why did this command fail?">
      <span class="starter-arrow">&rsaquo;</span>
      <span>Why did this command fail?</span>
    </button>
    <button class="starter" type="button" data-prompt="Explain the last output">
      <span class="starter-arrow">&rsaquo;</span>
      <span>Explain the last output</span>
    </button>
    <button class="starter" type="button" data-prompt="Suggest a fix for this error">
      <span class="starter-arrow">&rsaquo;</span>
      <span>Suggest a fix for this error</span>
    </button>
  </div>
  <div class="tip">Tip &middot; Press <kbd>&#8984;L</kbd> anywhere in the terminal</div>
</div>"""


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
