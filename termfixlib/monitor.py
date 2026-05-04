"""Per-session shell integration monitor for TermFix."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import threading
import time
from dataclasses import dataclass, field
from typing import Optional
from uuid import uuid4

try:
    import iterm2
except ImportError:  # Allows pure state/history helpers to be imported outside iTerm2.
    iterm2 = None  # type: ignore[assignment]

from .config import (
    DEFAULT_BASE_URL,
    DEFAULT_CONTEXT_LINES,
    DEFAULT_FIX_HOTKEY,
    DEFAULT_MAX_TOKENS,
    DEFAULT_MODEL,
    DEFAULT_PROMPT_HOTKEY,
    PROMPT_HISTORY_LIMIT,
    PROMPT_HISTORY_PATH,
)
from .context import collect_context

logger = logging.getLogger(__name__)

PROMPT_HISTORY_VERSION = 2
ERROR_RETENTION_LIMIT = 100
HANDLED_ERROR_RETENTION_LIMIT = 20
SHELL_INTEGRATION_START_TIMEOUT = 30.0
_PROMPT_CONTEXT_TEXT_LIMIT = 2_000
_PROMPT_CONTEXT_KEYS = (
    "cwd",
    "shell",
    "os_name",
    "os_version",
    "context_lines",
    "terminal_output_line_count",
)


def _safe_float(value, default: float) -> float:  # noqa: ANN001 - JSON payload input.
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _serializable_messages(messages) -> list[dict]:  # noqa: ANN001 - JSON payload input.
    """Return only provider-safe user/assistant text messages."""
    cleaned: list[dict] = []
    if not isinstance(messages, list):
        return cleaned
    for message in messages:
        if not isinstance(message, dict):
            continue
        role = message.get("role")
        if role not in ("user", "assistant"):
            continue
        content = str(message.get("content") or "")
        if content:
            cleaned.append({"role": role, "content": content})
    return cleaned


def _line_count(value) -> int:  # noqa: ANN001 - JSON payload input.
    if value is None:
        return 0
    text = str(value)
    if not text:
        return 0
    return len(text.splitlines()) or 1


def _serializable_context(context) -> dict:  # noqa: ANN001 - JSON payload input.
    """Return non-sensitive terminal context metadata for prompt history."""
    if not isinstance(context, dict):
        return {}

    cleaned: dict = {}
    for key in _PROMPT_CONTEXT_KEYS:
        if key not in context:
            continue
        value = context.get(key)
        if value is None:
            continue
        if key in ("context_lines", "terminal_output_line_count"):
            try:
                cleaned[key] = int(value)
            except (TypeError, ValueError):
                continue
            continue
        text = str(value)
        if len(text) > _PROMPT_CONTEXT_TEXT_LIMIT:
            text = text[-_PROMPT_CONTEXT_TEXT_LIMIT:]
        cleaned[key] = text
    if "terminal_output_line_count" not in cleaned and "terminal_output" in context:
        cleaned["terminal_output_line_count"] = _line_count(context.get("terminal_output"))
    return cleaned


@dataclass
class ErrorEntry:
    """A single captured command failure, enriched with LLM analysis lazily."""
    session_id: str
    command: str
    exit_code: int
    context: dict
    id: str = field(default_factory=lambda: uuid4().hex)
    timestamp: float = field(default_factory=time.time)
    result: Optional[str] = None
    analyzed: bool = False
    analysis_started: bool = False
    handled: bool = False
    handled_at: Optional[float] = None
    status: str = "pending"
    updated_at: float = field(default_factory=time.time)


@dataclass
class PromptEntry:
    """A manual user prompt scoped to one terminal session snapshot."""
    session_id: str
    context: dict
    session: Optional[iterm2.Session] = None
    messages: list[dict] = field(default_factory=list)
    id: str = field(default_factory=lambda: uuid4().hex)
    timestamp: float = field(default_factory=time.time)
    user_prompt: str = ""
    result: Optional[str] = None
    analysis_started: bool = False
    status: str = "input"
    updated_at: float = field(default_factory=time.time)
    restored: bool = False
    source_session_id: str = ""


class TermFixState:
    """Shared state between the monitor and the UI layer."""

    def __init__(self) -> None:
        self._lock = asyncio.Lock()
        self._state_lock = threading.RLock()
        self.errors: list[ErrorEntry] = []
        self.prompts: list[PromptEntry] = self._load_prompt_history()

        self.api_key: str = ""
        self.api_key_error: str = ""
        self.base_url: str = DEFAULT_BASE_URL
        self.base_url_error: str = ""
        self.model: str = DEFAULT_MODEL
        self.context_lines: int = DEFAULT_CONTEXT_LINES
        self.max_tokens: int = DEFAULT_MAX_TOKENS
        self.max_tokens_error: str = ""
        self.fix_hotkey: str = DEFAULT_FIX_HOTKEY
        self.fix_hotkey_error: str = ""
        self.prompt_hotkey: str = DEFAULT_PROMPT_HOTKEY
        self.prompt_hotkey_error: str = ""
        self.hotkey_listener_error: str = ""
        self.analyzing: bool = False
        self.analysis_tasks: dict[str, asyncio.Task] = {}

        self.component: Optional[iterm2.StatusBarComponent] = None
        self.connection: Optional[iterm2.Connection] = None
        self.loop: Optional[asyncio.AbstractEventLoop] = None
        self.status_server = None
        self.status_server_lock = threading.Lock()
        self.status_server_url: str = ""
        self.status_server_token: str = ""
        self.terminal_sessions: dict[str, iterm2.Session] = {}
        self.prompt_sessions: dict[str, iterm2.Session] = {}
        self.shell_integration_missing_sessions: set[str] = set()
        self.popover_last_seen: dict[str, float] = {}
        self.popover_close_requests: set[str] = set()
        self.last_viewed_error_id: str = ""

    async def add_error(self, entry: ErrorEntry) -> None:
        async with self._lock:
            with self._state_lock:
                self.errors.append(entry)
                self._prune_error_history()

    async def remove_error(self, entry: ErrorEntry) -> None:
        async with self._lock:
            with self._state_lock:
                self._discard_error(entry)

    async def add_prompt(self, entry: PromptEntry) -> None:
        async with self._lock:
            with self._state_lock:
                self.prompts.append(entry)
                self._trim_prompt_history()

    @property
    def error_count(self) -> int:
        return self.unhandled_error_count

    @property
    def unhandled_error_count(self) -> int:
        with self._state_lock:
            return sum(1 for entry in self.errors if not entry.handled)

    @property
    def total_error_count(self) -> int:
        with self._state_lock:
            return len(self.errors)

    def latest_error(self) -> Optional[ErrorEntry]:
        with self._state_lock:
            return self.errors[-1] if self.errors else None

    def latest_unhandled_error(self, session_id: Optional[str] = None) -> Optional[ErrorEntry]:
        with self._state_lock:
            for entry in reversed(self.errors):
                if entry.handled:
                    continue
                if session_id is not None and entry.session_id != session_id:
                    continue
                return entry
        return None

    def last_viewed_error(self, session_id: Optional[str] = None) -> Optional[ErrorEntry]:
        with self._state_lock:
            if not self.last_viewed_error_id:
                return None
            for entry in reversed(self.errors):
                if entry.id != self.last_viewed_error_id:
                    continue
                if session_id is not None and entry.session_id != session_id:
                    return None
                return entry
        return None

    def recent_errors(
        self,
        active_entry_id: str = "",
        limit: int = 10,
    ) -> list[ErrorEntry]:
        """Return newest retained errors, always including the active entry."""
        with self._state_lock:
            limit = max(1, int(limit))
            entries = list(self.errors[-limit:])
            active_entry = None
            if active_entry_id:
                for entry in self.errors:
                    if entry.id == active_entry_id:
                        active_entry = entry
                        break
            if active_entry is not None and active_entry.id not in {
                entry.id for entry in entries
            }:
                if len(entries) >= limit:
                    entries = entries[1:]
                entries.append(active_entry)
            return list(reversed(entries))

    def get_error(self, entry_id: str) -> Optional[ErrorEntry]:
        with self._state_lock:
            for entry in self.errors:
                if entry.id == entry_id:
                    return entry
        return None

    def mark_error_handled(self, entry_id: str) -> bool:
        with self._state_lock:
            entry = self.get_error(entry_id)
            if entry is None or entry.handled:
                return False
            handled_at = time.time()
            entry.handled = True
            entry.handled_at = handled_at
            entry.updated_at = handled_at
            self.last_viewed_error_id = entry.id
            self._prune_error_history()
            return True

    def _prune_error_history(self) -> None:
        """Bound captured errors, pruning handled history before live failures."""
        with self._state_lock:
            handled_limit = max(0, int(HANDLED_ERROR_RETENTION_LIMIT))
            handled_entries = [entry for entry in self.errors if entry.handled]
            handled_overflow = len(handled_entries) - handled_limit
            if handled_overflow > 0:
                for entry in handled_entries[:handled_overflow]:
                    self._discard_error(entry)

            total_limit = max(0, int(ERROR_RETENTION_LIMIT))
            total_overflow = len(self.errors) - total_limit
            if total_overflow <= 0:
                return

            for entry in list(self.errors):
                if total_overflow <= 0:
                    break
                if not entry.handled:
                    continue
                if self._discard_error(entry):
                    total_overflow -= 1

            total_overflow = len(self.errors) - total_limit
            if total_overflow <= 0:
                return

            for entry in list(self.errors)[:total_overflow]:
                self._discard_error(entry)

    def _discard_error(self, entry: ErrorEntry) -> bool:
        with self._state_lock:
            try:
                self.errors.remove(entry)
            except ValueError:
                return False
            self.popover_last_seen.pop(entry.id, None)
            self.popover_close_requests.discard(entry.id)
            if self.last_viewed_error_id == entry.id:
                self.last_viewed_error_id = ""
            return True

    def latest_prompt(self, session_id: str) -> Optional[PromptEntry]:
        with self._state_lock:
            for entry in reversed(self.prompts):
                if entry.session_id == session_id:
                    return entry
        return None

    def latest_prompt_any(self) -> Optional[PromptEntry]:
        with self._state_lock:
            return self.prompts[-1] if self.prompts else None

    def get_prompt(self, entry_id: str) -> Optional[PromptEntry]:
        with self._state_lock:
            for entry in self.prompts:
                if entry.id == entry_id:
                    return entry
        return None

    def prompt_entries(self) -> list[PromptEntry]:
        """Return a stable snapshot of retained prompt entries."""
        with self._state_lock:
            return list(self.prompts)

    @property
    def shell_integration_missing(self) -> bool:
        with self._state_lock:
            return bool(self.shell_integration_missing_sessions)

    def mark_shell_integration_missing(self, session_id: str) -> bool:
        with self._state_lock:
            if session_id in self.shell_integration_missing_sessions:
                return False
            self.shell_integration_missing_sessions.add(session_id)
            return True

    def clear_shell_integration_missing(self, session_id: str) -> bool:
        with self._state_lock:
            if session_id not in self.shell_integration_missing_sessions:
                return False
            self.shell_integration_missing_sessions.discard(session_id)
            return True

    def refresh_analyzing(self) -> None:
        with self._state_lock:
            self.analyzing = any(entry.status == "streaming" for entry in self.errors) or any(
                entry.status == "streaming" for entry in self.prompts
            )

    def save_prompt_history(self) -> None:
        """Persist prompt conversations to a fixed on-disk JSON file."""
        self._trim_prompt_history()
        records = []
        with self._state_lock:
            for entry in self.prompts:
                if not entry.messages:
                    continue
                records.append(
                    {
                        "id": entry.id,
                        "timestamp": entry.timestamp,
                        "updated_at": entry.updated_at,
                        "source_session_id": entry.session_id or entry.source_session_id,
                        "context": _serializable_context(entry.context),
                        "messages": _serializable_messages(entry.messages),
                    }
                )

        try:
            os.makedirs(os.path.dirname(PROMPT_HISTORY_PATH), exist_ok=True)
            tmp_path = f"{PROMPT_HISTORY_PATH}.tmp"
            with open(tmp_path, "w", encoding="utf-8") as fh:
                json.dump(
                    {
                        "version": PROMPT_HISTORY_VERSION,
                        "prompts": records[-PROMPT_HISTORY_LIMIT:],
                    },
                    fh,
                    ensure_ascii=False,
                    indent=2,
                )
            os.replace(tmp_path, PROMPT_HISTORY_PATH)
        except Exception as exc:
            logger.warning("Could not save prompt history: %s", exc)

    def _load_prompt_history(self) -> list[PromptEntry]:
        """Load persisted prompt conversations from disk."""
        try:
            with open(PROMPT_HISTORY_PATH, "r", encoding="utf-8") as fh:
                payload = json.load(fh)
        except FileNotFoundError:
            return []
        except Exception as exc:
            logger.warning("Could not load prompt history: %s", exc)
            return []

        prompts = payload.get("prompts", []) if isinstance(payload, dict) else []
        entries: list[PromptEntry] = []
        for item in prompts:
            if not isinstance(item, dict):
                continue
            messages = _serializable_messages(item.get("messages", []))
            if not messages:
                continue
            context = _serializable_context(item.get("context", {}))
            entry = PromptEntry(
                session_id="",
                context=context,
                messages=messages,
                id=str(item.get("id") or uuid4().hex),
                timestamp=_safe_float(item.get("timestamp"), time.time()),
                updated_at=_safe_float(item.get("updated_at"), time.time()),
                status="done",
                restored=True,
                source_session_id=str(
                    item.get("session_id") or item.get("source_session_id") or ""
                ),
            )
            entries.append(entry)

        entries.sort(key=lambda entry: entry.updated_at)
        return entries[-PROMPT_HISTORY_LIMIT:]

    def _trim_prompt_history(self) -> None:
        with self._state_lock:
            if len(self.prompts) <= PROMPT_HISTORY_LIMIT:
                return
            self.prompts = self.prompts[-PROMPT_HISTORY_LIMIT:]

    def mark_popover_seen(self, entry_id: str) -> None:
        with self._state_lock:
            self.popover_last_seen[entry_id] = time.time()

    def is_popover_open(self, entry_id: str, ttl: float = 1.5) -> bool:
        with self._state_lock:
            last_seen = self.popover_last_seen.get(entry_id, 0)
        return time.time() - last_seen < ttl

    def request_popover_close(self, entry_id: str) -> None:
        with self._state_lock:
            self.popover_close_requests.add(entry_id)

    def consume_popover_close_request(self, entry_id: str) -> bool:
        with self._state_lock:
            if entry_id not in self.popover_close_requests:
                return False
            self.popover_close_requests.discard(entry_id)
            return True

    def mark_popover_closed(self, entry_id: str) -> None:
        with self._state_lock:
            self.popover_last_seen.pop(entry_id, None)
            self.popover_close_requests.discard(entry_id)

    async def notify_ui_update(self) -> None:
        """Ask the status bar component to re-render and update the badge."""
        if self.component is None or self.connection is None:
            return
        try:
            await self.component.async_invalidate(self.connection)
        except Exception as exc:
            logger.debug("async_invalidate failed: %s", exc)
        try:
            await self.component.async_set_unread_count(
                None,
                self.unhandled_error_count,
            )
        except Exception as exc:
            logger.debug("async_set_unread_count failed: %s", exc)


async def start_monitoring(
    connection: iterm2.Connection,
    app: iterm2.App,
    state: TermFixState,
) -> None:
    """Start a dedicated PromptMonitor task for each session."""

    async def _task(session_id: str) -> None:
        session = app.get_session_by_id(session_id)
        if session is None:
            logger.debug("Session %s disappeared before monitor startup", session_id)
            return
        await _session_worker(connection, session, state)

    await iterm2.EachSessionOnceMonitor.async_foreach_session_create_task(app, _task)


async def _session_worker(
    connection: iterm2.Connection,
    session: iterm2.Session,
    state: TermFixState,
) -> None:
    """Monitor one session's shell-integration events."""
    session_id = session.session_id
    current_command = ""
    modes = [
        iterm2.PromptMonitor.Mode.COMMAND_START,
        iterm2.PromptMonitor.Mode.COMMAND_END,
    ]

    logger.debug("Worker started for session %s", session_id)
    state.terminal_sessions[session_id] = session
    saw_command_start = False
    shell_integration_warning_sent = False
    try:
        async with iterm2.PromptMonitor(connection, session_id, modes=modes) as mon:
            while True:
                try:
                    if saw_command_start or shell_integration_warning_sent:
                        event = await mon.async_get()
                    else:
                        event = await asyncio.wait_for(
                            mon.async_get(),
                            timeout=SHELL_INTEGRATION_START_TIMEOUT,
                        )
                    mode, payload = _unpack_event(event)
                    _log_prompt_event(session_id, mode, payload)
                    if mode == iterm2.PromptMonitor.Mode.COMMAND_START:
                        saw_command_start = True
                        if state.clear_shell_integration_missing(session_id):
                            await state.notify_ui_update()
                        current_command = payload if isinstance(payload, str) else ""
                        if not current_command:
                            current_command = await _safe_get_variable(session, "command") or ""
                    elif mode == iterm2.PromptMonitor.Mode.COMMAND_END:
                        exit_status = payload if isinstance(payload, int) else 0
                        command = current_command or await _safe_get_variable(session, "command") or ""
                        if exit_status != 0:
                            await _handle_error(
                                connection,
                                session,
                                state,
                                command,
                                exit_status,
                            )
                        current_command = ""
                except asyncio.CancelledError:
                    raise
                except asyncio.TimeoutError:
                    if not saw_command_start:
                        shell_integration_warning_sent = True
                        if state.mark_shell_integration_missing(session_id):
                            logger.warning(
                                "Shell integration not detected for session %s",
                                session_id,
                            )
                            await state.notify_ui_update()
                except Exception as exc:
                    logger.error(
                        "Error in session worker %s: %s",
                        session_id,
                        exc,
                        exc_info=True,
                    )
    except asyncio.CancelledError:
        logger.debug("Worker cancelled for session %s", session_id)
    except Exception as exc:
        logger.error("PromptMonitor failed for session %s: %s", session_id, exc, exc_info=True)


