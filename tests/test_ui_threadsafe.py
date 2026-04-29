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
from termfixlib.monitor import TermFixState  # noqa: E402


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

    def test_analysis_task_start_mutates_task_dict_on_state_loop(self) -> None:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        self.addCleanup(asyncio.set_event_loop, None)
        self.addCleanup(loop.close)

        state = SimpleNamespace(
            loop=loop,
            analysis_tasks={},
            refresh_analyzing=lambda: None,
        )
        entry = SimpleNamespace(
            id="entry-12345678",
            analysis_started=False,
            status="pending",
            result=None,
            updated_at=0,
        )

        async def _run_once(_entry, _state):  # noqa: ANN001
            return None

        with mock.patch.object(
            ui,
            "_run_streaming_analysis",
            side_effect=_run_once,
        ), mock.patch.object(
            loop,
            "call_soon_threadsafe",
            wraps=loop.call_soon_threadsafe,
        ) as call_soon_threadsafe:
            ui._start_analysis_task(entry, state)
            self.assertEqual({}, state.analysis_tasks)
            loop.run_until_complete(asyncio.sleep(0))
            loop.run_until_complete(asyncio.sleep(0))

        call_soon_threadsafe.assert_called_once()
        self.assertTrue(entry.analysis_started)
        self.assertIn(entry.id, state.analysis_tasks)
        self.assertTrue(state.analysis_tasks[entry.id].done())

    def test_status_bar_registration_uses_finite_timeout(self) -> None:
        recorded = {}

        class _StringKnob:
            def __init__(self, **kwargs):  # noqa: ANN003
                self.kwargs = kwargs

        class _StatusBarComponent:
            def __init__(self, **kwargs):  # noqa: ANN003
                self.kwargs = kwargs

            async def async_register(self, connection, status_coro, timeout, onclick):  # noqa: ANN001
                recorded["timeout"] = timeout

        fake_iterm2 = SimpleNamespace(
            StringKnob=_StringKnob,
            StatusBarComponent=_StatusBarComponent,
            StatusBarRPC=lambda func: func,
            RPC=lambda func: func,
        )

        async def _register():
            with mock.patch.object(ui, "iterm2", fake_iterm2), mock.patch.object(
                ui,
                "_ensure_status_server",
                return_value=None,
            ):
                return await ui.register_status_bar(object(), object())

        state = asyncio.run(_register())

        self.assertIsInstance(state, TermFixState)
        self.assertEqual(ui._STATUS_REGISTER_TIMEOUT, recorded["timeout"])

    def test_status_bar_registration_timeout_has_clear_error(self) -> None:
        class _StringKnob:
            def __init__(self, **kwargs):  # noqa: ANN003
                self.kwargs = kwargs

        class _StatusBarComponent:
            def __init__(self, **kwargs):  # noqa: ANN003
                self.kwargs = kwargs

            async def async_register(self, connection, status_coro, timeout, onclick):  # noqa: ANN001
                raise asyncio.TimeoutError()

        fake_iterm2 = SimpleNamespace(
            StringKnob=_StringKnob,
            StatusBarComponent=_StatusBarComponent,
            StatusBarRPC=lambda func: func,
            RPC=lambda func: func,
        )

        async def _register():
            with mock.patch.object(ui, "iterm2", fake_iterm2), mock.patch.object(
                ui,
                "_ensure_status_server",
                return_value=None,
            ):
                return await ui.register_status_bar(object(), object())

        with self.assertRaisesRegex(RuntimeError, "registration timed out"):
            asyncio.run(_register())


if __name__ == "__main__":
    unittest.main()
