"""
StatusBar component and popover UI for TermFix.

AutoLaunch only loads the top-level termfix.py script. This module stays in a
support package so iTerm2 does not try to execute it as a standalone script.
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import html
import json
import logging
import os
import re
import secrets
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Awaitable, Callable, Optional
from urllib.parse import parse_qs, urlencode, urlparse

try:
    import iterm2
except ImportError:  # Allows pure rendering helpers to be imported outside iTerm2.
    iterm2 = None  # type: ignore[assignment]

from .config import (
    DEFAULT_BASE_URL,
    DEFAULT_CONTEXT_LINES,
    DEFAULT_FIX_HOTKEY,
    DEFAULT_MAX_TOKENS,
    DEFAULT_MODEL,
    DEFAULT_PROMPT_HOTKEY,
    MAX_CONTEXT_LINES,
    MAX_MAX_TOKENS,
    MIN_CONTEXT_LINES,
    MIN_MAX_TOKENS,
    POPOVER_HEIGHT,
    POPOVER_WIDTH,
    PROMPT_POPOVER_HEIGHT,
    PROMPT_POPOVER_WIDTH,
    STATUS_ERROR_FMT,
    STATUS_IDENTIFIER,
    STATUS_NORMAL,
    normalize_base_url,
    normalize_command_hotkey,
    normalize_max_tokens,
)
from .context import collect_context, normalize_context_lines
from .llm_client import stream_analyze_error, stream_user_prompt
from .monitor import PromptEntry

logger = logging.getLogger(__name__)

_INFO_POPOVER_ID = "__info__"
_PROMPT_POPOVER_ID = "__prompt__"
_STATE_LOOP_CALL_TIMEOUT = 2
_STATUS_REGISTER_TIMEOUT = 10
_POPOVER_CORS_ORIGIN = "null"
_POPOVER_ROUTES = frozenset(
    {"/cancel", "/closed", "/insert", "/prompt/new", "/prompt", "/state"}
)
_API_KEY_LABEL_RE = re.compile(
    r"^\s*(?:api\s*key|openai[_\s-]*api[_\s-]*key|authorization|bearer|token)\s*[:=]",
    re.IGNORECASE,
)
_API_KEY_LABELED_VALUE_RE = re.compile(
    r"^\s*(?:api\s*key|openai[_\s-]*api[_\s-]*key|authorization|token)\s*[:=]\s*(.+)$",
    re.IGNORECASE | re.DOTALL,
)
_API_KEY_BEARER_RE = re.compile(r"^\s*bearer\s+(.+)$", re.IGNORECASE | re.DOTALL)
_CODE_BLOCK_COPY_CSS = """\
    .code-block {
      position: relative;
      margin: 7px 0 10px;
      border-radius: 8px;
      background: #1d1d1b;
      overflow: hidden;
    }
    .code-actions {
      position: absolute;
      top: 7px;
      right: 7px;
      z-index: 1;
      display: flex;
      gap: 5px;
    }
    .code-action {
      height: 24px;
      min-width: 50px;
      padding: 0 8px;
      border: 1px solid rgba(255, 255, 255, 0.14);
      border-radius: 6px;
      background: rgba(255, 255, 255, 0.08);
      color: rgba(255, 255, 255, 0.78);
      font: 700 11px var(--sans, -apple-system, BlinkMacSystemFont, "Helvetica Neue", sans-serif);
      cursor: pointer;
    }
    .code-action:hover {
      background: rgba(255, 255, 255, 0.14);
      color: #ffffff;
    }
    .code-action.copied,
    .code-action.inserted {
      color: #ffffff;
      background: rgba(36, 165, 121, 0.5);
    }
    .code-action.needs-manual-copy,
    .code-action.error {
      color: #ffffff;
      background: rgba(191, 90, 90, 0.55);
    }
    .markdown pre {
      margin: 0;
      padding: 9px 11px;
      padding-right: 132px;
      overflow-x: auto;
      background: #1d1d1b;
    }
"""
_CODE_BLOCK_COPY_JS = """\
    async function copyText(text) {
      if (navigator.clipboard && navigator.clipboard.writeText) {
        await navigator.clipboard.writeText(text);
        return;
      }
      const copyTarget = document.createElement("textarea");
      copyTarget.value = text;
      copyTarget.setAttribute("readonly", "");
      copyTarget.style.position = "fixed";
      copyTarget.style.left = "-9999px";
      document.body.appendChild(copyTarget);
      copyTarget.select();
      try {
        if (!document.execCommand("copy")) {
          throw new Error("Clipboard copy failed.");
        }
      } finally {
        document.body.removeChild(copyTarget);
      }
    }

    function selectCodeText(code) {
      const selection = window.getSelection ? window.getSelection() : null;
      const range = document.createRange ? document.createRange() : null;
      if (!selection || !range) {
        return false;
      }
      range.selectNodeContents(code);
      selection.removeAllRanges();
      selection.addRange(range);
      return true;
    }

    function resetCodeButton(button, label, className, delay) {
      setTimeout(() => {
        button.textContent = label;
        if (className) {
          button.classList.remove(className);
        }
      }, delay);
    }

    async function insertCodeText(text) {
      if (typeof insertEndpoint !== "string" || !insertEndpoint) {
        throw new Error("Insert endpoint is unavailable.");
      }
      const response = await fetch(insertEndpoint, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ text })
      });
      const data = await response.json();
      if (!data.ok) {
        throw new Error(data.error || "Insert failed.");
      }
    }

    function handleCodeBlockCopy(event) {
      const target = event.target && event.target.closest ? event.target : null;
      const copyButton = target ? target.closest("[data-copy-code]") : null;
      const insertButton = target ? target.closest("[data-insert-code]") : null;
      const actionButton = copyButton || insertButton;
      if (!actionButton) {
        return false;
      }

      const block = actionButton.closest(".code-block");
      const code = block ? block.querySelector("code") : null;
      if (!code) {
        return true;
      }

      if (insertButton) {
        insertCodeText(code.textContent || "").then(() => {
          insertButton.textContent = "Inserted";
          insertButton.classList.add("inserted");
          resetCodeButton(insertButton, "Insert", "inserted", 1400);
        }).catch((error) => {
          insertButton.textContent = error.message || "Insert failed";
          insertButton.classList.add("error");
          resetCodeButton(insertButton, "Insert", "error", 2200);
        });
        return true;
      }

      copyText(code.textContent || "").then(() => {
        copyButton.textContent = "Copied";
        copyButton.classList.add("copied");
        resetCodeButton(copyButton, "Copy", "copied", 1200);
      }).catch(() => {
        const selected = selectCodeText(code);
        copyButton.textContent = selected ? "Selected - Cmd+C" : "Select code";
        copyButton.classList.remove("copied");
        copyButton.classList.add("needs-manual-copy");
        resetCodeButton(copyButton, "Copy", "needs-manual-copy", 2800);
      });
      return true;
    }
