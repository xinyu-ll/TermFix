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

try:
    import iterm2
except ImportError:  # Allows pure helpers to be imported outside iTerm2.
    iterm2 = None  # type: ignore[assignment]

logger = logging.getLogger(__name__)


async def collect_context(
    connection: iterm2.Connection,
    session: iterm2.Session,
    context_lines: int = 50,
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

    ctx["terminal_output"] = await _get_terminal_output(session, context_lines)
    ctx["cwd"] = await _get_variable(session, "path") or ""

    shell_var = await _get_variable(session, "shell")
    if shell_var:
        ctx["shell"] = os.path.basename(shell_var)
    else:
        ctx["shell"] = os.path.basename(os.environ.get("SHELL", "unknown"))

    return ctx


async def _get_terminal_output(session: iterm2.Session, max_lines: int) -> str:
    """Capture the last *max_lines* lines available from the terminal session."""
    lines: list[str] = []

    large = 100_000
    try:
        contents = await session.async_get_contents(0, large)
        for i in range(contents.number_of_lines):
            line = contents.line(i)
            text = line.string if line.string else ""
            lines.append(text.rstrip())
        return "\n".join(lines[-max_lines:])
    except AttributeError:
        logger.debug("async_get_contents unavailable, trying async_get_screen_contents")
    except Exception as exc:
        logger.warning("Could not get terminal contents: %s", exc)

    lines = []
    try:
        screen: iterm2.ScreenContents = await session.async_get_screen_contents()
        for i in range(screen.number_of_lines):
            line = screen.line(i)
            text: str = line.string if line.string else ""
            lines.append(text.rstrip())
        return "\n".join(lines[-max_lines:])
    except AttributeError:
        logger.debug("async_get_screen_contents unavailable")
    except Exception as exc:
        logger.warning("Could not get screen contents via async_get_screen_contents: %s", exc)

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
        "Analyze the error above and return your Markdown response.",
    ]
    return "\n".join(lines)


def build_manual_system_prompt(ctx: dict) -> str:
    """Build the system message for a user-authored terminal question."""
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
        "Current iTerm2 session command-line code/output (last 50 lines):",
        "---",
        ctx.get("terminal_output") or "(no terminal content captured)",
        "---",
        "",
        "Answer the user's prompt directly and concisely. When suggesting terminal commands,",
        "prefer safe, non-destructive commands and explain destructive operations clearly before use.",
        "Do not invent terminal state that is not present in the context.",
    ]
    return "\n".join(lines)
