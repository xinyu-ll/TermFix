"""
StatusBar component and popover UI for TermFix.

Registers a persistent status bar component (identifier: com.termfix.status)
with three user-configurable knobs:
  • API Key       — Anthropic API key
  • Model         — Claude model ID (default: claude-opus-4-6)
  • Context Lines — how many terminal lines to capture (default: 50)

Display logic:
  • No errors  → "✅"
  • N errors   → "🔴 Fix (N)"  +  red badge with count N

Click behaviour:
  1. Find the latest error for the clicked session (falls back to the global
     latest if no session-specific error is found).
  2. If the error has not yet been analysed, call the LLM (results are
     cached on the ErrorEntry so repeated clicks are instant).
  3. Open an HTML popover (450×350) with cause / fix commands / explanation.
"""

from __future__ import annotations

import asyncio
import html
import logging
from typing import Optional

import iterm2

from config import (
    DEFAULT_CONTEXT_LINES,
    DEFAULT_MODEL,
    POPOVER_HEIGHT,
    POPOVER_WIDTH,
    STATUS_ERROR_FMT,
    STATUS_IDENTIFIER,
    STATUS_NORMAL,
)
from llm_client import analyze_error

logger = logging.getLogger(__name__)


# ── Public API ─────────────────────────────────────────────────────────────

async def register_status_bar(
    connection: iterm2.Connection,
    app: iterm2.App,
) -> "TermFixState":  # forward ref — monitor imports this module too
    """Create and register the StatusBar component; return shared state."""
    # Import here to avoid circular import (monitor ↔ ui ↔ monitor)
    from monitor import TermFixState

    state = TermFixState()
    state.connection = connection

    knobs = [
        iterm2.StringKnob(
            name="API Key",
            placeholder="sk-ant-…",
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
            "Analyses failed commands and shows Claude-powered fix suggestions."
        ),
        knobs=knobs,
        icons=[],
        identifier=STATUS_IDENTIFIER,
        exemplar=STATUS_NORMAL,
    )

    state.component = component

    # ── Coro: returns the string shown in the status bar ───────────────────
    @iterm2.RPC
    async def _status_coro(knobs, session_id=None):
        # Sync knob values into shared state every render cycle
        _sync_knobs(state, knobs)
        if state.error_count == 0:
            return STATUS_NORMAL
        return STATUS_ERROR_FMT.format(count=state.error_count)

    # ── Click handler ──────────────────────────────────────────────────────
    @iterm2.RPC
    async def _on_click(session_id):
        asyncio.create_task(
            _handle_click(connection, session_id, state),
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


# ── Click handler implementation ───────────────────────────────────────────

async def _handle_click(
    connection: iterm2.Connection,
    session_id: Optional[str],
    state: "TermFixState",
) -> None:
    """Analyse the error (if needed) and open the popover."""
    from monitor import ErrorEntry  # avoid circular at module level

    entry = _pick_entry(state, session_id)
    if entry is None:
        # No errors recorded yet — nothing to show
        return

    # Analyse lazily: call Claude only on the first click for this entry
    if not entry.analyzed:
        entry.analyzed = True  # guard against concurrent double-clicks
        try:
            entry.result = await analyze_error(
                entry.context,
                api_key=state.api_key,
                model=state.model,
            )
        except Exception as exc:
            logger.error("LLM analysis failed: %s", exc)
            entry.result = {
                "cause": f"Analysis error: {exc}",
                "fix_commands": [],
                "explanation": str(exc),
            }

    result = entry.result or {
        "cause": "No result available.",
        "fix_commands": [],
        "explanation": "",
    }

    html_content = _build_html(entry, result)

    # Open the popover in the clicked session (or first available session)
    target_session_id = session_id or entry.session_id
    try:
        await iterm2.async_open_popover(
            connection=connection,
            session_id=target_session_id,
            html=html_content,
            size=iterm2.Size(POPOVER_WIDTH, POPOVER_HEIGHT),
        )
        # Remove only the entry that was just shown; other pending errors stay
        # in the queue so the badge count remains accurate.
        await state.remove_error(entry)
        await state.notify_ui_update()
    except Exception as exc:
        logger.error("Failed to open popover: %s", exc)


def _pick_entry(state: "TermFixState", session_id: Optional[str]):
    """Prefer the latest error from the clicked session; fall back globally."""
    if session_id:
        for entry in reversed(state.errors):
            if entry.session_id == session_id:
                return entry
    return state.latest_error()


# ── HTML builder ───────────────────────────────────────────────────────────

def _build_html(entry, result: dict) -> str:
    cause = html.escape(result.get("cause", ""))
    explanation = html.escape(result.get("explanation", ""))
    commands = result.get("fix_commands", [])

    # Build command list HTML
    if commands:
        cmd_items = "".join(
            f'<div class="cmd">$ {html.escape(c)}</div>' for c in commands
        )
        commands_section = f'<div class="code-block">{cmd_items}</div>'
    else:
        commands_section = '<p class="muted">No specific fix commands — see explanation.</p>'

    failed_cmd = html.escape(entry.command or "(unknown command)")
    exit_code = entry.exit_code

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
    .section {{ margin-bottom: 12px; }}
    .label {{
      font-size: 10px;
      font-weight: 700;
      letter-spacing: .05em;
      text-transform: uppercase;
      color: #8e8e93;
      margin-bottom: 4px;
    }}
    .cause {{ color: #c0392b; font-weight: 500; }}
    .code-block {{
      background: #1c1c1e;
      border-radius: 6px;
      padding: 8px 12px;
      overflow-x: auto;
    }}
    .cmd {{
      font-family: "Menlo", "Monaco", "Courier New", monospace;
      font-size: 12px;
      color: #30d158;
      margin-bottom: 3px;
    }}
    .cmd:last-child {{ margin-bottom: 0; }}
    .explanation {{ color: #3c3c43; }}
    .muted {{ color: #aeaeb2; font-style: italic; }}
    .failed-cmd {{
      font-family: "Menlo", monospace;
      font-size: 11px;
      color: #636366;
      background: #f2f2f7;
      border-radius: 4px;
      padding: 1px 6px;
    }}
  </style>
</head>
<body>
  <header>
    <h1>🔍 TermFix</h1>
    <span class="failed-cmd">{failed_cmd}</span>
    <span class="badge">exit {exit_code}</span>
  </header>

  <div class="section">
    <div class="label">Cause</div>
    <p class="cause">{cause}</p>
  </div>

  <div class="section">
    <div class="label">Fix Commands</div>
    {commands_section}
  </div>

  <div class="section">
    <div class="label">Explanation</div>
    <p class="explanation">{explanation}</p>
  </div>
</body>
</html>"""


# ── Knob sync ──────────────────────────────────────────────────────────────

def _sync_knobs(state: "TermFixState", knobs: dict) -> None:
    """Push current knob values into shared state."""
    if not isinstance(knobs, dict):
        return
    # Always write api_key so the user can clear/rotate it; an empty string
    # will surface as a "no API key" error on the next click, which is correct.
    state.api_key = knobs.get("api_key", "").strip()

    model = knobs.get("model", "").strip()
    if model:
        state.model = model

    ctx_raw = knobs.get("context_lines", "").strip()
    if ctx_raw.isdigit():
        state.context_lines = int(ctx_raw)
