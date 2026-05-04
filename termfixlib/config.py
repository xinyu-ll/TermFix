"""TermFix configuration constants and defaults."""

from __future__ import annotations

import os
import re
import urllib.parse

# Status bar
STATUS_IDENTIFIER = "com.termfix.status"
STATUS_NORMAL = "✅"
STATUS_ERROR_FMT = "🔴 Fix ({count})"

# Popover dimensions
POPOVER_WIDTH = 450
POPOVER_HEIGHT = 640
PROMPT_POPOVER_WIDTH = 800
PROMPT_POPOVER_HEIGHT = 540

# Persistent prompt conversation history
PROMPT_HISTORY_PATH = os.path.expanduser(
    "~/Library/Application Support/TermFix/prompt_history.json"
)
PROMPT_HISTORY_LIMIT = 100

# Defaults (overridden by StatusBar knobs at runtime)
DEFAULT_BASE_URL = "https://api.deepseek.com"
DEFAULT_MODEL = "deepseek-chat"
DEFAULT_CONTEXT_LINES = 50
MIN_CONTEXT_LINES = 1
MAX_CONTEXT_LINES = 500
DEFAULT_MAX_TOKENS = 2048
MIN_MAX_TOKENS = 1
MAX_MAX_TOKENS = 200000
DEFAULT_FIX_HOTKEY = "Cmd+J"
DEFAULT_PROMPT_HOTKEY = "Cmd+L"

_COMMAND_LETTER_RE = re.compile(r"^(?:cmd|command)\s*\+\s*([a-z])$", re.IGNORECASE)


def normalize_base_url(value, previous: str = DEFAULT_BASE_URL) -> tuple[str, str]:  # noqa: ANN001 - runtime knob input.
    """Return a validated http(s) base URL or preserve the previous valid value."""
    raw = str(value or "").strip()
    if not raw:
        return previous or DEFAULT_BASE_URL, ""
    if any(char.isspace() for char in raw):
        return previous or DEFAULT_BASE_URL, "Base URL must not contain whitespace."

    parsed = urllib.parse.urlparse(raw)
    scheme = parsed.scheme.lower()
    if scheme not in {"http", "https"}:
        return previous or DEFAULT_BASE_URL, "Base URL must start with http:// or https://."
    if not parsed.netloc or not parsed.hostname:
        return previous or DEFAULT_BASE_URL, "Base URL must include a host name."
    try:
        parsed.port
    except ValueError:
        return previous or DEFAULT_BASE_URL, "Base URL includes an invalid port."
    if parsed.params or parsed.query or parsed.fragment:
        return previous or DEFAULT_BASE_URL, "Base URL must not include params, query, or fragment."

    normalized = urllib.parse.urlunparse(
        (scheme, parsed.netloc, parsed.path.rstrip("/"), "", "", "")
    )
    return normalized or (previous or DEFAULT_BASE_URL), ""


def normalize_max_tokens(value, previous: int = DEFAULT_MAX_TOKENS) -> tuple[int, str]:  # noqa: ANN001 - runtime knob input.
    """Return a positive max_tokens value or preserve the previous valid value."""
    raw = str(value or "").strip()
    fallback = previous if isinstance(previous, int) and previous > 0 else DEFAULT_MAX_TOKENS
    if not raw:
        return fallback, ""
    try:
        parsed = int(raw)
    except ValueError:
        return fallback, "Max tokens must be a whole number."
    if parsed < MIN_MAX_TOKENS:
        return fallback, f"Max tokens must be at least {MIN_MAX_TOKENS}."
    if parsed > MAX_MAX_TOKENS:
        return fallback, f"Max tokens must be at most {MAX_MAX_TOKENS}."
    return parsed, ""


def normalize_command_hotkey(value, previous: str, default: str) -> tuple[str, str]:  # noqa: ANN001 - runtime knob input.
    """Return a normalized Command+letter hotkey or preserve the previous valid value."""
    raw = str(value or "").strip()
    fallback = previous or default
    if not raw:
        return fallback, ""
    match = _COMMAND_LETTER_RE.match(raw)
    if not match:
        return fallback, "Hotkey must be Command plus one ANSI letter, such as Cmd+J."
    return f"Cmd+{match.group(1).upper()}", ""

# System prompt for a terminal error analysis assistant. Keep this stable so
# responses remain consistent across providers and model changes.
SYSTEM_PROMPT = """\
You are TermFix, an expert terminal error analysis assistant integrated into iTerm2.

Your sole job: given information about a failed shell command (non-zero exit code),
determine the root cause and provide concrete, safe fix commands.

Always respond in concise Markdown. Do not wrap the whole response in a code
fence. Use this structure:

### Cause
One or two sentences describing the root cause.

### Fix
Use a shell code block for runnable commands when commands are appropriate.
If no command fix applies, say what file or logic should be changed.

### Details
Briefly explain why the failure happened and why the fix works.

Rules:
- Be concise, developer-facing, and direct.
- Prefer runnable shell commands in execution order when they are safe.
- Never suggest `rm -rf` or other destructive commands without explicit, prominent
  safety warnings.
- Prefer non-destructive, idempotent fixes.
- If the error is ambiguous, provide the most likely fix and mention alternatives in
  Details.

Recognized error categories (non-exhaustive):
  • Permission denied → sudo / chmod / chown
  • Command not found → package install (brew, apt, pip, npm, cargo …)
  • No such file or directory → path correction, mkdir, touch
  • Syntax / parse errors → correct the offending flag or expression
  • Missing environment variables → export / .env guidance
  • Network errors (DNS, TLS, timeout) → connectivity / proxy / cert fix
  • Package manager failures (pip, npm, brew, cargo) → cache clean / version pin
  • Git errors (conflicts, auth, missing ref) → targeted git commands
  • Docker errors (daemon, image, volume) → daemon start / pull / prune
  • Virtual-environment / interpreter mismatches → venv activation / nvm use
  • Make / cmake / compiler errors → missing dev headers, wrong toolchain

Respond ONLY with the Markdown answer. No preamble.\
"""
