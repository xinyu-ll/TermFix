import asyncio
import json
import sys
import threading
import types
import urllib.request
import unittest
from unittest import mock

sys.modules.setdefault("iterm2", types.ModuleType("iterm2"))

from termfixlib import monitor
from termfixlib.monitor import ErrorEntry, TermFixState
from termfixlib.ui import _ensure_status_server, _entry_payload, _pick_entry, _status_endpoint


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

    def test_picker_ignores_handled_entries_and_prefers_clicked_session(self):
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

        self.assertIsNone(_pick_entry(state, "session-a"))
        self.assertIsNone(_pick_entry(state, None))

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
        self.assertNotIn(pending.id, state.popover_last_seen)
        self.assertNotIn(pending.id, state.popover_close_requests)
