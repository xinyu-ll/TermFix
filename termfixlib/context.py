"""
Collect execution context from an iTerm2 session.

The returned dict is passed verbatim to the LLM as the user message body.
All fields degrade gracefully — individual failures produce empty strings
rather than raising, so a single bad API call never aborts the pipeline.
"""

from __future__ import annotations

from collections import deque
import logging
import os
import platform
from typing import Optional

try:
    import iterm2
except ImportError:  # Allows pure helpers to be imported outside iTerm2.
    iterm2 = None  # type: ignore[assignment]

from .config import DEFAULT_CONTEXT_LINES, MAX_CONTEXT_LINES, MIN_CONTEXT_LINES

logger = logging.getLogger(__name__)

MAX_TERMINAL_CONTEXT_LINES = MAX_CONTEXT_LINES


async def collect_context(
    connection: iterm2.Connection,
    session: iterm2.Session,
    context_lines: int = DEFAULT_CONTEXT_LINES,
    command: Optional[str] = None,
    exit_code: int = 1,
) -> dict:
    """Return a structured dict describing the failing command's environment."""
    ctx: dict = {
        "command": command or "",
        "exit_code": exit_code,
        "terminal_output": "",
        "cwd": "",
        "shell": "",
        "os_name": platform.system(),
        "os_version": platform.release(),
    }

    ctx["terminal_output"] = await _get_terminal_output(connection, session, context_lines)
    ctx["terminal_output_line_count"] = _count_lines(ctx["terminal_output"])
    ctx["cwd"] = await _get_variable(session, "path") or ""

    shell_var = await _get_variable(session, "shell")
    if shell_var:
        ctx["shell"] = os.path.basename(shell_var)
    else:
        ctx["shell"] = os.path.basename(os.environ.get("SHELL", "unknown"))

    return ctx


def normalize_context_lines(value, default: int = DEFAULT_CONTEXT_LINES) -> int:  # noqa: ANN001 - runtime knob input.
    """Return a bounded line count that is safe for terminal buffer slicing."""
    try:
        count = int(value)
    except (TypeError, ValueError):
        count = default
    return max(MIN_CONTEXT_LINES, min(count, MAX_CONTEXT_LINES))


async def _get_terminal_output(
    connection_or_session,
    session_or_max_lines,
    max_lines=None,
) -> str:
    """Capture the last *max_lines* lines available from the terminal session."""
    if max_lines is None:
        connection = None
        session = connection_or_session
        max_lines = session_or_max_lines
    else:
        connection = connection_or_session
        session = session_or_max_lines

    line_count = _bounded_line_count(max_lines)
    if line_count == 0:
        return ""

    try:
        contents = await _get_recent_contents(connection, session, line_count)
        return _join_tail_lines(contents, line_count)
    except AttributeError:
        logger.debug("bounded terminal contents unavailable, trying async_get_screen_contents")
    except Exception as exc:
        logger.warning("Could not get terminal contents: %s", exc)

    try:
        screen: iterm2.ScreenContents = await session.async_get_screen_contents()
        return _join_tail_lines(screen, line_count)
    except AttributeError:
        logger.debug("async_get_screen_contents unavailable")
    except Exception as exc:
        logger.warning("Could not get screen contents via async_get_screen_contents: %s", exc)

    return ""


def _bounded_line_count(max_lines: int) -> int:
    """Return a safe, bounded terminal context size."""
    try:
        requested = int(max_lines)
    except (TypeError, ValueError):
        return 0

    if requested <= 0:
        return 0
    return min(requested, MAX_TERMINAL_CONTEXT_LINES)


async def _get_recent_contents(
    connection: Optional[iterm2.Connection],
    session: iterm2.Session,
    line_count: int,
):
    """Fetch a bounded tail window from scrollback when line info is available.

    iTerm2 recommends pairing line info and content reads in a Transaction so
    the range calculation and read observe the same terminal state.
    """
    transaction = getattr(iterm2, "Transaction", None)

    if connection is not None and transaction is not None:
        async with transaction(connection):
            return await _read_recent_contents(session, line_count)

    return await _read_recent_contents(session, line_count)


