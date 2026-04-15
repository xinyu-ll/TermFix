#!/usr/bin/env python3
"""
TermFix — iTerm2 Long-Running Daemon
=====================================
Monitors every shell session for failed commands (exit code ≠ 0).
When a failure is detected, a red status-bar badge appears.
Clicking it calls the Claude API and presents fix suggestions in a popover.

Installation
────────────
1. Copy this entire *termfix/* directory to:
       ~/Library/ApplicationSupport/iTerm2/Scripts/AutoLaunch/termfix/

   The directory should look like:
       termfix/
       ├── termfix.py          ← this file (the daemon entry point)
       ├── monitor.py
       ├── context.py
       ├── llm_client.py
       ├── ui.py
       └── config.py

2. Ensure the required packages are installed for iTerm2's Python runtime:
       ~/.iterm2_venv/bin/pip install anthropic

   Or, if you use a system Python:
       pip3 install anthropic

3. In iTerm2: Scripts ▸ AutoLaunch ▸ termfix/termfix.py
   (or restart iTerm2 — AutoLaunch scripts start automatically)

4. After the script starts, add the "TermFix" component to your status bar:
       iTerm2 ▸ Preferences ▸ Profiles ▸ Session ▸ Configure Status Bar
   Drag "TermFix" into the active components area.

5. Click the component to open its knobs and paste your Anthropic API key.

Prerequisites
─────────────
• iTerm2 ≥ 3.4 with Python API enabled (Preferences ▸ General ▸ Magic)
• Shell Integration installed in each session you want to monitor
  (iTerm2 ▸ Install Shell Integration)
• Python ≥ 3.8 with the `anthropic` package installed

How it works
────────────
                    ┌─────────────────────────────────────────┐
  shell integration │  COMMAND_START ──► capture command text │
  notifications     │  COMMAND_END   ──► check exit code      │
                    └──────────────┬──────────────────────────┘
                                   │ exit_code ≠ 0
                                   ▼
                    ┌──────────────────────────────────┐
                    │  collect_context()               │
                    │  (terminal output, cwd, shell)   │
                    └──────────────┬───────────────────┘
                                   │
                                   ▼
                    ┌──────────────────────────────────┐
                    │  TermFixState.add_error()        │
                    │  badge counter ++                │
                    │  status bar → "🔴 Fix (N)"       │
                    └──────────────────────────────────┘
                                   │ user clicks
                                   ▼
                    ┌──────────────────────────────────┐
                    │  analyze_error()  [lazy, cached] │
                    │  Claude API (streaming + cache)  │
                    └──────────────┬───────────────────┘
                                   │
                                   ▼
                    ┌──────────────────────────────────┐
                    │  async_open_popover()            │
                    │  cause / fix commands / explain  │
                    └──────────────────────────────────┘
"""

from __future__ import annotations

import logging
import os
import sys

# ── Make sure sibling modules are importable ──────────────────────────────
# When iTerm2 runs this file as an AutoLaunch script the CWD is not
# guaranteed to be the script directory, so we add the directory explicitly.
_script_dir = os.path.dirname(os.path.abspath(__file__))
if _script_dir not in sys.path:
    sys.path.insert(0, _script_dir)

import iterm2

from monitor import start_monitoring
from ui import register_status_bar

# ── Logging ────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(name)s: %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("termfix")


# ── Entry point ────────────────────────────────────────────────────────────

async def main(connection: iterm2.Connection) -> None:
    """Called once by iTerm2 when the daemon starts.

    Registers the status bar component and then enters the monitoring loop.
    This coroutine runs forever — iterm2.run_forever() keeps the event loop
    alive.
    """
    logger.info("TermFix starting…")

    app = await iterm2.async_get_app(connection)

    # 1. Register the StatusBar component and get shared state
    state = await register_status_bar(connection, app)

    logger.info("Status bar component registered — entering monitor loop")

    # 2. Start monitoring (blocks forever via asyncio.gather)
    await start_monitoring(connection, app, state)


# ── Run ────────────────────────────────────────────────────────────────────
iterm2.run_forever(main)