"""


def _popover_cors_origin(origin: str) -> Optional[str]:
    """Return the CORS origin accepted for iTerm's opaque popover document."""
    if origin == _POPOVER_CORS_ORIGIN:
        return _POPOVER_CORS_ORIGIN
    return None


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
            name=f"Context Lines ({MIN_CONTEXT_LINES}-{MAX_CONTEXT_LINES})",
            placeholder=str(DEFAULT_CONTEXT_LINES),
            default_value=str(DEFAULT_CONTEXT_LINES),
            key="context_lines",
        ),
        iterm2.StringKnob(
            name=f"Max Tokens ({MIN_MAX_TOKENS}-{MAX_MAX_TOKENS})",
            placeholder=str(DEFAULT_MAX_TOKENS),
            default_value=str(DEFAULT_MAX_TOKENS),
            key="max_tokens",
        ),
        iterm2.StringKnob(
            name="Fix Hotkey",
            placeholder=DEFAULT_FIX_HOTKEY,
            default_value=DEFAULT_FIX_HOTKEY,
            key="fix_hotkey",
        ),
        iterm2.StringKnob(
            name="Prompt Hotkey",
            placeholder=DEFAULT_PROMPT_HOTKEY,
            default_value=DEFAULT_PROMPT_HOTKEY,
            key="prompt_hotkey",
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
        unhandled_count = state.unhandled_error_count
        if unhandled_count == 0:
            return STATUS_NORMAL
        return STATUS_ERROR_FMT.format(count=unhandled_count)

    @iterm2.RPC
    async def _on_click(session_id):
        asyncio.create_task(
            _handle_click(connection, session_id, state, toggle=False),
            name="termfix-click",
        )

    try:
        await component.async_register(
            connection,
            _status_coro,
            timeout=_STATUS_REGISTER_TIMEOUT,
            onclick=_on_click,
        )
    except asyncio.TimeoutError as exc:
        message = (
            "TermFix status bar registration timed out. "
            "Reload the iTerm2 Python API script and try again."
        )
        logger.error(message)
        raise RuntimeError(message) from exc

    logger.info("TermFix status bar component registered (%s)", STATUS_IDENTIFIER)
    return state


async def start_hotkey_listener(
    connection: iterm2.Connection,
    app: iterm2.App,
    state: "TermFixState",
) -> None:
    """Listen for TermFix hotkeys in the active session."""
    patterns = _build_command_letter_patterns()

    try:
        logger.info("TermFix hotkey listener registered (%s, %s)", state.fix_hotkey, state.prompt_hotkey)
        async with iterm2.KeystrokeFilter(connection, patterns):
            async with iterm2.KeystrokeMonitor(connection, advanced=True) as mon:
                while True:
                    keystroke = await mon.async_get()
                    hotkey = _termfix_hotkey_kind(keystroke, state)
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
        state.prompt_sessions[session_id] = session
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
    """Attach only live empty placeholders; restored history stays detached."""
    for entry in state.prompts:
        if entry.status == "input" and not entry.messages and not entry.session_id:
            entry.session_id = session_id
            entry.session = session


def _start_analysis_task(entry, state: "TermFixState") -> None:
    """Launch one streaming analysis task for an entry."""
    _schedule_state_loop_callback(state, _start_analysis_task_on_loop, entry, state)


def _start_analysis_task_on_loop(entry, state: "TermFixState") -> None:
    """Launch one streaming analysis task for an entry on the state event loop."""
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
    _schedule_state_loop_callback(state, _start_prompt_analysis_task_on_loop, entry, state)


def _start_prompt_analysis_task_on_loop(entry: PromptEntry, state: "TermFixState") -> None:
    """Launch one streaming user-prompt task on the state event loop."""
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


def _schedule_state_loop_callback(
    state: "TermFixState",
    callback: Callable[..., None],
    *args,
) -> bool:
    """Schedule a state mutation callback on the owning asyncio loop."""
    loop = state.loop
    if loop is None:
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            logger.error("Cannot schedule TermFix state update; event loop is unavailable.")
            return False
        state.loop = loop
    try:
        if asyncio.get_running_loop() is loop:
            callback(*args)
            return True
    except RuntimeError:
        pass
    try:
        loop.call_soon_threadsafe(callback, *args)
    except RuntimeError as exc:
        logger.error("Cannot schedule TermFix state update: %s", exc)
        return False
    return True


async def _run_streaming_analysis(entry, state: "TermFixState") -> None:
    """Update entry.result as provider chunks arrive."""
    try:
        await state.notify_ui_update()
        async for snapshot in stream_analyze_error(
            entry.context,
            api_key=state.api_key,
            base_url=state.base_url,
            model=state.model,
            max_tokens=state.max_tokens,
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
                entry.context["context_lines"] = state.context_lines
                state.save_prompt_history()
            except Exception as exc:
                logger.warning("Could not refresh prompt context: %s", exc)
        async for snapshot in stream_user_prompt(
            entry.context,
            entry.user_prompt,
            api_key=state.api_key,
            base_url=state.base_url,
            model=state.model,
            max_tokens=state.max_tokens,
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
    """Prefer new errors, then reopen the last viewed result when nothing is pending."""
    if session_id:
        entry = state.latest_unhandled_error(session_id)
        if entry is not None:
            return entry
    entry = state.latest_unhandled_error()
    if entry is not None:
        return entry
    if session_id:
        entry = state.last_viewed_error(session_id)
        if entry is not None:
            return entry
    return state.last_viewed_error()


def _notify_ui_update_from_thread(state: "TermFixState") -> None:
    if state.loop is None:
        return
    try:
        asyncio.run_coroutine_threadsafe(state.notify_ui_update(), state.loop)
    except RuntimeError as exc:
        logger.debug("Could not schedule UI update: %s", exc)


def _mark_error_handled_from_thread(state: "TermFixState", entry_id: str) -> bool:
    handled_error = state.mark_error_handled(entry_id)
    if handled_error:
        _notify_ui_update_from_thread(state)
    return handled_error


def _status_endpoint(
    state: "TermFixState",
    path: str,
    query: Optional[dict[str, str]] = None,
) -> str:
    """Return a token-bearing local popover endpoint URL."""
    _ensure_status_server(state)
    token = state.status_server_token
    if not token:
        raise RuntimeError("TermFix status server token is unavailable")
    endpoint = f"{state.status_server_url}/{token}{path}"
    if query:
        endpoint = f"{endpoint}?{urlencode(query)}"
    return endpoint


def _ensure_status_server(state: "TermFixState") -> None:
    """Start a local JSON endpoint for popovers to poll live analysis state."""
    if state.status_server_url:
        return

    token = secrets.token_urlsafe(32)

    class Handler(BaseHTTPRequestHandler):
        def do_OPTIONS(self):  # noqa: N802 - BaseHTTPRequestHandler API.
            parsed = urlparse(self.path)
            request_path = self._authenticated_path(parsed)
            if request_path is None:
                self._send_json(
                    {"ok": False, "error": "Unauthorized."},
                    status=403,
                    allow_cors=False,
                )
                return
            if request_path not in _POPOVER_ROUTES:
                self.send_error(404)
                return
            self._send_json({})

        def do_POST(self):  # noqa: N802 - BaseHTTPRequestHandler API.
            self._handle_request()

        def do_GET(self):  # noqa: N802 - BaseHTTPRequestHandler API.
            self._handle_request()

        def _handle_request(self) -> None:
            parsed = urlparse(self.path)
            request_path = self._authenticated_path(parsed)
            if request_path is None:
                self._send_json(
                    {"ok": False, "error": "Unauthorized."},
                    status=403,
                    allow_cors=False,
                )
                return

            if request_path == "/closed":
                entry_id = parse_qs(parsed.query).get("entry", [""])[0]
                self._send_json(_mark_popover_closed_from_thread(state, entry_id))
                return

            if request_path == "/cancel":
                if self.command != "POST":
                    self.send_error(405)
                    return
                entry_id = parse_qs(parsed.query).get("entry", [""])[0]
                self._send_json(_cancel_analysis_from_thread(state, entry_id))
                return

            if request_path == "/prompt/new":
                if self.command != "POST":
                    self.send_error(405)
                    return
                entry_id = parse_qs(parsed.query).get("entry", [""])[0]
                session_id = parse_qs(parsed.query).get("session", [""])[0]
                self._send_json(_create_prompt_from_thread(state, entry_id, session_id))
                return

            if request_path == "/prompt":
                if self.command != "POST":
                    self.send_error(405)
                    return
                entry_id = parse_qs(parsed.query).get("entry", [""])[0]
                session_id = parse_qs(parsed.query).get("session", [""])[0]
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
                self._send_json(_submit_prompt_from_thread(state, entry_id, prompt, session_id))
                return

            if request_path == "/insert":
                if self.command != "POST":
                    self.send_error(405)
                    return
                session_id = parse_qs(parsed.query).get("session", [""])[0]
                try:
                    content_length = int(self.headers.get("Content-Length", "0") or 0)
                except ValueError:
                    content_length = 0
                raw_body = self.rfile.read(min(content_length, 128_000))
                try:
                    payload = json.loads(raw_body.decode("utf-8") or "{}")
                    text = str(payload.get("text", ""))
                except Exception:
                    self._send_json({"ok": False, "error": "Invalid insert payload."})
                    return
                self._send_json(_insert_code_from_thread(state, text, session_id))
                return

            if request_path != "/state":
                self.send_error(404)
                return

            entry_id = parse_qs(parsed.query).get("entry", [""])[0]
            session_id = parse_qs(parsed.query).get("session", [""])[0]
            self._send_json(_entry_payload_from_thread(state, entry_id, session_id))

        def _authenticated_path(self, parsed) -> Optional[str]:  # noqa: ANN001
            parts = parsed.path.split("/")
            if len(parts) < 3:
                return None
            candidate = parts[1]
            if not secrets.compare_digest(candidate, token):
                return None
            return "/" + "/".join(parts[2:])

        def _send_json(
            self,
            payload: dict,
            status: int = 200,
            allow_cors: bool = True,
        ) -> None:
            body = json.dumps(payload).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            cors_origin = _popover_cors_origin(self.headers.get("Origin", ""))
            if allow_cors and cors_origin:
                self.send_header("Access-Control-Allow-Origin", cors_origin)
                self.send_header("Vary", "Origin")
                self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
                self.send_header("Access-Control-Allow-Headers", "Content-Type")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, fmt, *args):  # noqa: ANN001
            # Default request-line logs include the bearer token embedded in the URL.
            return

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(
        target=server.serve_forever,
        name="termfix-popover-status",
        daemon=True,
    )
    thread.start()

    state.status_server = server
    state.status_server_url = f"http://127.0.0.1:{server.server_port}"
    state.status_server_token = token
    logger.info("TermFix popover status server listening on %s", state.status_server_url)


def _call_state_loop_from_thread(
    state: "TermFixState",
    make_coro: Callable[[], Awaitable[dict]],
    action: str,
) -> dict:
    """Run one state operation on the asyncio loop from an HTTP handler thread."""
    if state.loop is None:
        return {"ok": False, "error": "TermFix event loop is unavailable."}

    coro: Optional[Awaitable[dict]] = None
    future = None
    try:
        coro = make_coro()
        future = asyncio.run_coroutine_threadsafe(coro, state.loop)
        coro = None
        return future.result(timeout=_STATE_LOOP_CALL_TIMEOUT)
    except concurrent.futures.TimeoutError as exc:
        if future is not None:
            try:
                future.cancel()
            except Exception as cancel_exc:
                logger.debug("%s cancellation after timeout failed: %s", action, cancel_exc)
        logger.error("%s timed out: %s", action, exc, exc_info=True)
        return {"ok": False, "error": str(exc)}
    except Exception as exc:
        logger.error("%s failed: %s", action, exc, exc_info=True)
        return {"ok": False, "error": str(exc)}
    finally:
        if coro is not None:
            close = getattr(coro, "close", None)
            if close is not None:
                close()


def _mark_popover_closed_from_thread(state: "TermFixState", entry_id: str) -> dict:
    """Record a popover close event on the asyncio loop."""
    return _call_state_loop_from_thread(
        state,
        lambda: _mark_popover_closed(entry_id, state),
        "Popover close",
    )


async def _mark_popover_closed(entry_id: str, state: "TermFixState") -> dict:
    if entry_id:
        if state.get_prompt(entry_id) is not None:
            state.mark_popover_closed(_PROMPT_POPOVER_ID)
        else:
            _mark_error_handled_from_thread(state, entry_id)
        state.mark_popover_closed(entry_id)
    return {"ok": True}


def _cancel_analysis_from_thread(state: "TermFixState", entry_id: str) -> dict:
    """Cancel an in-flight error or prompt analysis task on the asyncio loop."""
    if not entry_id:
        return {"ok": False, "error": "Missing entry."}
    return _call_state_loop_from_thread(
        state,
        lambda: _cancel_analysis(entry_id, state),
        "Analysis cancel",
    )


async def _cancel_analysis(entry_id: str, state: "TermFixState") -> dict:
    entry = state.get_prompt(entry_id) or state.get_error(entry_id)
    if entry is None:
        return {"ok": False, "error": "Entry expired."}

    task = state.analysis_tasks.get(entry_id)
    cancelled = False
    if task is not None and not task.done():
        task.cancel()
        cancelled = True

    if getattr(entry, "status", "") == "streaming":
        entry.status = "cancelled"
        entry.updated_at = time.time()
    state.refresh_analyzing()
    await state.notify_ui_update()
    return {"ok": True, "cancelled": cancelled, "status": entry.status}


def _entry_payload_from_thread(
    state: "TermFixState",
    entry_id: str,
    popover_session_id: str = "",
) -> dict:
    """Build a state payload on the asyncio loop."""
    return _call_state_loop_from_thread(
        state,
        lambda: _entry_payload_on_loop(entry_id, state, popover_session_id),
        "State payload",
    )


async def _entry_payload_on_loop(
    entry_id: str,
    state: "TermFixState",
    popover_session_id: str = "",
) -> dict:
    return _entry_payload(state, entry_id, popover_session_id)


def _entry_payload(
    state: "TermFixState",
    entry_id: str,
    popover_session_id: str = "",
) -> dict:
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
            "history_html": _prompt_history_to_html(
                state,
                popover_session_id or prompt_entry.session_id,
                entry_id,
            ),
            "context_label": _prompt_context_label(
                prompt_entry,
                state,
                popover_session_id,
            ),
            "context_class": _prompt_context_class(prompt_entry, popover_session_id),
            "body_html": _conversation_to_html(
                prompt_entry.messages,
                prompt_entry.result or "",
                prompt_entry.context,
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

    _mark_error_handled_from_thread(state, entry_id)
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


def _submit_prompt_from_thread(
    state: "TermFixState",
    entry_id: str,
    prompt: str,
    popover_session_id: str = "",
) -> dict:
    """Submit a prompt from the HTTP handler thread into the asyncio loop."""
    if not entry_id:
        return {"ok": False, "error": "Missing prompt entry."}
    if not prompt:
        return {"ok": False, "error": "Prompt is empty."}
    return _call_state_loop_from_thread(
        state,
        lambda: _submit_prompt_entry(entry_id, prompt[:16_000], state, popover_session_id),
        "Prompt submit",
    )


def _create_prompt_from_thread(
    state: "TermFixState",
    entry_id: str,
    popover_session_id: str = "",
) -> dict:
    """Create a new prompt conversation based on an existing prompt's session."""
    if not entry_id:
        return {"ok": False, "error": "Missing prompt entry."}
    return _call_state_loop_from_thread(
        state,
        lambda: _create_prompt_entry(entry_id, state, popover_session_id),
        "Prompt creation",
    )


def _insert_code_from_thread(
    state: "TermFixState",
    text: str,
    popover_session_id: str = "",
) -> dict:
    """Insert code text into the target iTerm2 session from the HTTP handler thread."""
    if not text:
        return {"ok": False, "error": "Code block is empty."}
    return _call_state_loop_from_thread(
        state,
        lambda: _insert_code_text(text[:64_000], state, popover_session_id),
        "Code insert",
    )


async def _insert_code_text(
    text: str,
    state: "TermFixState",
    popover_session_id: str = "",
) -> dict:
    session, session_id = await _resolve_insert_session(state, popover_session_id)
    if session is None:
        return {"ok": False, "error": "Current terminal session is unavailable."}

    send_text = getattr(session, "async_send_text", None)
    if send_text is None:
        return {"ok": False, "error": "Terminal session cannot accept inserted text."}

    try:
        try:
            await send_text(text, suppress_broadcast=True)
        except TypeError:
            await send_text(text)
    except Exception as exc:
        logger.warning("Could not insert code into session %s: %s", session_id, exc)
        return {"ok": False, "error": str(exc)}

    return {"ok": True, "session_id": session_id}


async def _create_prompt_entry(
    entry_id: str,
    state: "TermFixState",
    popover_session_id: str = "",
) -> dict:
    """Create an empty prompt conversation for the same session as entry_id."""
    current = state.get_prompt(entry_id)
    if current is None:
        return {"ok": False, "error": "Prompt entry expired."}
    if not current.messages and current.status == "input":
        return {"ok": True, "entry_id": current.id}

    session_id = current.session_id or popover_session_id
    entry = PromptEntry(
        session_id=session_id,
        context={},
        session=current.session or state.prompt_sessions.get(session_id),
    )
    await state.add_prompt(entry)
    state.mark_popover_seen(_PROMPT_POPOVER_ID)
    state.mark_popover_seen(entry.id)
    return {"ok": True, "entry_id": entry.id}


async def _submit_prompt_entry(
    entry_id: str,
    prompt: str,
    state: "TermFixState",
    popover_session_id: str = "",
) -> dict:
    """Record user text and start the model request for a prompt entry."""
    entry = state.get_prompt(entry_id)
    if entry is None:
        return {"ok": False, "error": "Prompt entry expired."}
    if entry.status == "streaming":
        return {"ok": False, "error": "Prompt is already running."}

    if _is_detached_prompt(entry) and not await _resume_prompt_in_session(
        entry,
        state,
        popover_session_id,
    ):
        return {
            "ok": False,
            "error": (
                "Current terminal session is unavailable or closed. "
                f"Reopen {getattr(state, 'prompt_hotkey', DEFAULT_PROMPT_HOTKEY)} and try again."
            ),
        }
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


def _build_command_letter_patterns() -> list[iterm2.KeystrokePattern]:
    """Build Command-only patterns for all available ANSI letter keys."""
    patterns = []
    for letter in "ABCDEFGHIJKLMNOPQRSTUVWXYZ":
        keycode = _ansi_letter_keycode(letter)
        if keycode is not None:
            patterns.append(_build_command_key_pattern(keycode))
    return patterns


def _ansi_letter_keycode(letter: str):
    """Return the iTerm ANSI keycode for a letter when available."""
    if iterm2 is None:
        return None
    keycode = getattr(iterm2, "Keycode", None)
    return getattr(keycode, f"ANSI_{letter.upper()}", None)


def _keystroke_ansi_letter(keystroke) -> str:
    """Return the ANSI letter represented by a keystroke keycode."""
    for letter in "ABCDEFGHIJKLMNOPQRSTUVWXYZ":
        if keystroke.keycode == _ansi_letter_keycode(letter):
            return letter
    return ""


def _hotkey_letter(value: str) -> str:
    """Extract the configured letter from a normalized Cmd+X hotkey."""
    text = str(value or "").strip()
    if "+" not in text:
        return ""
    return text.rsplit("+", 1)[-1].strip().upper()


def _termfix_hotkey_kind(keystroke, state: Optional["TermFixState"] = None) -> Optional[str]:
    """Return the TermFix action for supported key-down events."""
    if keystroke.action not in (
        iterm2.Keystroke.Action.NA,
        iterm2.Keystroke.Action.KEY_DOWN,
    ):
        return None
    modifiers = set(keystroke.modifiers)
    if modifiers != {iterm2.Modifier.COMMAND}:
        return None
    letter = _keystroke_ansi_letter(keystroke)
    fix_hotkey = getattr(state, "fix_hotkey", DEFAULT_FIX_HOTKEY)
    prompt_hotkey = getattr(state, "prompt_hotkey", DEFAULT_PROMPT_HOTKEY)
    if letter and letter == _hotkey_letter(fix_hotkey):
        return "fix"
    if letter and letter == _hotkey_letter(prompt_hotkey):
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
    endpoint = json.dumps(_status_endpoint(state, "/state", {"entry": entry.id}))
    close_endpoint = json.dumps(_status_endpoint(state, "/closed", {"entry": entry.id}))
    close_key = json.dumps(
        _hotkey_letter(getattr(state, "fix_hotkey", DEFAULT_FIX_HOTKEY)).lower()
    )
    insert_endpoint = json.dumps(
        _status_endpoint(state, "/insert", {"session": getattr(entry, "session_id", "")})
    )

    return f"""\
<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <style>
    *, *::before, *::after {{ box-sizing: border-box; margin: 0; padding: 0; }}
    :root {{
      --paper: #ffffff;
      --panel: #ffffff;
      --line: #e5e5ea;
      --ink: #1c1c1e;
      --soft-ink: #3c3c43;
      --muted: #8e8e93;
      --field: #f2f2f7;
      --accent: #c0392b;
      --code-ink: #30d158;
      --sans: -apple-system, BlinkMacSystemFont, "SF Pro Text", "Helvetica Neue", sans-serif;
      --mono: "SF Mono", "Menlo", "Monaco", "Cascadia Mono", monospace;
    }}
    @media (prefers-color-scheme: dark) {{
      :root {{
        --paper: #1c1c1e;
        --panel: #242426;
        --line: #3a3a3c;
        --ink: #f5f5f7;
        --soft-ink: #d1d1d6;
        --muted: #98989d;
        --field: #2c2c2e;
        --accent: #ff6961;
        --code-ink: #32d74b;
      }}
    }}
    body {{
      font-family: var(--sans);
      font-size: 13px;
      color: var(--ink);
      background: var(--paper);
      padding: 14px 16px;
      line-height: 1.45;
    }}
    header {{
      display: flex;
      align-items: center;
      gap: 8px;
      margin-bottom: 12px;
      padding-bottom: 10px;
      border-bottom: 1px solid var(--line);
    }}
    header h1 {{ font-size: 14px; font-weight: 650; color: var(--accent); }}
    .badge {{
      font-size: 11px;
      font-weight: 500;
      background: var(--field);
      border-radius: 4px;
      padding: 1px 6px;
      color: var(--muted);
      font-family: var(--mono);
    }}
    .markdown h1,
    .markdown h2,
    .markdown h3 {{
      font-size: 13px;
      color: var(--accent);
      margin: 12px 0 6px;
    }}
    .markdown h1:first-child,
    .markdown h2:first-child,
    .markdown h3:first-child {{ margin-top: 0; }}
    .markdown p {{ margin: 0 0 8px; color: var(--soft-ink); }}
    .markdown ul,
    .markdown ol {{ margin: 0 0 8px 18px; padding: 0; }}
    .markdown li {{ margin-bottom: 4px; }}
{_CODE_BLOCK_COPY_CSS}
    .markdown code {{
      font-family: var(--mono);
      font-size: 12px;
      background: var(--field);
      border-radius: 4px;
      padding: 1px 4px;
    }}
    .markdown pre code {{
      font-family: var(--mono);
      font-size: 12px;
      color: var(--code-ink);
      background: transparent;
      border-radius: 0;
      padding: 0;
    }}
    .failed-cmd {{
      font-family: var(--mono);
      font-size: 11px;
      color: var(--muted);
      background: var(--field);
      border-radius: 4px;
      padding: 1px 6px;
    }}
    .status {{
      margin-left: auto;
      font-size: 11px;
      color: var(--muted);
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
    <h1>TermFix</h1>
    <span class="failed-cmd">{failed_cmd}</span>
    <span class="badge">exit {exit_code}</span>
    <span id="status" class="status {html.escape(entry.status)}">{html.escape(entry.status)}</span>
  </header>

  <div id="content" class="markdown">{body}</div>
  <script>
    const endpoint = {endpoint};
    const closeEndpoint = {close_endpoint};
    const insertEndpoint = {insert_endpoint};
    const contentEl = document.getElementById("content");
    const statusEl = document.getElementById("status");
    let lastHtml = contentEl.innerHTML;
    let timer = null;
    let pollDelay = 350;
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

{_CODE_BLOCK_COPY_JS}

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
        const nextDelay = data.done ? 2000 : 350;
        if (timer && nextDelay !== pollDelay) {{
          clearInterval(timer);
          pollDelay = nextDelay;
          timer = setInterval(refresh, pollDelay);
        }}
      }} catch (error) {{
        statusEl.textContent = "offline";
        statusEl.className = "status error";
      }}
    }}

    contentEl.addEventListener("click", handleCodeBlockCopy);

    document.addEventListener("keydown", (event) => {{
      if (event.metaKey && !event.ctrlKey && !event.altKey && !event.shiftKey &&
          event.key && event.key.toLowerCase() === {close_key}) {{
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
    timer = setInterval(refresh, pollDelay);
  </script>
</body>
</html>"""


def _build_prompt_html(entry: PromptEntry, state: "TermFixState") -> str:
    """Build the manual prompt popover."""
    _ensure_status_server(state)
    state_endpoint = json.dumps(_status_endpoint(state, "/state"))
    submit_endpoint = json.dumps(_status_endpoint(state, "/prompt"))
    new_endpoint = json.dumps(_status_endpoint(state, "/prompt/new"))
    cancel_endpoint = json.dumps(_status_endpoint(state, "/cancel"))
    close_endpoint = json.dumps(_status_endpoint(state, "/closed"))
    insert_endpoint = json.dumps(_status_endpoint(state, "/insert", {"session": entry.session_id}))
    active_entry_id = json.dumps(entry.id)
    popover_session_id = json.dumps(entry.session_id)
    history = _prompt_history_to_html(state, entry.session_id, entry.id)
    body = _conversation_to_html(entry.messages, entry.result or "", entry.context)
    cwd_label = html.escape(_prompt_cwd_label(entry.context))
    title_suffix = f' <span class="title-dot">&middot;</span> {cwd_label}' if cwd_label else ""
    context_label = html.escape(_prompt_context_label(entry, state, entry.session_id))
    context_class = html.escape(_prompt_context_class(entry, entry.session_id))
    prompt_hotkey = getattr(state, "prompt_hotkey", DEFAULT_PROMPT_HOTKEY)
    close_key = json.dumps(_hotkey_letter(prompt_hotkey).lower())
    shortcut_label = html.escape(prompt_hotkey.replace("Cmd+", "⌘"))

    return f"""\
<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <style>
    *, *::before, *::after {{ box-sizing: border-box; margin: 0; padding: 0; }}
    :root {{
      --paper: #ffffff;
      --panel: #f7f7f9;
      --panel-strong: #ffffff;
      --line: #e5e5ea;
      --line-strong: #c7c7cc;
      --ink: #1c1c1e;
      --soft-ink: #3c3c43;
      --muted: #8e8e93;
      --field: #f2f2f7;
      --green: #24a579;
      --accent: #c0392b;
      --button-bg: #111111;
      --button-ink: #ffffff;
      --shadow: 0 18px 45px rgba(0, 0, 0, 0.12);
      --mono: "SF Mono", "Menlo", "Monaco", "Cascadia Mono", monospace;
      --sans: -apple-system, BlinkMacSystemFont, "SF Pro Text", "Helvetica Neue", sans-serif;
    }}
    @media (prefers-color-scheme: dark) {{
      :root {{
        --paper: #1c1c1e;
        --panel: #242426;
        --panel-strong: #2c2c2e;
        --line: #3a3a3c;
        --line-strong: #545458;
        --ink: #f5f5f7;
        --soft-ink: #d1d1d6;
        --muted: #98989d;
        --field: #2c2c2e;
        --accent: #ff6961;
        --button-bg: #f5f5f7;
        --button-ink: #1c1c1e;
        --shadow: 0 18px 45px rgba(0, 0, 0, 0.36);
      }}
    }}
    html, body {{
      height: 100%;
    }}
    body {{
      min-height: 100%;
      padding: 14px;
      overflow: hidden;
      color: var(--ink);
      background: var(--paper);
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
      border-radius: 12px;
      background: var(--panel-strong);
      box-shadow: var(--shadow);
    }}
    .window-bar {{
      position: relative;
      height: 50px;
      flex: 0 0 50px;
      display: flex;
      align-items: center;
      gap: 10px;
      padding: 0 16px;
      border-bottom: 1px solid var(--line);
      background: var(--panel);
    }}
    .window-title {{
      min-width: 0;
      flex: 1;
      overflow: hidden;
      color: var(--ink);
      font-family: var(--sans);
      font-size: 14px;
      font-weight: 650;
      letter-spacing: 0;
      text-align: left;
      text-overflow: ellipsis;
      white-space: nowrap;
    }}
    .title-dot {{
      color: var(--muted);
      padding: 0 4px;
    }}
    .shortcut {{
      min-width: 30px;
      height: 22px;
      display: inline-flex;
      align-items: center;
      justify-content: center;
      border: 1px solid var(--line);
      border-radius: 5px;
      background: var(--panel-strong);
      color: var(--muted);
      font-family: var(--mono);
      font-size: 12px;
      font-weight: 700;
    }}
    .status {{
      color: var(--muted);
      font-size: 11px;
      font-weight: 650;
    }}
    .status.streaming {{
      color: var(--accent);
    }}
    .status.streaming::before {{
      content: "";
      display: inline-block;
      width: 6px;
      height: 6px;
      margin-right: 6px;
      border-radius: 999px;
      background: currentColor;
      animation: pulse 1s ease-in-out infinite;
    }}
    .status.streaming::after {{
      content: "";
      display: inline-block;
      width: 1em;
      animation: dots 1.1s steps(4, end) infinite;
    }}
    .prompt-shell {{
      flex: 1;
      min-height: 0;
      display: flex;
    }}
    .history-pane {{
      width: clamp(172px, 28%, 236px);
      flex: 0 0 clamp(172px, 28%, 236px);
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
      background: var(--panel-strong);
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
      background: var(--field);
    }}
    .history-item.active {{
      border-color: var(--line);
      background: var(--panel-strong);
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
    .history-badge {{
      flex: 0 0 auto;
      color: #8a6d16;
      font-family: var(--mono);
      font-size: 10px;
      font-weight: 800;
      text-transform: uppercase;
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
      background: var(--panel-strong);
    }}
    .conversation {{
      flex: 1;
      min-height: 0;
      display: flex;
      flex-direction: column;
      gap: 14px;
      overflow-y: auto;
      padding: clamp(22px, 4vw, 34px) clamp(22px, 4vw, 34px) 26px;
      background: var(--panel-strong);
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
      color: var(--muted);
      font-family: var(--mono);
      font-size: 19px;
      font-weight: 800;
    }}
    .empty-state h2 {{
      margin-bottom: 4px;
      color: var(--ink);
      font-size: 17px;
      font-weight: 850;
      letter-spacing: 0;
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
      background: var(--panel-strong);
      color: var(--ink);
      font-family: var(--mono);
      font-size: 13px;
      font-weight: 700;
      text-align: left;
      cursor: pointer;
    }}
    .starter:hover {{
      border-color: var(--line-strong);
      background: var(--field);
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
      background: var(--field);
      color: var(--ink);
      line-height: 1.55;
    }}
    .turn.user {{
      align-items: flex-end;
    }}
    .turn.user .bubble {{
      border-color: var(--ink);
      background: var(--button-bg);
      color: var(--button-ink);
    }}
    .turn.user .role {{
      margin-right: 4px;
    }}
    .turn.assistant .bubble {{
      border-color: var(--line);
      background: var(--field);
    }}
    .streaming-note {{
      display: none;
      align-items: center;
      gap: 8px;
      color: var(--muted);
      font-size: 12px;
      font-weight: 650;
    }}
    .chat-pane.busy .streaming-note {{
      display: flex;
    }}
    .spinner {{
      width: 12px;
      height: 12px;
      border: 2px solid var(--line);
      border-top-color: var(--accent);
      border-radius: 999px;
      animation: spin 0.8s linear infinite;
    }}
    form {{
      flex: 0 0 auto;
      padding: 12px 12px 14px;
      border-top: 1px solid var(--line);
      background: var(--panel-strong);
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
    .context-line.restored .context-dot {{
      background: #b58300;
    }}
    .context-line.empty .context-dot {{
      background: var(--muted);
    }}
    .input-row {{
      display: flex;
      gap: 8px;
      align-items: stretch;
    }}
    textarea {{
      flex: 1;
      min-height: 42px;
      max-height: 180px;
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
      color: var(--muted);
    }}
    textarea:focus {{
      border-color: var(--line-strong);
      box-shadow: 0 0 0 3px rgba(0, 122, 255, 0.16);
    }}
    textarea:disabled {{
      color: var(--muted);
      background: var(--panel);
    }}
    .input-actions {{
      flex: 0 0 auto;
      display: flex;
      gap: 8px;
      align-items: stretch;
    }}
    #send-button,
    #stop-button {{
      min-width: 78px;
      border: 0;
      border-radius: 10px;
      font: 800 13px var(--sans);
      cursor: pointer;
    }}
    #send-button {{
      background: var(--button-bg);
      color: var(--button-ink);
    }}
    #stop-button {{
      display: none;
      border: 1px solid #c55345;
      background: #ffffff;
      color: #a63b30;
    }}
    .input-actions.streaming #send-button {{
      display: none;
    }}
    .input-actions.streaming #stop-button {{
      display: inline-block;
    }}
    #send-button kbd {{
      margin-left: 7px;
      border-color: currentColor;
      background: transparent;
      color: var(--button-ink);
      opacity: 0.72;
    }}
    button:disabled,
    #send-button:disabled,
    #stop-button:disabled {{
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
{_CODE_BLOCK_COPY_CSS}
    .markdown code {{
      padding: 1px 4px;
      border-radius: 4px;
      background: var(--field);
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
      background-color: rgba(142, 142, 147, 0.32);
    }}
    ::-webkit-scrollbar-track {{ background: transparent; }}
    @keyframes pulse {{
      0%, 100% {{ opacity: 0.35; transform: scale(0.9); }}
      50% {{ opacity: 1; transform: scale(1); }}
    }}
    @keyframes spin {{
      to {{ transform: rotate(360deg); }}
    }}
    @keyframes dots {{
      0% {{ content: ""; }}
      25% {{ content: "."; }}
      50% {{ content: ".."; }}
      75%, 100% {{ content: "..."; }}
    }}
    @media (max-width: 620px) {{
      body {{ padding: 10px; }}
      .prompt-shell {{ flex-direction: column; }}
      .history-pane {{
        width: auto;
        flex: 0 0 auto;
        max-height: 150px;
        border-right: 0;
        border-bottom: 1px solid var(--line);
      }}
      .new-chat {{
        margin-bottom: 10px;
      }}
      .history-list {{
        display: flex;
        gap: 8px;
        overflow-x: auto;
        overflow-y: hidden;
        padding-bottom: 4px;
      }}
      .history-group {{
        display: none;
      }}
      .history-item {{
        min-width: 180px;
        margin-bottom: 0;
      }}
      .bubble {{
        max-width: 96%;
      }}
    }}
  </style>
</head>
<body>
  <div class="window">
    <header class="window-bar">
      <h1 class="window-title">TermFix{title_suffix}</h1>
      <kbd class="shortcut">{shortcut_label}</kbd>
      <span id="status" class="status {html.escape(entry.status)}">{html.escape(entry.status)}</span>
    </header>

    <div class="prompt-shell">
      <aside class="history-pane">
        <button id="new-chat-button" class="new-chat" type="button">New chat</button>
        <div id="history-list" class="history-list">{history}</div>
      </aside>
      <section id="chat-pane" class="chat-pane">
        <div id="content" class="conversation">{body}</div>
        <form id="prompt-form">
          <div id="context-line" class="context-line {context_class}">
            <span class="context-dot" aria-hidden="true"></span>
            <span id="context-label">{context_label}</span>
          </div>
          <div id="streaming-note" class="streaming-note" role="status" aria-live="polite">
            <span class="spinner" aria-hidden="true"></span>
            <span>TermFix is responding</span>
          </div>
          <div class="input-row">
            <textarea id="prompt-input" placeholder="Ask about this terminal session..." autofocus></textarea>
            <div id="input-actions" class="input-actions">
              <button id="send-button" type="submit">Send <kbd>&#8984;&#8617;</kbd></button>
              <button id="stop-button" type="button">Stop</button>
            </div>
          </div>
        </form>
      </section>
    </div>
  </div>

  <script>
    const stateEndpoint = {state_endpoint};
    const submitEndpoint = {submit_endpoint};
    const newEndpoint = {new_endpoint};
    const cancelEndpoint = {cancel_endpoint};
    const closeEndpoint = {close_endpoint};
    const insertEndpoint = {insert_endpoint};
    let activeEntryId = {active_entry_id};
    const popoverSessionId = {popover_session_id};
    const formEl = document.getElementById("prompt-form");
    const promptEl = document.getElementById("prompt-input");
    const inputActionsEl = document.getElementById("input-actions");
    const sendButton = document.getElementById("send-button");
    const stopButton = document.getElementById("stop-button");
    const newChatButton = document.getElementById("new-chat-button");
    const historyListEl = document.getElementById("history-list");
    const chatPaneEl = document.getElementById("chat-pane");
    const contentEl = document.getElementById("content");
    const statusEl = document.getElementById("status");
    const contextLineEl = document.getElementById("context-line");
    const contextLabelEl = document.getElementById("context-label");
    let lastHtml = contentEl.innerHTML;
    let timer = null;
    let closing = false;
    let busy = statusEl.textContent === "streaming";
    let composing = false;
    setBusy(busy);

    function withEntry(url) {{
      let query = "?entry=" + encodeURIComponent(activeEntryId);
      if (popoverSessionId) {{
        query += "&session=" + encodeURIComponent(popoverSessionId);
      }}
      return url + query;
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

    function setBusy(isBusy) {{
      busy = Boolean(isBusy);
      promptEl.disabled = busy;
      sendButton.disabled = busy;
      stopButton.disabled = !busy;
      inputActionsEl.className = busy ? "input-actions streaming" : "input-actions";
      sendButton.textContent = busy ? "Sending..." : "Send";
      if (!busy) {{
        const shortcut = document.createElement("kbd");
        shortcut.innerHTML = "&#8984;&#8617;";
        sendButton.appendChild(shortcut);
      }}
      chatPaneEl.classList.toggle("busy", busy);
      chatPaneEl.setAttribute("aria-busy", busy ? "true" : "false");
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

{_CODE_BLOCK_COPY_JS}

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
        if (typeof data.context_label === "string") {{
          contextLabelEl.textContent = data.context_label;
        }}
        if (typeof data.context_class === "string") {{
          contextLineEl.className = "context-line " + data.context_class;
        }}
        setStatus(data.status);
        setBusy(data.status === "streaming");
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
      setBusy(true);
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
        setBusy(false);
        promptEl.focus();
      }}
    }}

    async function cancelPrompt() {{
      if (!busy) {{
        return;
      }}
      stopButton.disabled = true;
      try {{
        const response = await fetch(withEntry(cancelEndpoint), {{ method: "POST" }});
        const data = await response.json();
        if (!data.ok) {{
          throw new Error(data.error || "Cancel failed.");
        }}
        setStatus(data.status || "cancelled");
        setBusy(false);
        refresh();
      }} catch (error) {{
        setStatus("error");
        contentEl.innerHTML = "<p>" + escapeHtml(error.message || error) + "</p>";
        setBusy(false);
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

    stopButton.addEventListener("click", () => {{
      cancelPrompt();
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
      if (handleCodeBlockCopy(event)) {{
        return;
      }}

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
          event.key && event.key.toLowerCase() === {close_key}) {{
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
    max_tokens = html.escape(str(getattr(state, "max_tokens", DEFAULT_MAX_TOKENS)))
    fix_hotkey = html.escape(getattr(state, "fix_hotkey", DEFAULT_FIX_HOTKEY))
    prompt_hotkey = html.escape(getattr(state, "prompt_hotkey", DEFAULT_PROMPT_HOTKEY))
    api_key_error = getattr(state, "api_key_error", "")
    api_key_status = html.escape(api_key_error or ("Configured" if state.api_key else "Missing"))
    validation_errors = [
        getattr(state, "base_url_error", ""),
        getattr(state, "max_tokens_error", ""),
        getattr(state, "fix_hotkey_error", ""),
        getattr(state, "prompt_hotkey_error", ""),
        api_key_error,
    ]
    validation_html = "".join(
        f'<div class="validation-error">{html.escape(error)}</div>'
        for error in validation_errors
        if error
    )
    close_key = json.dumps(
        _hotkey_letter(getattr(state, "fix_hotkey", DEFAULT_FIX_HOTKEY)).lower()
    )
    endpoint = json.dumps(_status_endpoint(state, "/state", {"entry": _INFO_POPOVER_ID}))
    close_endpoint = json.dumps(
        _status_endpoint(state, "/closed", {"entry": _INFO_POPOVER_ID})
    )

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
    .validation-error {{
      margin: 0 0 6px 0;
      color: #b42318;
      font-weight: 600;
    }}
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
  <div class="item"><span class="label">Max Tokens:</span> <code>{max_tokens}</code></div>
  <div class="item"><span class="label">Hotkeys:</span> <code>{fix_hotkey}</code> / <code>{prompt_hotkey}</code></div>
  <div class="item"><span class="label">API Key:</span> <code>{api_key_status}</code></div>
  {validation_html}
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
          event.key && event.key.toLowerCase() === {close_key}) {{
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

    if "base_url" in knobs:
        base_url, base_url_error = normalize_base_url(
            knobs.get("base_url", ""),
            getattr(state, "base_url", DEFAULT_BASE_URL),
        )
        state.base_url = base_url
        state.base_url_error = base_url_error
        if base_url_error:
            logger.warning("Ignoring Base URL setting: %s", base_url_error)

    previous_api_key_error = getattr(state, "api_key_error", "")
    api_key, api_key_error = _normalize_api_key(knobs.get("api_key", ""))
    state.api_key = api_key
    state.api_key_error = api_key_error
    if api_key_error and api_key_error != previous_api_key_error:
        logger.warning("Ignoring API key setting: %s", api_key_error)

    model = knobs.get("model", "").strip()
    if model:
        state.model = model

    ctx_raw = knobs.get("context_lines", "").strip()
    if ctx_raw.isdigit():
        state.context_lines = normalize_context_lines(ctx_raw)

    if "max_tokens" in knobs:
        max_tokens, max_tokens_error = normalize_max_tokens(
            knobs.get("max_tokens", ""),
            getattr(state, "max_tokens", DEFAULT_MAX_TOKENS),
        )
        state.max_tokens = max_tokens
        state.max_tokens_error = max_tokens_error
        if max_tokens_error:
            logger.warning("Ignoring Max Tokens setting: %s", max_tokens_error)

    if "fix_hotkey" in knobs:
        fix_hotkey, fix_hotkey_error = normalize_command_hotkey(
            knobs.get("fix_hotkey", ""),
            getattr(state, "fix_hotkey", DEFAULT_FIX_HOTKEY),
            DEFAULT_FIX_HOTKEY,
        )
        state.fix_hotkey = fix_hotkey
        state.fix_hotkey_error = fix_hotkey_error
        if fix_hotkey_error:
            logger.warning("Ignoring Fix Hotkey setting: %s", fix_hotkey_error)

    if "prompt_hotkey" in knobs:
        prompt_hotkey, prompt_hotkey_error = normalize_command_hotkey(
            knobs.get("prompt_hotkey", ""),
            getattr(state, "prompt_hotkey", DEFAULT_PROMPT_HOTKEY),
            DEFAULT_PROMPT_HOTKEY,
        )
        state.prompt_hotkey = prompt_hotkey
        state.prompt_hotkey_error = prompt_hotkey_error
        if prompt_hotkey_error:
            logger.warning("Ignoring Prompt Hotkey setting: %s", prompt_hotkey_error)


def _normalize_api_key(value) -> tuple[str, str]:  # noqa: ANN001 - iTerm knob value.
    """Return a stripped API key or a user-readable validation error."""
    api_key = str(value or "").strip()
    if not api_key:
        return "", ""

    labeled_match = _API_KEY_LABELED_VALUE_RE.match(api_key)
    if labeled_match:
        api_key = labeled_match.group(1).strip()

    bearer_match = _API_KEY_BEARER_RE.match(api_key)
    if bearer_match:
        api_key = bearer_match.group(1).strip()

    if _API_KEY_LABEL_RE.search(api_key):
        return "", "Remove label text such as 'API Key:' and paste only the key."
    if any(char.isspace() for char in api_key):
        return "", "API key contains internal whitespace; paste only the key."
    return api_key, ""


def _prompt_history_to_html(
    state: "TermFixState",
    session_id: str,
    active_entry_id: str,
) -> str:
    """Render current-session history plus detached restored conversations."""
    entries = [
        entry
        for entry in state.prompts
        if entry.messages
        and (entry.session_id == session_id or _is_detached_prompt(entry))
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
        restored = " restored" if _is_detached_prompt(entry) else ""
        title = html.escape(_prompt_history_title(entry, limit=30))
        full_title = _prompt_history_title(entry, limit=None)
        if _is_detached_prompt(entry):
            full_title = f"{full_title} - restored history"
        full_title = html.escape(full_title)
        preview = html.escape(_prompt_history_preview(entry))
        timestamp = html.escape(time.strftime("%H:%M", time.localtime(entry.timestamp)))
        badge = '<span class="history-badge">Restored</span>' if restored else ""
        blocks.append(
            f'<button class="history-item{active}{status}{restored}" '
            f'data-entry-id="{html.escape(entry.id)}" title="{full_title}" type="button">'
            '<span class="history-main">'
            f'<span class="history-title">{title}</span>'
            f'<span class="history-time">{timestamp}</span>'
            f"{badge}"
            "</span>"
            f'<span class="history-preview">{preview}</span>'
            "</button>"
        )
    return "\n".join(blocks)


def _is_detached_prompt(entry: PromptEntry) -> bool:
    return bool(entry.messages and not entry.session_id)


async def _resume_prompt_in_session(
    entry: PromptEntry,
    state: "TermFixState",
    popover_session_id: str,
) -> bool:
    """Bind one selected restored conversation to the active session on submit."""
    if entry.session_id:
        return True
    if not popover_session_id:
        return False

    session, session_id = await _resolve_live_prompt_session(state, popover_session_id)
    if session is None:
        return False

    entry.session_id = session_id
    entry.session = session
    entry.restored = False
    entry.context = {}
    return True


async def _resolve_live_prompt_session(
    state: "TermFixState",
    popover_session_id: str,
) -> tuple[object | None, str]:
    """Return a live prompt session, preferring the popover session then active session."""
    app = None
    if state.connection is not None and iterm2 is not None and hasattr(iterm2, "async_get_app"):
        try:
            app = await iterm2.async_get_app(state.connection)
        except Exception as exc:
            logger.debug("Could not refresh iTerm app while resuming prompt: %s", exc)

    if app is not None:
        session = app.get_session_by_id(popover_session_id)
        if session is not None:
            state.prompt_sessions[popover_session_id] = session
            return session, popover_session_id

        active_session_id = await _get_active_session_id(app)
        if active_session_id:
            session = app.get_session_by_id(active_session_id)
            if session is not None:
                state.prompt_sessions[active_session_id] = session
                logger.info(
                    "Prompt session %s is unavailable; using active session %s",
                    popover_session_id,
                    active_session_id,
                )
                return session, active_session_id
        logger.warning("Prompt session is no longer live: %s", popover_session_id)
        return None, ""

    session = state.prompt_sessions.get(popover_session_id)
    if session is None:
        return None, ""
    stored_session_id = getattr(session, "session_id", popover_session_id)
    if stored_session_id != popover_session_id:
        logger.warning(
            "Stored prompt session id mismatch: expected %s, got %s",
            popover_session_id,
            stored_session_id,
        )
        return None, ""
    return session, popover_session_id


async def _resolve_insert_session(
    state: "TermFixState",
    popover_session_id: str = "",
):
    """Return a live terminal session for inserting code text."""
    sessions = getattr(state, "terminal_sessions", {})
    prompt_sessions = getattr(state, "prompt_sessions", {})
    if popover_session_id:
        session = sessions.get(popover_session_id) or prompt_sessions.get(popover_session_id)
        if session is not None and getattr(session, "session_id", popover_session_id) == popover_session_id:
            return session, popover_session_id

    connection = getattr(state, "connection", None)
    if connection is not None and iterm2 is not None:
        try:
            app = await iterm2.async_get_app(connection)
            if popover_session_id:
                session = app.get_session_by_id(popover_session_id)
                if session is not None:
                    sessions[popover_session_id] = session
                    return session, popover_session_id
            active_session_id = await _get_active_session_id(app)
            if active_session_id:
                session = app.get_session_by_id(active_session_id)
                if session is not None:
                    sessions[active_session_id] = session
                    return session, active_session_id
        except Exception as exc:
            logger.debug("Could not resolve live insert session: %s", exc)

    if popover_session_id:
        session = prompt_sessions.get(popover_session_id)
        if session is not None and getattr(session, "session_id", popover_session_id) == popover_session_id:
            return session, popover_session_id

    return None, ""


def _prompt_context_label(
    entry: PromptEntry,
    state: "TermFixState",
    popover_session_id: str = "",
) -> str:
    """Return the context status shown below the conversation."""
    if _is_detached_prompt(entry):
        cwd = _prompt_cwd_label(entry.context)
        origin = f" from {cwd}" if cwd else ""
        return f"Restored history{origin} - next reply uses current session context"

    if not entry.session_id:
        return "Current session context will attach when you send"

    if popover_session_id and entry.session_id != popover_session_id:
        cwd = _prompt_cwd_label(entry.context)
        origin = f" from {cwd}" if cwd else ""
        return f"Different session context{origin}"

    context_lines = _prompt_context_lines(entry.context, state)
    return f"Current session context - last {context_lines} lines of output"


def _prompt_context_class(entry: PromptEntry, popover_session_id: str = "") -> str:
    if _is_detached_prompt(entry):
        return "restored"
    if not entry.session_id:
        return "empty"
    if popover_session_id and entry.session_id != popover_session_id:
        return "restored"
    return "current"


def _prompt_context_lines(context: dict, state: "TermFixState") -> int:
    try:
        return int((context or {}).get("context_lines") or state.context_lines)
    except (TypeError, ValueError):
        return state.context_lines


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
    cleaned_lines: list[str] = []
    for line in text.splitlines():
        cleaned = line.strip()
        cleaned = re.sub(r"^>+\s*", "", cleaned)
        cleaned = re.sub(r"^#{1,6}\s*", "", cleaned)
        cleaned = re.sub(r"^(```+|~~~+)\s*\w*\s*$", "", cleaned)
        cleaned = re.sub(r"^[-+*]\s+", "", cleaned)
        cleaned = re.sub(r"^\d+[.)]\s+", "", cleaned)
        cleaned = cleaned.replace("`", "")
        cleaned = re.sub(r"\*\*(.*?)\*\*", r"\1", cleaned)
        cleaned = re.sub(r"__(.*?)__", r"\1", cleaned)
        if cleaned:
            cleaned_lines.append(cleaned)
    cleaned = " ".join(cleaned_lines)
    return " ".join(cleaned.split())


def _truncate_text(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    return text[:limit].rstrip() + "..."


def _conversation_to_html(
    messages: list[dict],
    current_response: str = "",
    context: Optional[dict] = None,
) -> str:
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

    starters = _prompt_starters(context or {})
    starter_buttons = "\n".join(
        '<button class="starter" type="button" data-prompt="'
        f'{html.escape(prompt, quote=True)}">'
        '<span class="starter-arrow">&rsaquo;</span>'
        f"<span>{html.escape(label)}</span>"
        "</button>"
        for label, prompt in starters
    )

    return f"""\
<div class="empty-state">
  <div class="terminal-glyph">$_</div>
  <h2>Stuck on a command?</h2>
  <p>{html.escape(_prompt_empty_state_summary(context or {}))}</p>
  <div class="starter-list">
    {starter_buttons}
  </div>
  <div class="tip">Tip &middot; Press <kbd>&#8984;L</kbd> anywhere in the terminal</div>
</div>"""


def _prompt_empty_state_summary(context: dict) -> str:
    hint = _prompt_context_hint(context)
    if hint:
        return f"Ask about the recent terminal context, including: {hint}"
    return "Ask about the last output, or paste a command for help."


def _prompt_starters(context: dict) -> list[tuple[str, str]]:
    hint = _prompt_context_hint(context)
    if hint:
        return [
            (
                "Explain the recent output",
                "Explain the recent terminal output and what I should do next.",
            ),
            (
                "Find likely failures",
                "Find likely errors or risky steps in the recent terminal context.",
            ),
            (
                "Suggest next command",
                "Suggest the safest next command based on the recent terminal context.",
            ),
        ]
    return [
        ("Explain the last output", "Explain the last output"),
        ("Suggest a fix", "Suggest a fix for this error"),
        ("Draft a safe command", "Draft a safe terminal command for what I describe next."),
    ]


def _prompt_context_hint(context: dict, limit: int = 76) -> str:
    command = str((context or {}).get("command") or "").strip()
    if command:
        return _truncate_text(command, limit)

    output = str((context or {}).get("terminal_output") or "")
    for line in reversed(output.splitlines()):
        compact = _compact_text(line)
        if compact:
            return _truncate_text(compact, limit)
    return ""


def _plain_text_to_html(text: str) -> str:
    """Render user-authored prompt text without interpreting Markdown."""
    return html.escape(text).replace("\n", "<br>")


def _markdown_to_html(markdown: str) -> str:
    """Render a small, safe Markdown subset to HTML."""
    lines = markdown.splitlines()
    blocks: list[str] = []
    paragraph: list[str] = []
    list_items: list[str] = []
    list_tag: Optional[str] = None
    in_code = False
    code_lines: list[str] = []

    def flush_paragraph() -> None:
        nonlocal paragraph
        if paragraph:
            blocks.append(f"<p>{_inline_markdown(' '.join(paragraph))}</p>")
            paragraph = []

    def flush_list() -> None:
        nonlocal list_items, list_tag
        if list_items:
            tag = list_tag or "ul"
            blocks.append(f"<{tag}>" + "".join(f"<li>{item}</li>" for item in list_items) + f"</{tag}>")
            list_items = []
            list_tag = None

    def append_list_item(tag: str, item: str) -> None:
        nonlocal list_tag
        if list_tag is not None and list_tag != tag:
            flush_list()
        list_tag = tag
        list_items.append(item)

    def code_block_html(lines: list[str]) -> str:
        code = html.escape("\n".join(lines))
        return (
            '<div class="code-block">'
            '<div class="code-actions">'
            '<button class="code-action copy-code" type="button" data-copy-code>Copy</button>'
            '<button class="code-action insert-code" type="button" data-insert-code>Insert</button>'
            "</div>"
            f"<pre><code>{code}</code></pre>"
            "</div>"
        )

    for line in lines:
        stripped = line.strip()

        if stripped.startswith("```"):
            if in_code:
                blocks.append(code_block_html(code_lines))
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
            append_list_item("ul", _inline_markdown(stripped[2:].strip()))
        elif re.match(r"^\d+[.)]\s+", stripped):
            flush_paragraph()
            append_list_item("ol", _inline_markdown(re.sub(r"^\d+[.)]\s+", "", stripped).strip()))
        else:
            flush_list()
            paragraph.append(stripped)

    if in_code:
        blocks.append(code_block_html(code_lines))
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
