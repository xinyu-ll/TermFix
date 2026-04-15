"""
Shell-integration monitor for TermFix.

Architecture
────────────
One *global* PromptMonitor task reads every shell-integration notification
and routes it to a per-session asyncio.Queue.

A separate *EachSessionOnceMonitor* task runs concurrently; whenever iTerm2
creates a new session it calls asyncio.create_task() to spin up a lightweight
per-session worker that drains that session's queue.

This satisfies the concurrency requirement (independent task per session) while
avoiding the problem of multiple coroutines all blocking on the same monitor.

PromptMonitor notification shape
─────────────────────────────────
The iTerm2 Python API delivers notifications either as a plain object with a
.mode attribute, or as a (mode, info) tuple depending on version.  We detect
both forms at runtime so the code is forward-compatible.

Shell-integration modes used:
  COMMAND_START  — user just pressed Enter; command string is available
  COMMAND_END    — command exited; exit_status is available

If shell integration is NOT installed in a given session, no COMMAND_START /
COMMAND_END notifications are emitted for that session and monitoring is a
no-op (no errors are raised, the queue simply stays empty).
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from typing import Any, Dict, Optional

import iterm2

from config import DEFAULT_CONTEXT_LINES
from context import collect_context

logger = logging.getLogger(__name__)


# ── Shared data structures ─────────────────────────────────────────────────

@dataclass
class ErrorEntry:
    """A single captured command failure, enriched with LLM analysis lazily."""
    session_id: str
    command: str
    exit_code: int
    context: dict
    timestamp: float = field(default_factory=time.time)
    # Populated by the UI layer when the user clicks:
    result: Optional[dict] = None
    analyzed: bool = False


class TermFixState:
    """Thread-safe shared state between the monitor and the UI layer."""

    def __init__(self) -> None:
        self._lock = asyncio.Lock()
        self.errors: list[ErrorEntry] = []

        # Knob values written by the StatusBar component callback:
        self.api_key: str = ""
        self.model: str = "claude-opus-4-6"
        self.context_lines: int = DEFAULT_CONTEXT_LINES

        # Set after component registration so the monitor can trigger refreshes:
        self.component: Optional[iterm2.StatusBarComponent] = None
        self.connection: Optional[iterm2.Connection] = None

    # ── Mutation helpers ───────────────────────────────────────────────────

    async def add_error(self, entry: ErrorEntry) -> None:
        async with self._lock:
            self.errors.append(entry)

    async def clear_errors(self) -> None:
        async with self._lock:
            self.errors.clear()

    # ── Read helpers (no lock needed for simple reads) ─────────────────────

    @property
    def error_count(self) -> int:
        return len(self.errors)

    def latest_error(self) -> Optional[ErrorEntry]:
        return self.errors[-1] if self.errors else None

    # ── UI refresh ─────────────────────────────────────────────────────────

    async def notify_ui_update(self) -> None:
        """Ask the StatusBar component to re-render and update the badge."""
        if self.component is None or self.connection is None:
            return
        try:
            await self.component.async_invalidate(self.connection)
        except Exception as exc:
            logger.debug("async_invalidate failed: %s", exc)
        try:
            await self.component.async_set_unread_count(
                self.connection, self.error_count
            )
        except Exception as exc:
            logger.debug("async_set_unread_count failed: %s", exc)


# ── Per-session queue registry ─────────────────────────────────────────────
# Keyed by session_id → asyncio.Queue[(mode, notification)]
_session_queues: Dict[str, asyncio.Queue] = {}


# ── Public entry point ─────────────────────────────────────────────────────

async def start_monitoring(
    connection: iterm2.Connection,
    app: iterm2.App,
    state: TermFixState,
) -> None:
    """Launch the global monitor router + the new-session watcher.

    This coroutine runs forever; it should be awaited from the main entry
    point so the event loop keeps running.
    """
    await asyncio.gather(
        _run_global_prompt_monitor(connection, state),
        _watch_new_sessions(connection, app, state),
    )


# ── Global prompt event router ─────────────────────────────────────────────

async def _run_global_prompt_monitor(
    connection: iterm2.Connection,
    state: TermFixState,
) -> None:
    """Read every PromptMonitor notification and route it to the right queue."""
    modes = [
        iterm2.PromptMonitor.Mode.COMMAND_START,
        iterm2.PromptMonitor.Mode.COMMAND_END,
    ]
    while True:
        try:
            async with iterm2.PromptMonitor(connection, modes=modes) as mon:
                while True:
                    try:
                        raw = await mon.async_get()
                        mode, notification = _unpack_notification(raw)
                        session_id = _extract_session_id(notification)
                        if session_id and session_id in _session_queues:
                            await _session_queues[session_id].put((mode, notification))
                    except asyncio.CancelledError:
                        raise
                    except Exception as exc:
                        logger.error("Error processing prompt notification: %s", exc)
        except asyncio.CancelledError:
            return
        except Exception as exc:
            logger.error("Global PromptMonitor crashed: %s — restarting in 2 s", exc)
            await asyncio.sleep(2)


def _unpack_notification(raw: Any):
    """Normalise the notification into (mode, notification_obj).

    Handles two forms emitted by different iTerm2 Python API versions:
      - Tuple form:  (Mode, info_obj)
      - Object form: single object with a .mode attribute
    """
    if isinstance(raw, tuple) and len(raw) == 2:
        return raw[0], raw[1]
    # Object form
    mode = getattr(raw, "mode", raw)
    return mode, raw


def _extract_session_id(notification: Any) -> Optional[str]:
    """Best-effort extraction of a session_id from a notification object."""
    for attr in ("session_id", "sessionId", "session"):
        val = getattr(notification, attr, None)
        if isinstance(val, str) and val:
            return val
    return None


# ── New-session watcher ────────────────────────────────────────────────────

async def _watch_new_sessions(
    connection: iterm2.Connection,
    app: iterm2.App,
    state: TermFixState,
) -> None:
    """Detect new sessions and launch a worker task for each."""
    # Register existing sessions first
    for window in app.windows:
        for tab in window.tabs:
            for session in tab.sessions:
                _register_session(connection, session, state)

    # Then watch for sessions created after startup
    async with iterm2.EachSessionOnceMonitor(app) as mon:
        while True:
            try:
                session_id: str = await mon.async_get()
                session = app.get_session_by_id(session_id)
                if session:
                    _register_session(connection, session, state)
                else:
                    logger.debug("New session %s not found in app — skipping", session_id)
            except asyncio.CancelledError:
                return
            except Exception as exc:
                logger.error("EachSessionOnceMonitor error: %s", exc)


def _register_session(
    connection: iterm2.Connection,
    session: iterm2.Session,
    state: TermFixState,
) -> None:
    """Create a queue for the session and start its worker task."""
    sid = session.session_id
    if sid in _session_queues:
        return  # Already registered
    queue: asyncio.Queue = asyncio.Queue()
    _session_queues[sid] = queue
    asyncio.create_task(
        _session_worker(connection, session, queue, state),
        name=f"termfix-worker-{sid[:8]}",
    )
    logger.debug("Registered session %s", sid)


# ── Per-session worker ─────────────────────────────────────────────────────

async def _session_worker(
    connection: iterm2.Connection,
    session: iterm2.Session,
    queue: asyncio.Queue,
    state: TermFixState,
) -> None:
    """Process command start/end events for one session.

    This task lives as long as the session exists.  Any exception is caught
    and logged so one broken session never affects others.
    """
    session_id = session.session_id
    current_command: str = ""

    logger.debug("Worker started for session %s", session_id)
    try:
        while True:
            try:
                mode, notification = await queue.get()
                await _handle_notification(
                    connection, session, state,
                    mode, notification,
                    current_command_ref=[current_command],
                )
                # Update mutable reference from the helper
                # (Python doesn't have pass-by-reference for str, so use a list)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.error(
                    "Error in session worker %s: %s", session_id, exc, exc_info=True
                )
    except asyncio.CancelledError:
        logger.debug("Worker cancelled for session %s", session_id)
    finally:
        _session_queues.pop(session_id, None)
        logger.debug("Worker finished for session %s", session_id)


async def _handle_notification(
    connection: iterm2.Connection,
    session: iterm2.Session,
    state: TermFixState,
    mode: Any,
    notification: Any,
    current_command_ref: list[str],
) -> None:
    """Dispatch a single notification for one session."""
    Mode = iterm2.PromptMonitor.Mode

    if mode == Mode.COMMAND_START:
        # Capture the command string the user is about to run.
        # Shell integration provides it either on the notification object
        # or in the "command" session variable.
        cmd = _get_attr_str(notification, "command", "commandLine", "cmd")
        if not cmd:
            cmd = await _safe_get_variable(session, "command") or ""
        current_command_ref[0] = cmd
        logger.debug("Session %s COMMAND_START: %r", session.session_id, cmd)

    elif mode == Mode.COMMAND_END:
        exit_status = _get_attr_int(notification, "exit_status", "exitStatus", "returnCode")
        command = current_command_ref[0]

        # Also try the session variable in case we missed the start event
        if not command:
            command = await _safe_get_variable(session, "command") or ""

        logger.debug(
            "Session %s COMMAND_END: exit=%d cmd=%r",
            session.session_id, exit_status, command,
        )

        if exit_status != 0:
            await _handle_error(connection, session, state, command, exit_status)

        # Reset for the next command
        current_command_ref[0] = ""


async def _handle_error(
    connection: iterm2.Connection,
    session: iterm2.Session,
    state: TermFixState,
    command: str,
    exit_code: int,
) -> None:
    """Collect context and record a new error entry in shared state."""
    logger.info(
        "Error captured — session=%s exit=%d cmd=%r",
        session.session_id, exit_code, command,
    )
    try:
        ctx = await collect_context(
            connection, session,
            context_lines=state.context_lines,
            command=command,
            exit_code=exit_code,
        )
        entry = ErrorEntry(
            session_id=session.session_id,
            command=command,
            exit_code=exit_code,
            context=ctx,
        )
        await state.add_error(entry)
        await state.notify_ui_update()
    except Exception as exc:
        logger.error("Failed to record error entry: %s", exc, exc_info=True)


# ── Notification attribute helpers ────────────────────────────────────────

def _get_attr_str(obj: Any, *names: str) -> str:
    """Return the first non-empty string attribute found on obj."""
    for name in names:
        val = getattr(obj, name, None)
        if isinstance(val, str) and val:
            return val
    return ""


def _get_attr_int(obj: Any, *names: str) -> int:
    """Return the first integer attribute found on obj, defaulting to 0."""
    for name in names:
        val = getattr(obj, name, None)
        if isinstance(val, int):
            return val
    return 0


async def _safe_get_variable(session: iterm2.Session, name: str) -> Optional[str]:
    """Read a session variable without raising."""
    try:
        val = await session.async_get_variable(name)
        return str(val) if val else None
    except Exception:
        return None
