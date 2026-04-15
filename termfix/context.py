"""
Collect execution context from an iTerm2 session.

The returned dict is passed verbatim to the LLM as the user message body.
All fields degrade gracefully — individual failures produce empty strings
rather than raising, so a single bad API call never aborts the pipeline.
"""

from __future__ import annotations

import logging
import os
import platform
from typing import Optional

import iterm2

logger = logging.getLogger(__name__)


async def collect_context(
    connection: iterm2.Connection,
    session: iterm2.Session,
    context_lines: int = 50,
    command: Optional[str] = None,
    exit_code: int = 1,
) -> dict:
    """Return a structured dict describing the failing command's environment.

    Args:
        connection: Active iTerm2 connection.
        session:    The session where the command failed.
        context_lines: How many terminal lines to capture for output context.
        command:    The command string that was run (from shell integration).
        exit_code:  The numeric exit code of the failed command.

    Returns:
        Dict with keys: command, exit_code, terminal_output, cwd, shell,
        os_name, os_version.
    """
    ctx: dict = {
        "command": command or "",
        "exit_code": exit_code,
        "terminal_output": "",
        "cwd": "",
        "shell": "",
        "os_name": platform.system(),
        "os_version": platform.release(),
    }

    # ── Terminal output ────────────────────────────────────────────────────
    ctx["terminal_output"] = await _get_terminal_output(session, context_lines)

    # ── Current working directory ──────────────────────────────────────────
    ctx["cwd"] = await _get_variable(session, "path") or ""

    # ── Shell type ────────────────────────────────────────────────────────
    shell_var = await _get_variable(session, "shell")
    if shell_var:
        ctx["shell"] = os.path.basename(shell_var)
    else:
        ctx["shell"] = os.path.basename(os.environ.get("SHELL", "unknown"))

    return ctx


# ── Private helpers ────────────────────────────────────────────────────────

async def _get_terminal_output(session: iterm2.Session, max_lines: int) -> str:
    """Capture the last *max_lines* lines visible on the terminal screen.

    iTerm2 exposes two complementary APIs:
      • session.async_get_screen_contents()  — current visible screen
      • session.async_get_contents(r0, r1)   — arbitrary scrollback range

    We try async_get_screen_contents first (most portable), then fall back to
    async_get_contents if the first call is unavailable.
    """
    lines: list[str] = []

    # Attempt 1: screen contents (current visible buffer)
    try:
        screen: iterm2.ScreenContents = await session.async_get_screen_contents()
        for i in range(screen.number_of_lines):
            line = screen.line(i)
            text: str = line.string if line.string else ""
            lines.append(text.rstrip())
        return "\n".join(lines[-max_lines:])
    except AttributeError:
        # async_get_screen_contents might not exist in all API versions
        logger.debug("async_get_screen_contents unavailable, trying async_get_contents")
    except Exception as exc:
        logger.warning("Could not get screen contents via async_get_screen_contents: %s", exc)

    # Attempt 2: explicit row range via async_get_contents(first_row, last_row)
    # We can't reliably probe the scrollback height on older API versions, so
    # request a deliberately oversized range (0 … _LARGE).  iTerm2 clamps the
    # result to whatever is actually available, giving us all scrollback lines.
    # We then take only the last max_lines from that full result.
    _LARGE = 100_000
    try:
        contents = await session.async_get_contents(0, _LARGE)
        for i in range(contents.number_of_lines):
            line = contents.line(i)
            text = line.string if line.string else ""
            lines.append(text.rstrip())
        return "\n".join(lines[-max_lines:])
    except Exception as exc:
        logger.warning("Could not get terminal contents: %s", exc)

    return ""


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
        "Analyze the error above and return your JSON response.",
    ]
    return "\n".join(lines)