def _unpack_event(event):
    """Normalize PromptMonitor events into (mode, payload)."""
    if isinstance(event, tuple) and len(event) == 2:
        return event[0], event[1]
    return getattr(event, "mode", event), getattr(event, "value", None)


def _log_prompt_event(session_id: str, mode, payload) -> None:  # noqa: ANN001
    """Log prompt-monitor lifecycle metadata without exposing command payloads."""
    mode_name = getattr(mode, "name", mode)
    payload_type = "none" if payload is None else type(payload).__name__
    logger.info(
        "Prompt event - session=%s mode=%s payload_type=%s",
        session_id,
        mode_name,
        payload_type,
    )
    logger.debug(
        "Prompt event payload - session=%s mode=%s payload=%r",
        session_id,
        mode_name,
        payload,
    )


async def _handle_error(
    connection: iterm2.Connection,
    session: iterm2.Session,
    state: TermFixState,
    command: str,
    exit_code: int,
) -> None:
    """Collect context and record a new error entry in shared state."""
    logger.info(
        "Error captured - session=%s exit=%d",
        session.session_id,
        exit_code,
    )
    logger.debug(
        "Captured failing command - session=%s exit=%d cmd=%r",
        session.session_id,
        exit_code,
        command,
    )
    try:
        state.terminal_sessions[session.session_id] = session
        ctx = await collect_context(
            connection,
            session,
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


async def _safe_get_variable(session: iterm2.Session, name: str) -> Optional[str]:
    """Read a session variable without raising."""
    try:
        val = await session.async_get_variable(name)
        return str(val) if val else None
    except Exception:
        return None