async def _read_recent_contents(session: iterm2.Session, line_count: int):
    line_info = await session.async_get_line_info()
    first_line = _first_recent_line(line_info, line_count)
    return await session.async_get_contents(first_line, line_count)


def _first_recent_line(line_info, line_count: int) -> int:  # noqa: ANN001 - iTerm2 object.
    overflow = _non_negative_int(getattr(line_info, "overflow", 0))
    scrollback_height = _non_negative_int(getattr(line_info, "scrollback_buffer_height", 0))
    mutable_height = _non_negative_int(getattr(line_info, "mutable_area_height", 0))
    available_lines = scrollback_height + mutable_height

    return overflow + max(available_lines - line_count, 0)


def _non_negative_int(value) -> int:  # noqa: ANN001 - iTerm2 numeric field.
    try:
        return max(int(value), 0)
    except (TypeError, ValueError):
        return 0


def _join_tail_lines(contents, line_count: int) -> str:  # noqa: ANN001 - iTerm2 content object.
    lines: deque[str] = deque(maxlen=line_count)
    for line in _iter_line_contents(contents):
        text = getattr(line, "string", "") or ""
        lines.append(text.rstrip())
    return "\n".join(lines)


def _iter_line_contents(contents):  # noqa: ANN001 - iTerm2 content object.
    """Yield line objects from both current and documented iTerm2 content shapes."""
    if hasattr(contents, "number_of_lines") and hasattr(contents, "line"):
        for i in range(_non_negative_int(contents.number_of_lines)):
            yield contents.line(i)
        return

    for line in contents:
        yield line


def _count_lines(text: str) -> int:
    """Return the number of captured terminal lines in *text*."""
    return text.count("\n") + 1 if text else 0


async def _get_variable(session: iterm2.Session, name: str) -> Optional[str]:
    """Safely read a session variable; returns None on any error."""
    try:
        value = await session.async_get_variable(name)
        return value if isinstance(value, str) else (str(value) if value else None)
    except Exception as exc:
        logger.debug("Could not read session variable %r: %s", name, exc)
        return None


def build_user_message(ctx: dict) -> str:
    """Format the context dict into a human-readable prompt for the LLM."""
    lines = [
        f"Command: {ctx['command'] or '(unknown)'}",
        f"Exit code: {ctx['exit_code']}",
        f"Working directory: {ctx['cwd'] or '(unknown)'}",
        f"Shell: {ctx['shell'] or '(unknown)'}",
        f"OS: {ctx['os_name']} {ctx['os_version']}",
        "",
        "Terminal output (last lines before command exited):",
        "---",
        ctx["terminal_output"] or "(no output captured)",
        "---",
        "",
        "Analyze the error above and return your Markdown response.",
    ]
    return "\n".join(lines)


def build_manual_system_prompt(ctx: dict) -> str:
    """Build the system message for a user-authored terminal question."""
    terminal_output = ctx.get("terminal_output") or ""
    terminal_line_count = ctx.get("terminal_output_line_count")
    if not isinstance(terminal_line_count, int):
        terminal_line_count = _count_lines(terminal_output)
    line_label = "line" if terminal_line_count == 1 else "lines"

    lines = [
        "You are TermFix, an assistant integrated into iTerm2.",
        "The user is working in an iTerm2 command-line environment.",
        "Use the terminal session context below as system context for this request.",
        "The command-line code/output is the recent terminal content captured from the current session.",
        "",
        f"Working directory: {ctx.get('cwd') or '(unknown)'}",
        f"Shell: {ctx.get('shell') or '(unknown)'}",
        f"OS: {ctx.get('os_name') or '(unknown)'} {ctx.get('os_version') or ''}".rstrip(),
        "",
        (
            "Current iTerm2 session command-line code/output "
            f"(last {terminal_line_count} {line_label}):"
        ),
        "---",
        terminal_output or "(no terminal content captured)",
        "---",
        "",
        "Answer the user's prompt directly and concisely. When suggesting terminal commands,",
        "prefer safe, non-destructive commands and explain destructive operations clearly before use.",
        "Do not invent terminal state that is not present in the context.",
    ]
    return "\n".join(lines)
