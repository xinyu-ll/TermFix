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
from . import markdown as _markdown_rendering
from .context import collect_context, normalize_context_lines
from .llm_client import check_provider_connection, stream_analyze_error, stream_user_prompt
from .monitor import PromptEntry
from .safety import REDACTION_STATUS_TEXT, UnsafeInsertError, prepare_insert_text

logger = logging.getLogger(__name__)

_INFO_POPOVER_ID = "__info__"
_PROMPT_POPOVER_ID = "__prompt__"
_ERROR_INBOX_LIMIT = 10
_STATE_LOOP_CALL_TIMEOUT = 2
_STATUS_REGISTER_TIMEOUT = 10
_STATUS_HOTKEY_ERROR = "⚠ Hotkey off"
_STATUS_HOTKEY_CONFIG_ERROR = "⚠ Hotkey config"
_STATUS_SHELL_INTEGRATION_ERROR = "⚠ Shell integration"
_STATUS_SERVER_LOCK_GUARD = threading.Lock()
_POPOVER_CORS_ORIGIN = "null"
_POPOVER_ROUTES = frozenset(
    {
        "/cancel",
        "/closed",
        "/dismiss",
        "/insert",
        "/prompt/new",
        "/prompt",
        "/retry",
        "/state",
        "/test-connection",
    }
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
_TERMFIX_API_KEY_ENV_VAR = "TERMFIX_API_KEY"
_DEEPSEEK_API_KEY_ENV_VAR = "DEEPSEEK_API_KEY"
_OPENAI_API_KEY_ENV_VAR = "OPENAI_API_KEY"
_CODE_BLOCK_COPY_CSS = _markdown_rendering._CODE_BLOCK_COPY_CSS
_CODE_BLOCK_COPY_JS = _markdown_rendering._CODE_BLOCK_COPY_JS
_compact_text = _markdown_rendering._compact_text
_inline_bold_to_html = _markdown_rendering._inline_bold_to_html
_inline_markdown = _markdown_rendering._inline_markdown
_markdown_to_html = _markdown_rendering._markdown_to_html
_plain_text_to_html = _markdown_rendering._plain_text_to_html


def _prompt_popover_id(session_id: str) -> str:
    return f"{_PROMPT_POPOVER_ID}:{session_id}" if session_id else _PROMPT_POPOVER_ID


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
        return _status_badge_text(state)

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
    state.hotkey_listener_error = ""

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
        state.hotkey_listener_error = str(exc) or exc.__class__.__name__
        await state.notify_ui_update()
        await asyncio.Event().wait()


def _status_badge_text(state: "TermFixState") -> str:
    if getattr(state, "fix_hotkey_error", "") or getattr(state, "prompt_hotkey_error", ""):
        return _STATUS_HOTKEY_CONFIG_ERROR
    if getattr(state, "hotkey_listener_error", ""):
        return _STATUS_HOTKEY_ERROR
    if getattr(state, "shell_integration_missing", False):
        return _STATUS_SHELL_INTEGRATION_ERROR
    if getattr(state, "analyzing", False):
        return "⏳ Analyzing..."
    unhandled_count = getattr(state, "unhandled_error_count", 0)
    if unhandled_count == 0:
        return STATUS_NORMAL
    return STATUS_ERROR_FMT.format(count=unhandled_count)


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

    prompt_popover_id = _prompt_popover_id(session_id)
    if state.is_popover_open(prompt_popover_id):
        state.request_popover_close(prompt_popover_id)
        logger.info("TermFix prompt popover close requested — session=%s", session_id)
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
        state.mark_popover_seen(prompt_popover_id)
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
    for entry in _prompt_entries_snapshot(state):
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


def _recent_error_entries(
    state: "TermFixState",
    active_entry_id: str,
    active_entry=None,  # noqa: ANN001 - accepts ErrorEntry-like test doubles.
) -> list:
    recent_errors = getattr(state, "recent_errors", None)
    if callable(recent_errors):
        entries = list(recent_errors(active_entry_id, _ERROR_INBOX_LIMIT))
    else:
        entries = list(reversed(list(getattr(state, "errors", []) or [])[-_ERROR_INBOX_LIMIT:]))

    if active_entry is not None:
        active_id = str(getattr(active_entry, "id", ""))
        if active_id and active_id not in {str(getattr(entry, "id", "")) for entry in entries}:
            entries.insert(0, active_entry)
    return entries


def _error_inbox_payload(
    state: "TermFixState",
    active_entry_id: str,
    active_entry=None,  # noqa: ANN001 - accepts ErrorEntry-like test doubles.
) -> list[dict]:
    items: list[dict] = []
    for entry in _recent_error_entries(state, active_entry_id, active_entry):
        entry_id = str(getattr(entry, "id", ""))
        if not entry_id:
            continue
        items.append(
            {
                "id": entry_id,
                "command": str(getattr(entry, "command", "") or "(unknown command)"),
                "exit_code": getattr(entry, "exit_code", ""),
                "status": str(getattr(entry, "status", "") or "pending"),
                "handled": bool(getattr(entry, "handled", False)),
                "active": entry_id == active_entry_id,
                "updated_at": getattr(entry, "updated_at", 0),
            }
        )
    return items


def _error_inbox_to_html(
    state: "TermFixState",
    active_entry_id: str,
    active_entry=None,  # noqa: ANN001 - accepts ErrorEntry-like test doubles.
) -> str:
    items = _error_inbox_payload(state, active_entry_id, active_entry)
    if not items:
        return '<div class="error-empty">No recent errors</div>'

    blocks: list[str] = []
    for item in items:
        classes = ["error-item"]
        if item["active"]:
            classes.append("active")
        classes.append("handled" if item["handled"] else "unhandled")
        command = html.escape(str(item["command"]))
        status = html.escape(str(item["status"]))
        exit_code = html.escape(str(item["exit_code"]))
        handled = "handled" if item["handled"] else "unhandled"
        current = '<span class="error-pill current">current</span>' if item["active"] else ""
        aria_current = ' aria-current="true"' if item["active"] else ""
        blocks.append(
            f'<button class="{" ".join(classes)}" data-entry-id="{html.escape(item["id"])}" '
            f'title="{command}" type="button"{aria_current}>'
            f'<span class="error-command">{command}</span>'
            '<span class="error-meta">'
            f"<span>exit {exit_code}</span>"
            f"<span>{status}</span>"
            f'<span class="error-pill">{handled}</span>'
            f"{current}"
            "</span>"
            "</button>"
        )
    return "\n".join(blocks)


def _error_context_label(entry) -> str:  # noqa: ANN001 - accepts ErrorEntry-like test doubles.
    context = getattr(entry, "context", {}) or {}
    if not isinstance(context, dict):
        context = {}

    parts: list[str] = []
    line_count = context.get("terminal_output_line_count") or context.get("context_lines")
    if line_count is None and context.get("terminal_output") is not None:
        output = str(context.get("terminal_output") or "")
        line_count = len(output.splitlines()) if output else 0
    try:
        line_count_int = int(line_count)
    except (TypeError, ValueError):
        line_count_int = 0
    if line_count_int > 0:
        unit = "line" if line_count_int == 1 else "lines"
        parts.append(f"{line_count_int} {unit} context")

    shell = str(context.get("shell") or "").strip()
    if shell:
        parts.append(shell)

    os_name = str(context.get("os_name") or "").strip()
    os_version = str(context.get("os_version") or "").strip()
    os_label = " ".join(part for part in (os_name, os_version) if part)
    if os_label:
        parts.append(os_label)

    return " · ".join(parts)


def _maybe_start_error_analysis(entry, state: "TermFixState") -> None:  # noqa: ANN001
    if getattr(entry, "analysis_started", False):
        return
    if getattr(entry, "status", "pending") != "pending":
        return
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return
    if getattr(state, "loop", None) is None:
        state.loop = loop
    _start_analysis_task_on_loop(entry, state)


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
    with _status_server_init_lock(state):
        _ensure_status_server_locked(state)


def _status_server_init_lock(state: "TermFixState") -> threading.Lock:
    lock = getattr(state, "status_server_lock", None)
    if lock is not None:
        return lock
    with _STATUS_SERVER_LOCK_GUARD:
        lock = getattr(state, "status_server_lock", None)
        if lock is None:
            lock = threading.Lock()
            state.status_server_lock = lock
        return lock


def _ensure_status_server_locked(state: "TermFixState") -> None:
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

            if request_path == "/dismiss":
                if self.command != "POST":
                    self.send_error(405)
                    return
                entry_id = parse_qs(parsed.query).get("entry", [""])[0]
                self._send_json(_dismiss_error_from_thread(state, entry_id))
                return

            if request_path == "/retry":
                if self.command != "POST":
                    self.send_error(405)
                    return
                entry_id = parse_qs(parsed.query).get("entry", [""])[0]
                self._send_json(_retry_analysis_from_thread(state, entry_id))
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

            if request_path == "/test-connection":
                if self.command != "POST":
                    self.send_error(405)
                    return
                self._send_json(_test_connection_from_thread(state))
                return

            if request_path != "/state":
                self.send_error(404)
                return

            entry_id = parse_qs(parsed.query).get("entry", [""])[0]
            session_id = parse_qs(parsed.query).get("session", [""])[0]
            start_analysis = parse_qs(parsed.query).get("start", [""])[0] == "1"
            self._send_json(
                _entry_payload_from_thread(
                    state,
                    entry_id,
                    session_id,
                    start_analysis=start_analysis,
                )
            )

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
    handled_error = False
    if entry_id:
        prompt_entry = state.get_prompt(entry_id)
        if prompt_entry is not None:
            state.mark_popover_closed(_prompt_popover_id(prompt_entry.session_id))
        else:
            handled_error = state.mark_error_handled(entry_id)
        state.mark_popover_closed(entry_id)
    if handled_error:
        await state.notify_ui_update()
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


def _retry_analysis_from_thread(state: "TermFixState", entry_id: str) -> dict:
    """Retry a failed error analysis on the asyncio loop."""
    if not entry_id:
        return {"ok": False, "error": "Missing entry."}
    return _call_state_loop_from_thread(
        state,
        lambda: _retry_analysis(entry_id, state),
        "Analysis retry",
    )


def _dismiss_error_from_thread(state: "TermFixState", entry_id: str) -> dict:
    """Mark an error handled without waiting for analysis to finish."""
    if not entry_id:
        return {"ok": False, "error": "Missing entry."}
    return _call_state_loop_from_thread(
        state,
        lambda: _dismiss_error(entry_id, state),
        "Error dismiss",
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


async def _retry_analysis(entry_id: str, state: "TermFixState") -> dict:
    entry = state.get_error(entry_id)
    if entry is None:
        return {"ok": False, "error": "Entry expired."}
    if entry.status == "streaming":
        return {"ok": False, "error": "Analysis is already running."}

    task = state.analysis_tasks.pop(entry_id, None)
    if task is not None and not task.done():
        task.cancel()

    entry.result = None
    entry.analyzed = False
    entry.analysis_started = False
    entry.status = "pending"
    entry.updated_at = time.time()
    _start_analysis_task_on_loop(entry, state)
    await state.notify_ui_update()
    return {"ok": True, "status": entry.status}


async def _dismiss_error(entry_id: str, state: "TermFixState") -> dict:
    entry = state.get_error(entry_id)
    if entry is None:
        return {"ok": False, "error": "Entry expired."}
    handled = state.mark_error_handled(entry_id)
    state.request_popover_close(entry_id)
    if handled:
        await state.notify_ui_update()
    return {"ok": True, "handled": handled}


def _entry_payload_from_thread(
    state: "TermFixState",
    entry_id: str,
    popover_session_id: str = "",
    start_analysis: bool = False,
) -> dict:
    """Build a state payload on the asyncio loop."""
    return _call_state_loop_from_thread(
        state,
        lambda: _entry_payload_on_loop(
            entry_id,
            state,
            popover_session_id,
            start_analysis=start_analysis,
        ),
        "State payload",
    )


async def _entry_payload_on_loop(
    entry_id: str,
    state: "TermFixState",
    popover_session_id: str = "",
    start_analysis: bool = False,
) -> dict:
    payload, handled_error = _entry_payload_with_handled_state(
        state,
        entry_id,
        popover_session_id,
        start_analysis=start_analysis,
    )
    if handled_error:
        await state.notify_ui_update()
    return payload


def _entry_payload(
    state: "TermFixState",
    entry_id: str,
    popover_session_id: str = "",
    start_analysis: bool = False,
) -> dict:
    payload, handled_error = _entry_payload_with_handled_state(
        state,
        entry_id,
        popover_session_id,
        start_analysis=start_analysis,
    )
    if handled_error:
        _notify_ui_update_from_thread(state)
    return payload


def _entry_payload_with_handled_state(
    state: "TermFixState",
    entry_id: str,
    popover_session_id: str = "",
    start_analysis: bool = False,
) -> tuple[dict, bool]:
    """Return a JSON-safe snapshot of the current analysis state."""
    if entry_id == _INFO_POPOVER_ID:
        state.mark_popover_seen(entry_id)
        return {
            "ok": True,
            "status": "info",
            "done": False,
            "should_close": state.consume_popover_close_request(entry_id),
        }, False

    prompt_entry = state.get_prompt(entry_id)
    if prompt_entry is not None:
        prompt_popover_id = _prompt_popover_id(popover_session_id or prompt_entry.session_id)
        state.mark_popover_seen(prompt_popover_id)
        state.mark_popover_seen(entry_id)
        done = prompt_entry.status in ("done", "error", "cancelled")
        return {
            "ok": True,
            "entry_id": prompt_entry.id,
            "session_id": prompt_entry.session_id,
            "status": prompt_entry.status,
            "done": done,
            "should_close": (
                state.consume_popover_close_request(prompt_popover_id)
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
        }, False

    entry = state.get_error(entry_id)
    if entry is None:
        return {
            "ok": False,
            "status": "missing",
            "done": True,
            "can_retry": False,
            "should_close": False,
            "errors": _error_inbox_payload(state, entry_id),
            "body_html": "<p>This TermFix result is no longer available.</p>",
        }, False

    if start_analysis:
        _maybe_start_error_analysis(entry, state)
    handled_error = state.mark_error_handled(entry_id)
    state.mark_popover_seen(entry_id)
    markdown = entry.result or "Analyzing..."
    done = entry.status in ("done", "error", "cancelled")
    return {
        "ok": True,
        "entry_id": entry.id,
        "session_id": entry.session_id,
        "command": entry.command or "(unknown command)",
        "exit_code": entry.exit_code,
        "context_label": _error_context_label(entry),
        "status": entry.status,
        "done": done,
        "can_retry": entry.status == "error",
        "should_close": state.consume_popover_close_request(entry_id),
        "updated_at": entry.updated_at,
        "errors": _error_inbox_payload(state, entry.id, entry),
        "body_html": _markdown_to_html(markdown),
    }, handled_error


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


def _test_connection_from_thread(state: "TermFixState") -> dict:
    """Run a provider connection test from the HTTP handler thread."""
    api_key = getattr(state, "api_key", "")
    if not api_key:
        return {
            "ok": False,
            "kind": "missing_api_key",
            "error": "API key is not configured. Set the API Key knob before testing.",
        }

    return check_provider_connection(
        api_key=api_key,
        base_url=getattr(state, "base_url", DEFAULT_BASE_URL),
        model=getattr(state, "model", DEFAULT_MODEL),
    )


async def _insert_code_text(
    text: str,
    state: "TermFixState",
    popover_session_id: str = "",
) -> dict:
    try:
        text = prepare_insert_text(text)
    except UnsafeInsertError as exc:
        return {"ok": False, "error": str(exc)}

    if not text:
        return {"ok": False, "error": "Code block is empty after removing final newlines."}

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
    state.mark_popover_seen(_prompt_popover_id(session_id))
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
    context_label = html.escape(_error_context_label(entry))
    context_hidden = "" if context_label else " hidden"
    body = _markdown_to_html(entry.result or "Analyzing...")
    inbox = _error_inbox_to_html(state, entry.id, entry)
    redaction_note = html.escape(
        f"{REDACTION_STATUS_TEXT}: command/output redacted before sending."
    )
    endpoint = json.dumps(_status_endpoint(state, "/state"))
    dismiss_endpoint = json.dumps(_status_endpoint(state, "/dismiss"))
    retry_endpoint = json.dumps(_status_endpoint(state, "/retry"))
    close_endpoint = json.dumps(_status_endpoint(state, "/closed"))
    close_key = json.dumps(
        _hotkey_letter(getattr(state, "fix_hotkey", DEFAULT_FIX_HOTKEY)).lower()
    )
    active_entry_id = json.dumps(entry.id)
    active_session_id = json.dumps(getattr(entry, "session_id", ""))
    insert_endpoint_base = json.dumps(_status_endpoint(state, "/insert"))
    insert_endpoint = json.dumps(
        _status_endpoint(state, "/insert", {"session": getattr(entry, "session_id", "")})
    )
    retry_hidden = "" if getattr(entry, "status", "") == "error" else " hidden"

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
    html, body {{
      height: 100%;
    }}
    body {{
      height: 100%;
      display: flex;
      flex-direction: column;
      font-family: var(--sans);
      font-size: 13px;
      color: var(--ink);
      background: var(--paper);
      padding: 14px 16px;
      line-height: 1.45;
      overflow: hidden;
    }}
    header {{
      flex: 0 0 auto;
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
      min-width: 0;
      max-width: 210px;
      overflow: hidden;
      text-overflow: ellipsis;
      white-space: nowrap;
      font-family: var(--mono);
      font-size: 11px;
      color: var(--muted);
      background: var(--field);
      border-radius: 4px;
      padding: 1px 6px;
    }}
    .context-label {{
      min-width: 0;
      max-width: 180px;
      overflow: hidden;
      color: var(--muted);
      font-family: var(--mono);
      font-size: 11px;
      font-weight: 650;
      text-overflow: ellipsis;
      white-space: nowrap;
    }}
    .context-label[hidden] {{
      display: none;
    }}
    .status {{
      flex: 0 0 auto;
      font-size: 11px;
      color: var(--muted);
    }}
    .dismiss-button {{
      margin-left: auto;
      min-height: 26px;
      border: 1px solid var(--line);
      border-radius: 6px;
      background: transparent;
      color: var(--muted);
      font: 700 12px var(--sans);
      padding: 3px 9px;
      cursor: pointer;
    }}
    .dismiss-button:hover {{
      color: var(--ink);
      background: var(--field);
    }}
    .security-note {{
      flex: 0 0 auto;
      margin: -4px 0 10px;
      color: var(--muted);
      font-family: var(--mono);
      font-size: 11px;
      font-weight: 650;
    }}
    .error-inbox {{
      flex: 0 0 auto;
      margin: -2px 0 12px;
      padding-bottom: 10px;
      border-bottom: 1px solid var(--line);
    }}
    .inbox-label {{
      margin-bottom: 7px;
      color: var(--muted);
      font-size: 10px;
      font-weight: 800;
      letter-spacing: 0.06em;
      text-transform: uppercase;
    }}
    .error-list {{
      display: flex;
      gap: 7px;
      overflow-x: auto;
      padding-bottom: 2px;
    }}
    .error-item {{
      width: 156px;
      flex: 0 0 156px;
      min-height: 58px;
      padding: 8px 9px;
      border: 1px solid transparent;
      border-radius: 8px;
      background: var(--field);
      color: var(--ink);
      text-align: left;
      cursor: pointer;
    }}
    .error-item:hover {{
      border-color: var(--line);
    }}
    .error-item.active {{
      border-color: var(--accent);
      background: var(--panel);
    }}
    .error-command {{
      display: block;
      overflow: hidden;
      color: var(--ink);
      font-family: var(--mono);
      font-size: 11px;
      font-weight: 700;
      line-height: 1.25;
      text-overflow: ellipsis;
      white-space: nowrap;
    }}
    .error-meta {{
      display: flex;
      flex-wrap: wrap;
      gap: 4px 7px;
      margin-top: 6px;
      color: var(--muted);
      font-family: var(--mono);
      font-size: 10px;
      font-weight: 700;
    }}
    .error-pill.current {{
      color: var(--accent);
    }}
    .error-empty {{
      color: var(--muted);
      font-size: 12px;
    }}
    #content {{
      flex: 1;
      min-height: 0;
      overflow-y: auto;
      padding-right: 2px;
    }}
    .actions {{
      flex: 0 0 auto;
      display: flex;
      justify-content: flex-end;
      gap: 8px;
      margin-top: 10px;
      padding-top: 10px;
      border-top: 1px solid var(--line);
    }}
    .actions[hidden],
    button[hidden] {{
      display: none;
    }}
    #retry-button {{
      min-height: 28px;
      border: 1px solid var(--line);
      border-radius: 6px;
      background: var(--field);
      color: var(--ink);
      font: 700 12px var(--sans);
      padding: 4px 10px;
      cursor: pointer;
    }}
    #retry-button:disabled {{
      opacity: 0.55;
      cursor: default;
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
    <span id="failed-cmd" class="failed-cmd">{failed_cmd}</span>
    <span id="exit-code" class="badge">exit {exit_code}</span>
    <span id="context-label" class="context-label"{context_hidden}>{context_label}</span>
    <button id="dismiss-button" class="dismiss-button" type="button">Ignore</button>
    <span id="status" class="status {html.escape(entry.status)}">{html.escape(entry.status)}</span>
  </header>

  <div class="security-note">{redaction_note}</div>
  <nav class="error-inbox" aria-label="Recent failed commands">
    <div class="inbox-label">Inbox</div>
    <div id="error-list" class="error-list">{inbox}</div>
  </nav>

  <div id="content" class="markdown">{body}</div>
  <div id="actions" class="actions">
    <button id="retry-button" type="button"{retry_hidden}>Retry analysis</button>
  </div>
  <script>
    const endpoint = {endpoint};
    const dismissEndpoint = {dismiss_endpoint};
    const retryEndpoint = {retry_endpoint};
    const closeEndpoint = {close_endpoint};
    const initialEntryId = {active_entry_id};
    const insertEndpointBase = {insert_endpoint_base};
    let activeEntryId = {active_entry_id};
    let activeSessionId = {active_session_id};
    let insertEndpoint = {insert_endpoint};
    const commandEl = document.getElementById("failed-cmd");
    const exitEl = document.getElementById("exit-code");
    const contextLabelEl = document.getElementById("context-label");
    const errorListEl = document.getElementById("error-list");
    const contentEl = document.getElementById("content");
    const statusEl = document.getElementById("status");
    const dismissButton = document.getElementById("dismiss-button");
    const retryButton = document.getElementById("retry-button");
    let lastHtml = contentEl.innerHTML;
    let timer = null;
    let pollDelay = 350;
    let closing = false;

    function withEntry(url, entryId) {{
      const sep = url.includes("?") ? "&" : "?";
      return url + sep + "entry=" + encodeURIComponent(entryId);
    }}

    function withSession(url, sessionId) {{
      if (!sessionId) {{
        return url;
      }}
      const sep = url.includes("?") ? "&" : "?";
      return url + sep + "session=" + encodeURIComponent(sessionId);
    }}

    function escapeHtml(text) {{
      return String(text)
        .replace(/&/g, "&amp;")
        .replace(/</g, "&lt;")
        .replace(/>/g, "&gt;")
        .replace(/"/g, "&quot;")
        .replace(/'/g, "&#39;");
    }}

    function sendClosed(entryId) {{
      if (!entryId) {{
        return;
      }}
      const url = withEntry(closeEndpoint, entryId);
      try {{
        if (navigator.sendBeacon) {{
          navigator.sendBeacon(url);
        }} else {{
          fetch(url, {{ method: "POST", keepalive: true }});
        }}
      }} catch (error) {{
        // Best effort only.
      }}
    }}

    function reportClosed() {{
      sendClosed(activeEntryId);
      if (initialEntryId !== activeEntryId) {{
        sendClosed(initialEntryId);
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

    function renderErrorInbox(errors) {{
      if (!Array.isArray(errors)) {{
        return;
      }}
      errorListEl.textContent = "";
      if (errors.length === 0) {{
        const empty = document.createElement("div");
        empty.className = "error-empty";
        empty.textContent = "No recent errors";
        errorListEl.appendChild(empty);
        return;
      }}
      for (const item of errors) {{
        if (!item || !item.id) {{
          continue;
        }}
        const button = document.createElement("button");
        button.type = "button";
        button.className = "error-item " + (item.active ? "active " : "") +
          (item.handled ? "handled" : "unhandled");
        button.dataset.entryId = item.id;
        if (item.active) {{
          button.setAttribute("aria-current", "true");
        }}
        button.title = item.command || "";

        const command = document.createElement("span");
        command.className = "error-command";
        command.textContent = item.command || "(unknown command)";
        button.appendChild(command);

        const meta = document.createElement("span");
        meta.className = "error-meta";
        const exitCode = document.createElement("span");
        const exitValue = item.exit_code === undefined || item.exit_code === null ? "" : item.exit_code;
        exitCode.textContent = "exit " + exitValue;
        const status = document.createElement("span");
        status.textContent = item.status || "pending";
        const handled = document.createElement("span");
        handled.className = "error-pill";
        handled.textContent = item.handled ? "handled" : "unhandled";
        meta.appendChild(exitCode);
        meta.appendChild(status);
        meta.appendChild(handled);
        if (item.active) {{
          const current = document.createElement("span");
          current.className = "error-pill current";
          current.textContent = "current";
          meta.appendChild(current);
        }}
        button.appendChild(meta);
        errorListEl.appendChild(button);
      }}
    }}

    function setStatus(status) {{
      if (!status) {{
        return;
      }}
      statusEl.textContent = status;
      statusEl.className = "status " + status;
    }}

    function setRetryVisible(canRetry) {{
      retryButton.hidden = !canRetry;
      retryButton.disabled = false;
    }}

    function setActiveEntry(entryId) {{
      if (!entryId || entryId === activeEntryId) {{
        return;
      }}
      activeEntryId = entryId;
      lastHtml = "";
      refresh();
    }}

    async function refresh() {{
      try {{
        const response = await fetch(
          withEntry(endpoint, activeEntryId) + "&start=1&t=" + Date.now(),
          {{
            cache: "no-store"
          }}
        );
        const data = await response.json();
        if (data.should_close) {{
          closePopover();
          return;
        }}
        if (data.entry_id) {{
          activeEntryId = data.entry_id;
        }}
        if (typeof data.session_id === "string") {{
          activeSessionId = data.session_id;
          insertEndpoint = withSession(insertEndpointBase, activeSessionId);
        }}
        if (typeof data.command === "string") {{
          commandEl.textContent = data.command;
          commandEl.title = data.command;
        }}
        if (Object.prototype.hasOwnProperty.call(data, "exit_code")) {{
          exitEl.textContent = "exit " + data.exit_code;
        }}
        if (Object.prototype.hasOwnProperty.call(data, "context_label")) {{
          contextLabelEl.textContent = data.context_label || "";
          contextLabelEl.hidden = !data.context_label;
        }}
        renderErrorInbox(data.errors);
        if (data.body_html && data.body_html !== lastHtml) {{
          lastHtml = data.body_html;
          contentEl.innerHTML = data.body_html;
        }}
        setStatus(data.status);
        setRetryVisible(Boolean(data.can_retry));
        const nextDelay = data.done ? 2000 : 350;
        if (timer && nextDelay !== pollDelay) {{
          clearInterval(timer);
          pollDelay = nextDelay;
          timer = setInterval(refresh, pollDelay);
        }}
      }} catch (error) {{
        setStatus("offline");
      }}
    }}

    async function retryAnalysis() {{
      if (!activeEntryId || retryButton.disabled) {{
        return;
      }}
      retryButton.disabled = true;
      setRetryVisible(false);
      setStatus("pending");
      lastHtml = "";
      contentEl.innerHTML = "<p>Analyzing...</p>";
      try {{
        const response = await fetch(withEntry(retryEndpoint, activeEntryId), {{
          method: "POST",
          cache: "no-store"
        }});
        const data = await response.json();
        if (!data.ok) {{
          throw new Error(data.error || "Retry failed.");
        }}
        if (timer) {{
          clearInterval(timer);
        }}
        pollDelay = 350;
        timer = setInterval(refresh, pollDelay);
        refresh();
      }} catch (error) {{
        contentEl.innerHTML = "<p>" + escapeHtml(error.message || error) + "</p>";
        setStatus("error");
        setRetryVisible(true);
      }}
    }}

    async function dismissError() {{
      if (!activeEntryId || dismissButton.disabled) {{
        return;
      }}
      dismissButton.disabled = true;
      try {{
        const response = await fetch(withEntry(dismissEndpoint, activeEntryId), {{
          method: "POST",
          cache: "no-store"
        }});
        const data = await response.json();
        if (!data.ok) {{
          throw new Error(data.error || "Dismiss failed.");
        }}
        closePopover();
      }} catch (error) {{
        dismissButton.disabled = false;
        setStatus("error");
        contentEl.innerHTML = "<p>" + escapeHtml(error.message || error) + "</p>";
      }}
    }}

    contentEl.addEventListener("click", handleCodeBlockCopy);
    dismissButton.addEventListener("click", dismissError);
    retryButton.addEventListener("click", retryAnalysis);

    errorListEl.addEventListener("click", (event) => {{
      const item = event.target.closest("[data-entry-id]");
      if (!item) {{
        return;
      }}
      setActiveEntry(item.getAttribute("data-entry-id"));
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
      margin: 0 0 10px;
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
    .history-search {{
      width: 100%;
      height: 32px;
      flex: 0 0 32px;
      margin: 0 0 12px;
      padding: 0 10px;
      border: 1px solid var(--line);
      border-radius: 7px;
      outline: none;
      background: var(--panel-strong);
      color: var(--ink);
      font: 12px var(--sans);
    }}
    .history-search:focus {{
      border-color: var(--line-strong);
      box-shadow: 0 0 0 3px rgba(0, 122, 255, 0.14);
    }}
    .history-search::placeholder {{
      color: var(--muted);
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
    .history-group[hidden],
    .history-item[hidden] {{
      display: none;
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
      .history-search {{
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
        <input id="history-search" class="history-search" type="search" placeholder="Search history..." autocomplete="off" spellcheck="false">
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
    const historySearchEl = document.getElementById("history-search");
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

    function applyHistoryFilter() {{
      const query = historySearchEl.value.trim().toLowerCase();
      const children = Array.from(historyListEl.children);
      let currentGroup = null;
      let groupHasVisibleItems = false;

      function finishGroup() {{
        if (currentGroup) {{
          currentGroup.hidden = Boolean(query) && !groupHasVisibleItems;
        }}
      }}

      for (const child of children) {{
        if (child.classList.contains("history-group")) {{
          finishGroup();
          currentGroup = child;
          groupHasVisibleItems = false;
          child.hidden = false;
          continue;
        }}
        if (!child.classList.contains("history-item")) {{
          continue;
        }}
        const haystack = (child.dataset.search || child.textContent || "").toLowerCase();
        const visible = !query || haystack.includes(query);
        child.hidden = !visible;
        if (visible) {{
          groupHasVisibleItems = true;
        }}
      }}
      finishGroup();
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
          applyHistoryFilter();
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

    historySearchEl.addEventListener("input", applyHistoryFilter);

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
    redaction_status = html.escape(REDACTION_STATUS_TEXT)
    close_key = json.dumps(
        _hotkey_letter(getattr(state, "fix_hotkey", DEFAULT_FIX_HOTKEY)).lower()
    )
    endpoint = json.dumps(_status_endpoint(state, "/state", {"entry": _INFO_POPOVER_ID}))
    close_endpoint = json.dumps(
        _status_endpoint(state, "/closed", {"entry": _INFO_POPOVER_ID})
    )
    test_endpoint = json.dumps(_status_endpoint(state, "/test-connection"))
    has_api_key = json.dumps(bool(state.api_key))
    missing_api_key_message = json.dumps(
        api_key_error or "API key is missing. Configure the API Key knob before testing."
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
    .actions {{
      margin: 12px 0 8px 0;
      display: flex;
      align-items: center;
      gap: 8px;
    }}
    button {{
      appearance: none;
      border: 1px solid #d1d1d6;
      border-radius: 6px;
      background: #f2f2f7;
      color: #1c1c1e;
      font: inherit;
      font-weight: 600;
      padding: 5px 9px;
      cursor: pointer;
    }}
    button:disabled {{
      cursor: default;
      opacity: 0.65;
    }}
    .connection-status {{
      min-height: 18px;
      margin: 0 0 6px 0;
      font-weight: 600;
    }}
    .connection-status.testing {{ color: #6e6e73; }}
    .connection-status.success {{ color: #167c46; }}
    .connection-status.failure {{ color: #b42318; }}
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
  <div class="item"><span class="label">Context:</span> <code>{redaction_status}</code></div>
  <div class="item"><span class="label">API Key:</span> <code>{api_key_status}</code></div>
  {validation_html}
  <div class="actions">
    <button id="test-connection" type="button">Test connection</button>
  </div>
  <div id="connection-status" class="connection-status" role="status" aria-live="polite"></div>
  <script>
    const endpoint = {endpoint};
    const closeEndpoint = {close_endpoint};
    const testEndpoint = {test_endpoint};
    const hasApiKey = {has_api_key};
    const missingApiKeyMessage = {missing_api_key_message};
    const testButton = document.getElementById("test-connection");
    const connectionStatusEl = document.getElementById("connection-status");
    let timer = null;
    let closing = false;

    function setConnectionStatus(kind, message) {{
      connectionStatusEl.className = kind ? "connection-status " + kind : "connection-status";
      connectionStatusEl.textContent = message || "";
    }}

    async function testConnection() {{
      if (!hasApiKey) {{
        setConnectionStatus("failure", missingApiKeyMessage);
        return;
      }}

      testButton.disabled = true;
      setConnectionStatus("testing", "Testing...");
      try {{
        const response = await fetch(testEndpoint, {{
          method: "POST",
          headers: {{ "Content-Type": "application/json" }},
          body: "{{}}",
          cache: "no-store"
        }});
        const data = await response.json();
        if (!data.ok) {{
          throw new Error(data.error || "Connection test failed.");
        }}
        setConnectionStatus("success", data.message || "Connection succeeded.");
      }} catch (error) {{
        setConnectionStatus("failure", error.message || "Connection test failed.");
      }} finally {{
        testButton.disabled = false;
      }}
    }}

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
    testButton.addEventListener("click", testConnection);

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

    if _knob_has(knobs, "base_url"):
        base_url, base_url_error = normalize_base_url(
            _knob_get(knobs, "base_url", ""),
            getattr(state, "base_url", DEFAULT_BASE_URL),
        )
        state.base_url = base_url
        state.base_url_error = base_url_error
        if base_url_error:
            logger.warning("Ignoring Base URL setting: %s", base_url_error)

    if _knob_has(knobs, "api_key"):
        previous_api_key_error = getattr(state, "api_key_error", "")
        api_key, api_key_error = _normalize_api_key(_knob_get(knobs, "api_key", ""))
        if not api_key and not api_key_error:
            api_key, api_key_error = _api_key_from_env(
                getattr(state, "base_url", DEFAULT_BASE_URL)
            )
        state.api_key = api_key
        state.api_key_error = api_key_error
        if api_key_error and api_key_error != previous_api_key_error:
            logger.warning("Ignoring API key setting: %s", api_key_error)

    model = str(_knob_get(knobs, "model", "") or "").strip()
    if model:
        state.model = model

    ctx_raw = str(_knob_get(knobs, "context_lines", "") or "").strip()
    if ctx_raw.isdigit():
        state.context_lines = normalize_context_lines(ctx_raw)

    if _knob_has(knobs, "max_tokens"):
        max_tokens, max_tokens_error = normalize_max_tokens(
            _knob_get(knobs, "max_tokens", ""),
            getattr(state, "max_tokens", DEFAULT_MAX_TOKENS),
        )
        state.max_tokens = max_tokens
        state.max_tokens_error = max_tokens_error
        if max_tokens_error:
            logger.warning("Ignoring Max Tokens setting: %s", max_tokens_error)

    if _knob_has(knobs, "fix_hotkey"):
        fix_hotkey, fix_hotkey_error = normalize_command_hotkey(
            _knob_get(knobs, "fix_hotkey", ""),
            getattr(state, "fix_hotkey", DEFAULT_FIX_HOTKEY),
            DEFAULT_FIX_HOTKEY,
        )
        state.fix_hotkey = fix_hotkey
        state.fix_hotkey_error = fix_hotkey_error
        if fix_hotkey_error:
            logger.warning("Ignoring Fix Hotkey setting: %s", fix_hotkey_error)

    if _knob_has(knobs, "prompt_hotkey"):
        prompt_hotkey, prompt_hotkey_error = normalize_command_hotkey(
            _knob_get(knobs, "prompt_hotkey", ""),
            getattr(state, "prompt_hotkey", DEFAULT_PROMPT_HOTKEY),
            DEFAULT_PROMPT_HOTKEY,
        )
        state.prompt_hotkey = prompt_hotkey
        state.prompt_hotkey_error = prompt_hotkey_error
        if prompt_hotkey_error:
            logger.warning("Ignoring Prompt Hotkey setting: %s", prompt_hotkey_error)

    _validate_hotkey_conflict(state)


def _knob_has(knobs: dict, canonical_key: str) -> bool:
    return _knob_lookup_key(knobs, canonical_key) is not None


def _knob_get(knobs: dict, canonical_key: str, default=""):  # noqa: ANN001
    key = _knob_lookup_key(knobs, canonical_key)
    if key is None:
        return default
    return knobs.get(key, default)


def _knob_lookup_key(knobs: dict, canonical_key: str) -> Optional[str]:
    if canonical_key in knobs:
        return canonical_key
    for raw_key in knobs:
        if _canonical_knob_key(raw_key) == canonical_key:
            return raw_key
    return None


def _canonical_knob_key(value) -> str:  # noqa: ANN001
    text = re.sub(r"\s*\([^)]*\)\s*$", "", str(value or "")).strip().lower()
    return re.sub(r"[^a-z0-9]+", "_", text).strip("_")


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


def _is_hotkey_conflict_error(error: str) -> bool:
    return "conflicts with" in str(error or "")


def _validate_hotkey_conflict(state: "TermFixState") -> None:
    if _is_hotkey_conflict_error(getattr(state, "fix_hotkey_error", "")):
        state.fix_hotkey_error = ""
    if _is_hotkey_conflict_error(getattr(state, "prompt_hotkey_error", "")):
        state.prompt_hotkey_error = ""

    if getattr(state, "fix_hotkey_error", "") or getattr(state, "prompt_hotkey_error", ""):
        return

    fix_hotkey = getattr(state, "fix_hotkey", DEFAULT_FIX_HOTKEY)
    prompt_hotkey = getattr(state, "prompt_hotkey", DEFAULT_PROMPT_HOTKEY)
    if _hotkey_letter(fix_hotkey) != _hotkey_letter(prompt_hotkey):
        return

    state.fix_hotkey_error = f"Fix Hotkey conflicts with Prompt Hotkey ({prompt_hotkey})."
    state.prompt_hotkey_error = f"Prompt Hotkey conflicts with Fix Hotkey ({fix_hotkey})."


def _api_key_env_vars_for_base_url(base_url: str) -> tuple[str, ...]:
    """Return safe environment key fallbacks for the configured provider."""
    hostname = (urlparse(str(base_url or DEFAULT_BASE_URL)).hostname or "").lower()
    provider_vars: tuple[str, ...] = ()
    if hostname == "api.deepseek.com":
        provider_vars = (_DEEPSEEK_API_KEY_ENV_VAR,)
    elif hostname == "api.openai.com":
        provider_vars = (_OPENAI_API_KEY_ENV_VAR,)
    return (_TERMFIX_API_KEY_ENV_VAR, *provider_vars)


def _api_key_from_env(base_url: str = DEFAULT_BASE_URL) -> tuple[str, str]:
    """Return the first configured API key environment variable safe for base_url."""
    for name in _api_key_env_vars_for_base_url(base_url):
        api_key, api_key_error = _normalize_api_key(os.environ.get(name, ""))
        if api_key or api_key_error:
            return api_key, api_key_error
    return "", ""


def _prompt_history_to_html(
    state: "TermFixState",
    session_id: str,
    active_entry_id: str,
) -> str:
    """Render current-session history plus detached restored conversations."""
    entries = [
        entry
        for entry in _prompt_entries_snapshot(state)
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
        raw_preview = _prompt_history_preview(entry)
        preview = html.escape(raw_preview)
        search_text = html.escape(f"{_prompt_history_title(entry, limit=None)} {raw_preview}")
        timestamp = html.escape(time.strftime("%H:%M", time.localtime(entry.timestamp)))
        badge = '<span class="history-badge">Restored</span>' if restored else ""
        blocks.append(
            f'<button class="history-item{active}{status}{restored}" '
            f'data-entry-id="{html.escape(entry.id)}" data-search="{search_text}" '
            f'title="{full_title}" type="button">'
            '<span class="history-main">'
            f'<span class="history-title">{title}</span>'
            f'<span class="history-time">{timestamp}</span>'
            f"{badge}"
            "</span>"
            f'<span class="history-preview">{preview}</span>'
            "</button>"
        )
    return "\n".join(blocks)


def _prompt_entries_snapshot(state: "TermFixState") -> list[PromptEntry]:
    prompt_entries = getattr(state, "prompt_entries", None)
    if callable(prompt_entries):
        return list(prompt_entries())
    return list(getattr(state, "prompts", []) or [])


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
    redaction_label = REDACTION_STATUS_TEXT.lower()
    if _is_detached_prompt(entry):
        cwd = _prompt_cwd_label(entry.context)
        origin = f" from {cwd}" if cwd else ""
        return (
            f"Restored history{origin} - next reply uses current session context, "
            f"{redaction_label}"
        )

    if not entry.session_id:
        return f"Current session context will attach when you send, {redaction_label}"

    if popover_session_id and entry.session_id != popover_session_id:
        cwd = _prompt_cwd_label(entry.context)
        origin = f" from {cwd}" if cwd else ""
        return f"Different session context{origin}, {redaction_label}"

    context_lines = _prompt_context_lines(entry.context, state)
    return f"Current session context - last {context_lines} lines of output, {redaction_label}"


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
