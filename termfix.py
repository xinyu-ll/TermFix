#!/usr/bin/env python3
"""
TermFix — iTerm2 Long-Running Daemon
=====================================
Monitors every shell session for failed commands (exit code ≠ 0).
When a failure is detected, a red status-bar badge appears.
Clicking it calls an OpenAI-compatible API and presents fix suggestions in a
popover.

Installation
────────────
1. Copy this file to:
       ~/Library/Application Support/iTerm2/Scripts/AutoLaunch/termfix.py

2. Copy the support package to:
       ~/Library/Application Support/iTerm2/Scripts/termfixlib/

   The directory structure should look like:
       Scripts/
       ├── AutoLaunch/
       │   └── termfix.py
       └── termfixlib/
           ├── __init__.py
           ├── config.py
           ├── context.py
           ├── llm_client.py
           ├── monitor.py
           └── ui.py

3. Restart iTerm2.

4. After the script starts, add the "TermFix" component to your status bar:
       iTerm2 ▸ Preferences ▸ Profiles ▸ Session ▸ Configure Status Bar
   Drag "TermFix" into the active components area.

5. Click the component to open its knobs and configure Base URL / API Key / Model.

Prerequisites
─────────────
• iTerm2 ≥ 3.4 with Python API enabled (Preferences ▸ General ▸ Magic)
• Shell Integration installed in each session you want to monitor
  (iTerm2 ▸ Install Shell Integration)
• Python ≥ 3.8 in iTerm2's script runtime
"""

from __future__ import annotations

import asyncio
import logging
import os
import sys

# When iTerm2 runs this file from AutoLaunch, helper modules live one
# directory up under Scripts/termfixlib.
_script_dir = os.path.dirname(os.path.abspath(__file__))
_scripts_root = os.path.dirname(_script_dir)
for path in (_script_dir, _scripts_root):
    if path not in sys.path:
        sys.path.insert(0, path)

import iterm2

from termfixlib.monitor import start_monitoring
from termfixlib.ui import register_status_bar, start_hotkey_listener

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(name)s: %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("termfix")


async def main(connection: iterm2.Connection) -> None:
    """Register the status bar component and enter the monitoring loop."""
    logger.info("TermFix starting")

    app = await iterm2.async_get_app(connection)
    state = await register_status_bar(connection, app)

    logger.info("Status bar component registered — entering monitor loop")
    await asyncio.gather(
        start_monitoring(connection, app, state),
        start_hotkey_listener(connection, app, state),
    )


iterm2.run_forever(main, retry=True)
