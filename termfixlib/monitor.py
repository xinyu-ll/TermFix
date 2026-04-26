"""Per-session shell integration monitor for TermFix."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from dataclasses import dataclass, field
from typing import Optional
from uuid import uuid4

import iterm2

from .config import (
    DEFAULT_BASE_URL,
    DEFAULT_CONTEXT_LINES,
    DEFAULT_MODEL,
    PROMPT_HISTORY_LIMIT,
    PROMPT_HISTORY_PATH,
)
from .context import collect_context

logger = logging.getLogger(__name__)


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


class TermFixState:
    """Shared state between the monitor and the UI layer."""

    def __init__(self) -> None:
        self._lock = asyncio.Lock()
        self.errors: list[ErrorEntry] = []
        self.prompts: list[PromptEntry] = self._load_prompt_history()

        self.api_key: str = ""
        self.base_url: str = DEFAULT_BASE_URL
        self.model: str = DEFAULT_MODEL
        self.context_lines: int = DEFAULT_CONTEXT_LINES
        self.analyzing: bool = False
        self.analysis_tasks: dict[str, asyncio.Task] = {}

        self.component: Optional[iterm2.StatusBarComponent] = None
        self.connection: Optional[iterm2.Connection] = None
        self.loop: Optional[asyncio.AbstractEventLoop] = None
        self.status_server = None
        self.status_server_url: str = ""
        self.status_server_token: str = ""
        self.popover_last_seen: dict[str, float] = {}
        self.popover_close_requests: set[str] = set()

    async def add_error(self, entry: ErrorEntry) -> None:
        async with self._lock:
            self.errors.append(entry)

    async def remove_error(self, entry: ErrorEntry) -> None:
        async with self._lock:
            try:
                self.errors.remove(entry)
            except ValueError:
                pass

    async def add_prompt(self, entry: PromptEntry) -> None:
        async with self._lock:
            self.prompts.append(entry)
            self._trim_prompt_history()

    @property
    def error_count(self) -> int:
        return len(self.errors)

    def latest_error(self) -> Optional[ErrorEntry]:
        return self.errors[-1] if self.errors else None

    def get_error(self, entry_id: str) -> Optional[ErrorEntry]:
        for entry in self.errors:
            if entry.id == entry_id:
                return entry
        return None

    def latest_prompt(self, session_id: str) -> Optional[PromptEntry]:
        for entry in reversed(self.prompts):
            if entry.session_id == session_id:
                return entry
        return None

    def latest_prompt_any(self) -> Optional[PromptEntry]:
        return self.prompts[-1] if self.prompts else None

    def get_prompt(self, entry_id: str) -> Optional[PromptEntry]:
        for entry in self.prompts:
            if entry.id == entry_id:
                return entry
        return None

    def refresh_analyzing(self) -> None:
        self.analyzing = any(entry.status == "streaming" for entry in self.errors) or any(
            entry.status == "streaming" for entry in self.prompts
        )

    def save_prompt_history(self) -> None:
        """Persist prompt conversations to a fixed on-disk JSON file."""
        self._trim_prompt_history()
        records = []
        for entry in self.prompts:
            if not entry.messages:
                continue
            records.append(
                {
                    "id": entry.id,
                    "timestamp": entry.timestamp,
                    "updated_at": entry.updated_at,
                    "messages": _serializable_messages(entry.messages),
                }
            )

        try:
            os.makedirs(os.path.dirname(PROMPT_HISTORY_PATH), exist_ok=True)
            tmp_path = f"{PROMPT_HISTORY_PATH}.tmp"
            with open(tmp_path, "w", encoding="utf-8") as fh:
                json.dump(
                    {"version": 1, "prompts": records[-PROMPT_HISTORY_LIMIT:]},
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
            entry = PromptEntry(
                session_id="",
                context={},
                messages=messages,
                id=str(item.get("id") or uuid4().hex),
                timestamp=_safe_float(item.get("timestamp"), time.time()),
                updated_at=_safe_float(item.get("updated_at"), time.time()),
                status="done",
            )
            entries.append(entry)

        entries.sort(key=lambda entry: entry.updated_at)
        return entries[-PROMPT_HISTORY_LIMIT:]

    def _trim_prompt_history(self) -> None:
        if len(self.prompts) <= PROMPT_HISTORY_LIMIT:
            return
        self.prompts = self.prompts[-PROMPT_HISTORY_LIMIT:]

    def mark_popover_seen(self, entry_id: str) -> None:
        self.popover_last_seen[entry_id] = time.time()

    def is_popover_open(self, entry_id: str, ttl: float = 1.5) -> bool:
        last_seen = self.popover_last_seen.get(entry_id, 0)
        return time.time() - last_seen < ttl

    def request_popover_close(self, entry_id: str) -> None:
        self.popover_close_requests.add(entry_id)

    def consume_popover_close_request(self, entry_id: str) -> bool:
        if entry_id not in self.popover_close_requests:
            return False
        self.popover_close_requests.remove(entry_id)
        return True

    def mark_popover_closed(self, entry_id: str) -> None:
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
                self.error_count,
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
    try:
        async with iterm2.PromptMonitor(connection, session_id, modes=modes) as mon:
            while True:
                try:
                    event = await mon.async_get()
                    mode, payload = _unpack_event(event)
                    logger.info(
                        "Prompt event — session=%s mode=%s payload=%r",
                        session_id,
                        getattr(mode, "name", mode),
                        payload,
                    )
                    if mode == iterm2.PromptMonitor.Mode.COMMAND_START:
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
        session.session_id,
        exit_code,
        command,
    )
    try:
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
