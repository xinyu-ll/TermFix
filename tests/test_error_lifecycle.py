import asyncio
import json
import sys
import threading
import types
import urllib.request
import unittest

sys.modules.setdefault("iterm2", types.ModuleType("iterm2"))

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
