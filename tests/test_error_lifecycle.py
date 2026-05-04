import asyncio
import json
import sys
import threading
import types
import urllib.request
import unittest
from unittest import mock

sys.modules.setdefault("iterm2", types.ModuleType("iterm2"))

from termfixlib import monitor, ui
from termfixlib.monitor import ErrorEntry, PromptEntry, TermFixState
from termfixlib.ui import (
    _cancel_analysis,
    _dismiss_error,
    _ensure_status_server,
    _entry_payload,
    _entry_payload_on_loop,
    _mark_popover_closed,
    _pick_entry,
    _retry_analysis,
    _status_endpoint,
)


def _entry(session_id: str, command: str, handled: bool = False) -> ErrorEntry:
    return ErrorEntry(
        session_id=session_id,
        command=command,
        exit_code=1,
        context={},
        handled=handled,
    )


def _read_json(url: str, method: str = "GET") -> dict:
    request = urllib.request.Request(url, method=method)
    with urllib.request.urlopen(request, timeout=2) as response:
        return json.loads(response.read().decode("utf-8"))


def _stop_status_server(state: TermFixState) -> None:
    if state.status_server is None:
        return
    state.status_server.shutdown()
    state.status_server.server_close()
    state.status_server = None
    state.status_server_url = ""


class ErrorLifecycleTest(unittest.TestCase):
    def setUp(self) -> None:
        self._main_loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._main_loop)
        self._old_error_retention_limit = monitor.ERROR_RETENTION_LIMIT
        self._old_handled_error_retention_limit = monitor.HANDLED_ERROR_RETENTION_LIMIT

    def tearDown(self) -> None:
        monitor.ERROR_RETENTION_LIMIT = self._old_error_retention_limit
        monitor.HANDLED_ERROR_RETENTION_LIMIT = self._old_handled_error_retention_limit
        asyncio.set_event_loop(None)
        self._main_loop.close()

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

    def test_error_count_tracks_unhandled_entries_separately_from_total(self):
        state = TermFixState()
        handled = _entry("session-a", "false", handled=True)
        pending = _entry("session-a", "make test")
        state.errors.extend([handled, pending])

        self.assertEqual(state.total_error_count, 2)
        self.assertEqual(state.unhandled_error_count, 1)
        self.assertEqual(state.error_count, 1)

        self.assertTrue(state.mark_error_handled(pending.id))
        self.assertEqual(state.total_error_count, 2)
        self.assertEqual(state.unhandled_error_count, 0)
        self.assertEqual(state.error_count, 0)
        self.assertTrue(pending.handled)
        self.assertIsNotNone(pending.handled_at)

    def test_add_error_prunes_handled_entries_before_unhandled_entries(self):
        monitor.ERROR_RETENTION_LIMIT = 4
        monitor.HANDLED_ERROR_RETENTION_LIMIT = 10
        state = TermFixState()
        handled_old = _entry("session-a", "handled-old", handled=True)
        handled_old.context = {"terminal_output": "old secret context"}
        handled_old.result = "old result"
        entries = [
            handled_old,
            _entry("session-a", "unhandled-old"),
            _entry("session-a", "handled-new", handled=True),
            _entry("session-a", "unhandled-new"),
            _entry("session-a", "latest-unhandled"),
        ]

        for entry in entries:
            self._main_loop.run_until_complete(state.add_error(entry))

        self.assertIsNone(state.get_error(handled_old.id))
        self.assertEqual(
            [entry.command for entry in state.errors],
            ["unhandled-old", "handled-new", "unhandled-new", "latest-unhandled"],
        )
        self.assertEqual(state.latest_unhandled_error().command, "latest-unhandled")

    def test_add_error_bounds_handled_history_below_total_limit(self):
        monitor.ERROR_RETENTION_LIMIT = 10
        monitor.HANDLED_ERROR_RETENTION_LIMIT = 2
        state = TermFixState()
        entries = [
            _entry("session-a", "handled-0", handled=True),
            _entry("session-a", "handled-1", handled=True),
            _entry("session-a", "pending"),
            _entry("session-a", "handled-2", handled=True),
            _entry("session-a", "handled-3", handled=True),
        ]

        for entry in entries:
            self._main_loop.run_until_complete(state.add_error(entry))

        self.assertEqual(
            [entry.command for entry in state.errors],
            ["pending", "handled-2", "handled-3"],
        )
        self.assertEqual(state.latest_unhandled_error().command, "pending")

    def test_mark_error_handled_prunes_old_handled_entries(self):
        monitor.ERROR_RETENTION_LIMIT = 10
        monitor.HANDLED_ERROR_RETENTION_LIMIT = 1
        state = TermFixState()
        handled_old = _entry("session-a", "handled-old", handled=True)
        pending = _entry("session-a", "pending")
        state.errors.extend([handled_old, pending])

        self.assertTrue(state.mark_error_handled(pending.id))

        self.assertIsNone(state.get_error(handled_old.id))
        self.assertIs(state.get_error(pending.id), pending)
        self.assertEqual(state.unhandled_error_count, 0)

    def test_mark_error_handled_is_thread_safe(self):
        state = TermFixState()
        pending = _entry("session-a", "pending")
        state.errors.append(pending)
        started = threading.Barrier(12)
        results = []
        exceptions = []

        def mark_handled() -> None:
            try:
                started.wait(timeout=1)
                results.append(state.mark_error_handled(pending.id))
            except Exception as exc:  # pragma: no cover - assertion reports details.
                exceptions.append(exc)

        threads = [threading.Thread(target=mark_handled) for _ in range(12)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=1)

        self.assertEqual([], exceptions)
        self.assertEqual(1, results.count(True))
        self.assertEqual(11, results.count(False))
        self.assertTrue(pending.handled)

    def test_popover_close_request_is_consumed_once_across_threads(self):
        state = TermFixState()
        entry_id = "entry-1"
        state.request_popover_close(entry_id)
        started = threading.Barrier(12)
        results = []
        exceptions = []

        def consume_request() -> None:
            try:
                started.wait(timeout=1)
                results.append(state.consume_popover_close_request(entry_id))
            except Exception as exc:  # pragma: no cover - assertion reports details.
                exceptions.append(exc)

        threads = [threading.Thread(target=consume_request) for _ in range(12)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=1)

        self.assertEqual([], exceptions)
        self.assertEqual(1, results.count(True))
        self.assertEqual(11, results.count(False))
        self.assertNotIn(entry_id, state.popover_close_requests)

    def test_picker_prefers_unhandled_entries_before_last_viewed_result(self):
        state = TermFixState()
        state.errors.extend(
            [
                _entry("session-a", "old", handled=True),
                _entry("session-a", "session-pending"),
                _entry("session-b", "global-pending"),
                _entry("session-a", "latest-handled", handled=True),
            ]
        )

        self.assertEqual(_pick_entry(state, "session-a").command, "session-pending")
        self.assertEqual(_pick_entry(state, "session-b").command, "global-pending")

        state.mark_error_handled(state.errors[1].id)
        self.assertEqual(_pick_entry(state, "session-a").command, "global-pending")

        state.mark_error_handled(state.errors[2].id)

        self.assertEqual(_pick_entry(state, "session-a").command, "global-pending")
        self.assertEqual(_pick_entry(state, None).command, "global-pending")

    def test_closed_viewed_error_can_be_reopened_until_new_error_arrives(self):
        state = TermFixState()
        viewed = _entry("session-a", "pytest")
        viewed.result = "Try the focused assertion."
        viewed.status = "done"
        state.errors.append(viewed)

        payload = _entry_payload(state, viewed.id)

        self.assertTrue(payload["ok"])
        self.assertTrue(viewed.handled)
        self.assertEqual(_pick_entry(state, "session-a"), viewed)

        fresh = _entry("session-a", "make test")
        state.errors.append(fresh)

        self.assertEqual(_pick_entry(state, "session-a"), fresh)

    def test_cancel_analysis_cancels_prompt_task_and_preserves_partial_result(self):
        state = TermFixState()
        prompt = PromptEntry(session_id="session-a", context={})
        prompt.status = "streaming"
        prompt.result = "Partial response"
        state.prompts.append(prompt)

        async def never_finishes() -> None:
            await asyncio.Event().wait()

        async def run() -> dict:
            task = asyncio.create_task(never_finishes())
            state.analysis_tasks[prompt.id] = task
            payload = await _cancel_analysis(prompt.id, state)
            await asyncio.sleep(0)
            self.assertTrue(task.cancelled())
            return payload

        payload = self._main_loop.run_until_complete(run())

        self.assertEqual(payload["ok"], True)
        self.assertEqual(payload["cancelled"], True)
        self.assertEqual(prompt.status, "cancelled")
        self.assertEqual(prompt.result, "Partial response")

    def test_retry_analysis_resets_failed_error_and_starts_new_task(self):
        state = TermFixState()
        state.loop = self._main_loop
        failed = _entry("session-a", "pytest")
        failed.status = "error"
        failed.result = "Network error."
        failed.analysis_started = True
        state.errors.append(failed)
        notifications = []

        async def notify_ui_update() -> None:
            notifications.append(True)

        async def fake_run_streaming_analysis(entry, state):  # noqa: ANN001
            entry.result = "Retry succeeded."
            entry.status = "done"
            state.analysis_tasks.pop(entry.id, None)

        state.notify_ui_update = notify_ui_update
        with mock.patch.object(
            ui,
            "_run_streaming_analysis",
            side_effect=fake_run_streaming_analysis,
        ):
            payload = self._main_loop.run_until_complete(
                _retry_analysis(failed.id, state)
            )
            self._main_loop.run_until_complete(asyncio.sleep(0))

        self.assertEqual(payload, {"ok": True, "status": "streaming"})
        self.assertEqual(failed.result, "Retry succeeded.")
        self.assertEqual(failed.status, "done")
        self.assertTrue(failed.analysis_started)
        self.assertEqual(notifications, [True])

    def test_dismiss_error_marks_handled_and_requests_popover_close(self):
        state = TermFixState()
        pending = _entry("session-a", "pytest")
        pending.status = "streaming"
        state.errors.append(pending)
        notifications = []

        async def notify_ui_update() -> None:
            notifications.append(True)

        state.notify_ui_update = notify_ui_update

        payload = self._main_loop.run_until_complete(_dismiss_error(pending.id, state))

        self.assertEqual(payload, {"ok": True, "handled": True})
        self.assertTrue(pending.handled)
        self.assertIn(pending.id, state.popover_close_requests)
        self.assertEqual(notifications, [True])

    def test_prompt_event_info_log_does_not_include_raw_payload(self):
        secret_payload = "deploy --token=secret"

        with self.assertLogs("termfixlib.monitor", level="INFO") as captured:
            monitor._log_prompt_event(
                "session-a",
                types.SimpleNamespace(name="COMMAND_START"),
                secret_payload,
            )

        output = "\n".join(captured.output)
        self.assertIn("session=session-a", output)
        self.assertIn("payload_type=str", output)
        self.assertNotIn(secret_payload, output)

    def test_shell_integration_missing_flag_is_session_scoped(self):
        state = TermFixState()

        self.assertTrue(state.mark_shell_integration_missing("session-a"))
        self.assertFalse(state.mark_shell_integration_missing("session-a"))
        self.assertTrue(state.shell_integration_missing)
        self.assertIn("session-a", state.shell_integration_missing_sessions)

        self.assertTrue(state.clear_shell_integration_missing("session-a"))
        self.assertFalse(state.clear_shell_integration_missing("session-a"))
        self.assertFalse(state.shell_integration_missing)

    def test_session_worker_warns_then_clears_when_command_start_arrives(self):
        state = TermFixState()
        notifications = []

        async def notify_ui_update() -> None:
            notifications.append(True)

        state.notify_ui_update = notify_ui_update

        class _Mode:
            COMMAND_START = "start"
            COMMAND_END = "end"

        class _PromptMonitor:
            Mode = _Mode

            def __init__(self, connection, session_id, modes):  # noqa: ANN001
                self.calls = 0

            async def __aenter__(self):
                return self

            async def __aexit__(self, exc_type, exc, traceback):  # noqa: ANN001
                return False

            async def async_get(self):
                self.calls += 1
                if self.calls == 1:
                    await asyncio.sleep(1)
                if self.calls == 2:
                    return (_Mode.COMMAND_START, "echo ready")
                await asyncio.Event().wait()

        fake_iterm2 = types.SimpleNamespace(PromptMonitor=_PromptMonitor)
        session = types.SimpleNamespace(session_id="session-a")

        async def run_worker() -> None:
            with mock.patch.object(monitor, "iterm2", fake_iterm2), mock.patch.object(
                monitor,
                "SHELL_INTEGRATION_START_TIMEOUT",
                0.01,
            ):
                task = asyncio.create_task(
                    monitor._session_worker(object(), session, state)
                )
                await asyncio.sleep(0.05)
                task.cancel()
                await task

        self._main_loop.run_until_complete(run_worker())

        self.assertFalse(state.shell_integration_missing)
        self.assertEqual(notifications, [True, True])

    def test_handle_error_info_log_does_not_include_raw_command(self):
        state = TermFixState()
        session = types.SimpleNamespace(session_id="session-a")
        secret_command = "deploy --token=secret"

        with mock.patch.object(
            monitor,
            "collect_context",
            new=mock.AsyncMock(return_value={}),
        ):
            with self.assertLogs("termfixlib.monitor", level="INFO") as captured:
                self._main_loop.run_until_complete(
                    monitor._handle_error(object(), session, state, secret_command, 42)
                )

        output = "\n".join(captured.output)
        self.assertIn("session=session-a", output)
        self.assertIn("exit=42", output)
        self.assertNotIn(secret_command, output)
        self.assertEqual(state.errors[0].command, secret_command)

    def test_state_payload_marks_error_handled_without_close_beacon(self):
        state = TermFixState()
        pending = _entry("session-a", "make test")
        pending.result = "Try fixing the failing assertion."
        pending.status = "done"
        state.errors.append(pending)

        payload = _entry_payload(state, pending.id)

        self.assertTrue(payload["ok"])
        self.assertEqual(payload["entry_id"], pending.id)
        self.assertEqual(state.unhandled_error_count, 0)
        self.assertTrue(pending.handled)
        self.assertIsNotNone(pending.handled_at)

    def test_state_payload_marks_error_retry_available_only_for_error_status(self):
        state = TermFixState()
        failed = _entry("session-a", "pytest")
        failed.status = "error"
        failed.result = "Network error."
        done = _entry("session-a", "make test")
        done.status = "done"
        done.result = "Already analyzed."
        state.errors.extend([failed, done])

        self.assertTrue(_entry_payload(state, failed.id)["can_retry"])
        self.assertFalse(_entry_payload(state, done.id)["can_retry"])

    def test_state_payload_includes_terminal_context_label(self):
        state = TermFixState()
        pending = _entry("session-a", "git push")
        pending.context = {
            "terminal_output_line_count": 50,
            "shell": "zsh",
            "os_name": "macOS",
            "os_version": "14.5",
        }
        state.errors.append(pending)

        payload = _entry_payload(state, pending.id)

        self.assertEqual(payload["context_label"], "50 lines context · zsh · macOS 14.5")

    def test_state_payload_includes_error_inbox_with_active_entry(self):
        state = TermFixState()
        handled = _entry("session-a", "old failure", handled=True)
        handled.result = "Old result"
        handled.status = "done"
        other = _entry("session-b", "npm test")
        other.result = "Other result"
        other.status = "done"
        active = _entry("session-a", "pytest tests/test_error_lifecycle.py")
        active.result = "Active result"
        active.status = "done"
        state.errors.extend([handled, other, active])

        payload = _entry_payload(state, active.id)

        self.assertTrue(payload["ok"])
        self.assertEqual(payload["entry_id"], active.id)
        self.assertEqual(payload["command"], "pytest tests/test_error_lifecycle.py")
        self.assertEqual(payload["exit_code"], 1)
        self.assertEqual(
            [item["id"] for item in payload["errors"]],
            [active.id, other.id, handled.id],
        )
        active_items = [item for item in payload["errors"] if item["active"]]
        self.assertEqual(len(active_items), 1)
        self.assertEqual(active_items[0]["id"], active.id)
        self.assertTrue(active_items[0]["handled"])
        self.assertFalse(payload["errors"][1]["handled"])

    def test_status_server_state_can_switch_error_entries(self):
        state = TermFixState()
        state.loop = self._start_loop()
        first = _entry("session-a", "pytest")
        first.result = "First focused fix."
        first.status = "done"
        second = _entry("session-a", "npm test")
        second.result = "Second focused fix."
        second.status = "done"
        state.errors.extend([first, second])
        _ensure_status_server(state)
        try:
            first_payload = _read_json(_status_endpoint(state, "/state", {"entry": first.id}))
            second_payload = _read_json(_status_endpoint(state, "/state", {"entry": second.id}))
        finally:
            _stop_status_server(state)

        self.assertTrue(first_payload["ok"])
        self.assertEqual(first_payload["entry_id"], first.id)
        self.assertIn("First focused fix.", first_payload["body_html"])
        self.assertTrue(second_payload["ok"])
        self.assertEqual(second_payload["entry_id"], second.id)
        self.assertIn("Second focused fix.", second_payload["body_html"])
        self.assertEqual(
            [item["id"] for item in second_payload["errors"] if item["active"]],
            [second.id],
        )

    def test_status_server_state_poll_marks_error_handled(self):
        state = TermFixState()
        state.loop = self._start_loop()
        pending = _entry("session-a", "pytest")
        state.errors.append(pending)
        _ensure_status_server(state)
        try:
            payload = _read_json(_status_endpoint(state, "/state", {"entry": pending.id}))
        finally:
            _stop_status_server(state)

        self.assertTrue(payload["ok"])
        self.assertEqual(payload["entry_id"], pending.id)
        self.assertEqual(state.unhandled_error_count, 0)
        self.assertTrue(pending.handled)

    def test_status_server_closed_preserves_cleanup_and_marks_handled(self):
        state = TermFixState()
        state.loop = self._start_loop()
        pending = _entry("session-a", "pytest")
        state.errors.append(pending)
        state.mark_popover_seen(pending.id)
        state.request_popover_close(pending.id)
        _ensure_status_server(state)
        try:
            payload = _read_json(
                _status_endpoint(state, "/closed", {"entry": pending.id}),
                "POST",
            )
        finally:
            _stop_status_server(state)

        self.assertEqual(payload, {"ok": True})
        self.assertTrue(pending.handled)
        self.assertEqual(_pick_entry(state, "session-a"), pending)
        self.assertNotIn(pending.id, state.popover_last_seen)
        self.assertNotIn(pending.id, state.popover_close_requests)

    def test_entry_payload_on_loop_marks_handled_without_thread_scheduler(self):
        state = TermFixState()
        pending = _entry("session-a", "pytest")
        pending.result = "Focused fix."
        pending.status = "done"
        state.errors.append(pending)
        notifications = []

        async def notify_ui_update() -> None:
            notifications.append(True)

        state.notify_ui_update = notify_ui_update

        with mock.patch.object(
            ui,
            "_notify_ui_update_from_thread",
            side_effect=AssertionError("loop path should not schedule from thread"),
        ):
            payload = self._main_loop.run_until_complete(
                _entry_payload_on_loop(pending.id, state)
            )

        self.assertTrue(payload["ok"])
        self.assertTrue(pending.handled)
        self.assertEqual(notifications, [True])

    def test_mark_popover_closed_on_loop_marks_handled_without_thread_scheduler(self):
        state = TermFixState()
        pending = _entry("session-a", "pytest")
        state.errors.append(pending)
        state.mark_popover_seen(pending.id)
        notifications = []

        async def notify_ui_update() -> None:
            notifications.append(True)

        state.notify_ui_update = notify_ui_update

        with mock.patch.object(
            ui,
            "_notify_ui_update_from_thread",
            side_effect=AssertionError("loop path should not schedule from thread"),
        ):
            payload = self._main_loop.run_until_complete(
                _mark_popover_closed(pending.id, state)
            )

        self.assertEqual(payload, {"ok": True})
        self.assertTrue(pending.handled)
        self.assertEqual(notifications, [True])
        self.assertNotIn(pending.id, state.popover_last_seen)
