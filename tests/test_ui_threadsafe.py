from __future__ import annotations

import asyncio
import logging
import sys
import threading
import types
import unittest
from types import SimpleNamespace
from unittest import mock


def _install_iterm2_stub() -> None:
    if "iterm2" in sys.modules:
        return

    iterm2 = types.ModuleType("iterm2")

    class _Dummy:
        pass

    class _KeystrokePattern:
        def __init__(self) -> None:
            self.required_modifiers = []
            self.forbidden_modifiers = []
            self.keycodes = []

    class _Modifier:
        COMMAND = object()
        CONTROL = object()
        OPTION = object()
        SHIFT = object()

    class _Keycode:
        ANSI_J = object()
        ANSI_L = object()

    def _identity_decorator(func=None, *args, **kwargs):  # noqa: ANN001
        if func is None:
            return lambda inner: inner
        return func

    async def _async_open_popover(*args, **kwargs):  # noqa: ANN001
        return None

    iterm2.Connection = _Dummy
    iterm2.App = _Dummy
    iterm2.Session = _Dummy
    iterm2.StatusBarComponent = _Dummy
    iterm2.StringKnob = _Dummy
    iterm2.StatusBarRPC = _identity_decorator
    iterm2.RPC = _identity_decorator
    iterm2.KeystrokePattern = _KeystrokePattern
    iterm2.Modifier = _Modifier
    iterm2.Keycode = _Keycode
    iterm2.Size = _Dummy
    iterm2.async_open_popover = _async_open_popover
    sys.modules["iterm2"] = iterm2


_install_iterm2_stub()

from termfixlib import ui  # noqa: E402


class StateLoopThreadsafeTests(unittest.TestCase):
    def setUp(self) -> None:
        self._old_timeout = ui._STATE_LOOP_CALL_TIMEOUT
        self._old_logging_disable = logging.root.manager.disable
        ui._STATE_LOOP_CALL_TIMEOUT = 0.2
        logging.disable(logging.CRITICAL)

    def tearDown(self) -> None:
        ui._STATE_LOOP_CALL_TIMEOUT = self._old_timeout
        logging.disable(self._old_logging_disable)

    def _start_loop(self):
        loop = asyncio.new_event_loop()
        ready = threading.Event()

        def run_loop() -> None:
            asyncio.set_event_loop(loop)
            ready.set()
            loop.run_forever()

        thread = threading.Thread(target=run_loop, daemon=True)
        thread.start()
        self.assertTrue(ready.wait(timeout=1))

        def cleanup() -> None:
            loop.call_soon_threadsafe(loop.stop)
            thread.join(timeout=1)
            loop.close()

        self.addCleanup(cleanup)
        return loop

    def test_timeout_cancels_scheduled_coroutine(self) -> None:
        loop = self._start_loop()
        state = SimpleNamespace(loop=loop)
        started = threading.Event()
        cancelled = threading.Event()
        mutated_after_timeout = []
        result_holder = {}

        async def slow_state_call() -> dict:
            started.set()
            try:
                await asyncio.sleep(10)
            except asyncio.CancelledError:
                cancelled.set()
                raise
            mutated_after_timeout.append(True)
            return {"ok": True}

        def call_helper() -> None:
            result_holder["result"] = ui._call_state_loop_from_thread(
                state,
                slow_state_call,
                "Slow state call",
            )

        helper_thread = threading.Thread(target=call_helper)
        helper_thread.start()
        self.assertTrue(started.wait(timeout=1))
        helper_thread.join(timeout=1)

        self.assertEqual({"ok": False, "error": ""}, result_holder["result"])
        self.assertTrue(cancelled.wait(timeout=1))
        self.assertEqual([], mutated_after_timeout)

    def test_scheduling_error_returns_error_payload_and_closes_coroutine(self) -> None:
        state = SimpleNamespace(loop=object())
        closed = []

        class CloseAwareAwaitable:
            def __await__(self):
                yield
                return {"ok": True}

            def close(self) -> None:
                closed.append(True)

        with mock.patch.object(
            ui.asyncio,
            "run_coroutine_threadsafe",
            side_effect=RuntimeError("Event loop is closed"),
        ):
            result = ui._call_state_loop_from_thread(
                state,
                CloseAwareAwaitable,
                "Closed loop call",
            )

        self.assertEqual(
            {"ok": False, "error": "Event loop is closed"},
            result,
        )
        self.assertEqual([True], closed)


if __name__ == "__main__":
    unittest.main()
