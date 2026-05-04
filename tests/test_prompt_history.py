from __future__ import annotations

import asyncio
import json
from pathlib import Path
import sys
import tempfile
import types
import unittest
from unittest import mock


def _install_iterm2_stub() -> None:
    if "iterm2" in sys.modules:
        return

    stub = types.ModuleType("iterm2")

    class _Dummy:
        pass

    class _Modifier:
        COMMAND = "command"
        CONTROL = "control"
        OPTION = "option"
        SHIFT = "shift"

    class _Keycode:
        ANSI_J = "j"
        ANSI_L = "l"

    class _Keystroke:
        class Action:
            NA = "na"
            KEY_DOWN = "key_down"

    stub.App = _Dummy
    stub.Connection = _Dummy
    stub.Session = _Dummy
    stub.Size = _Dummy
    stub.StatusBarComponent = _Dummy
    stub.KeystrokePattern = _Dummy
    stub.Modifier = _Modifier
    stub.Keycode = _Keycode
    stub.Keystroke = _Keystroke
    stub.StatusBarRPC = lambda func: func
    stub.RPC = lambda func: func
    stub.async_open_popover = None
    sys.modules["iterm2"] = stub


_install_iterm2_stub()

from termfixlib import monitor, ui  # noqa: E402
from termfixlib.monitor import PromptEntry, TermFixState  # noqa: E402


class PromptHistoryTests(unittest.TestCase):
    def setUp(self) -> None:
        self._main_loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._main_loop)
        self._old_history_path = monitor.PROMPT_HISTORY_PATH
        self._old_history_limit = monitor.PROMPT_HISTORY_LIMIT
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp_path = Path(self._tmp.name)

    def tearDown(self) -> None:
        monitor.PROMPT_HISTORY_PATH = self._old_history_path
        monitor.PROMPT_HISTORY_LIMIT = self._old_history_limit
        self._tmp.cleanup()
        asyncio.set_event_loop(None)
        self._main_loop.close()

    def _set_history_path(self, path: Path) -> None:
        monitor.PROMPT_HISTORY_PATH = str(path)

    def test_prompt_list_access_uses_state_lock(self):
        history_path = self.tmp_path / "prompt_history.json"
        self._set_history_path(history_path)
        monitor.PROMPT_HISTORY_LIMIT = 1
        state = TermFixState()
        entry = PromptEntry(
            session_id="session-a",
            context={},
            id="kept",
            messages=[{"role": "user", "content": "question"}],
        )
        state.prompts = [entry]

        class TrackingLock:
            def __init__(self) -> None:
                self.entries = 0

            def __enter__(self):
                self.entries += 1
                return self

            def __exit__(self, exc_type, exc, traceback):  # noqa: ANN001
                return False

        tracking_lock = TrackingLock()
        state._state_lock = tracking_lock

        self.assertIs(state.latest_prompt("session-a"), entry)
        self.assertIs(state.latest_prompt_any(), entry)
        self.assertIs(state.get_prompt("kept"), entry)
        self.assertEqual(state.prompt_entries(), [entry])
        self._main_loop.run_until_complete(
            state.add_prompt(
                PromptEntry(
                    session_id="session-a",
                    context={},
                    id="newest",
                    messages=[{"role": "user", "content": "new"}],
                )
            )
        )
        state.save_prompt_history()

        self.assertGreaterEqual(tracking_lock.entries, 7)
        self.assertEqual([prompt.id for prompt in state.prompts], ["newest"])

    def test_prompt_history_filters_messages_and_trims_limit(self):
        history_path = self.tmp_path / "prompt_history.json"
        self._set_history_path(history_path)
        monitor.PROMPT_HISTORY_LIMIT = 2

        state = TermFixState()
        state.prompts = [
            PromptEntry(
                session_id="session-a",
                context={},
                id="old",
                timestamp=1,
                updated_at=1,
                messages=[{"role": "user", "content": "old"}],
            ),
            PromptEntry(
                session_id="session-a",
                context={},
                id="kept",
                timestamp=2,
                updated_at=2,
                messages=[
                    {"role": "system", "content": "ignore"},
                    {"role": "user", "content": "question"},
                    {"role": "assistant", "content": "answer"},
                    {"role": "assistant", "content": ""},
                    "bad",
                ],
            ),
            PromptEntry(
                session_id="session-b",
                context={},
                id="newest",
                timestamp=3,
                updated_at=3,
                messages=[{"role": "user", "content": "new"}],
            ),
        ]

        state.save_prompt_history()

        payload = json.loads(history_path.read_text(encoding="utf-8"))
        self.assertEqual(payload["version"], monitor.PROMPT_HISTORY_VERSION)
        self.assertEqual([item["id"] for item in payload["prompts"]], ["kept", "newest"])
        self.assertEqual(
            payload["prompts"][0]["messages"],
            [
                {"role": "user", "content": "question"},
                {"role": "assistant", "content": "answer"},
            ],
        )

        restored = TermFixState()

        self.assertEqual([entry.id for entry in restored.prompts], ["kept", "newest"])
        self.assertEqual(restored.prompts[0].session_id, "")
        self.assertEqual(restored.prompts[0].status, "done")
        self.assertEqual(
            restored.prompts[0].messages,
            [
                {"role": "user", "content": "question"},
                {"role": "assistant", "content": "answer"},
            ],
        )

    def test_prompt_history_ignores_missing_or_malformed_files(self):
        self._set_history_path(self.tmp_path / "missing.json")
        self.assertEqual(TermFixState().prompts, [])

        bad_path = self.tmp_path / "bad.json"
        bad_path.write_text("{not-json", encoding="utf-8")
        self._set_history_path(bad_path)

        self.assertEqual(TermFixState().prompts, [])

    def test_version_1_prompt_history_loads_as_detached(self):
        history_path = self.tmp_path / "prompt_history.json"
        history_path.write_text(
            json.dumps(
                {
                    "version": 1,
                    "prompts": [
                        {
                            "id": "old-entry",
                            "timestamp": 100.0,
                            "updated_at": 200.0,
                            "messages": [{"role": "user", "content": "old prompt"}],
                        }
                    ],
                }
            ),
            encoding="utf-8",
        )
        self._set_history_path(history_path)

        state = TermFixState()

        self.assertEqual(len(state.prompts), 1)
        entry = state.prompts[0]
        self.assertEqual(entry.id, "old-entry")
        self.assertEqual(entry.session_id, "")
        self.assertEqual(entry.source_session_id, "")
        self.assertEqual(entry.context, {})
        self.assertIs(entry.restored, True)

    def test_prompt_history_saves_safe_metadata_and_reloads_detached(self):
        history_path = self.tmp_path / "state" / "prompt_history.json"
        self._set_history_path(history_path)

        state = TermFixState()
        state.prompts = [
            PromptEntry(
                session_id="live-session",
                context={
                    "command": "cat ~/.ssh/id_rsa",
                    "cwd": "/tmp/project",
                    "shell": "zsh",
                    "terminal_output": "secret line 1\nsecret line 2",
                    "context_lines": 25,
                },
                messages=[
                    {"role": "user", "content": "what changed?"},
                    {"role": "assistant", "content": "A status check."},
                ],
                status="done",
            )
        ]

        state.save_prompt_history()
        payload = json.loads(history_path.read_text(encoding="utf-8"))
        record = payload["prompts"][0]
        self.assertEqual(payload["version"], monitor.PROMPT_HISTORY_VERSION)
        self.assertEqual(record["source_session_id"], "live-session")
        self.assertEqual(record["context"]["cwd"], "/tmp/project")
        self.assertEqual(record["context"]["shell"], "zsh")
        self.assertEqual(record["context"]["context_lines"], 25)
        self.assertEqual(record["context"]["terminal_output_line_count"], 2)
        self.assertNotIn("command", record["context"])
        self.assertNotIn("terminal_output", record["context"])

        reloaded = TermFixState()
        restored = reloaded.prompts[0]
        self.assertEqual(restored.session_id, "")
        self.assertEqual(restored.source_session_id, "live-session")
        self.assertEqual(restored.context["cwd"], "/tmp/project")
        self.assertEqual(restored.context["terminal_output_line_count"], 2)
        self.assertNotIn("command", restored.context)
        self.assertNotIn("terminal_output", restored.context)
        self.assertIs(restored.restored, True)

    def test_legacy_prompt_history_drops_ambient_terminal_context(self):
        history_path = self.tmp_path / "prompt_history.json"
        history_path.write_text(
            json.dumps(
                {
                    "version": 2,
                    "prompts": [
                        {
                            "id": "legacy-context",
                            "timestamp": 100.0,
                            "updated_at": 200.0,
                            "session_id": "old-session",
                            "context": {
                                "command": "deploy --token secret",
                                "cwd": "/tmp/project",
                                "shell": "zsh",
                                "terminal_output": "token=secret\nsecond line",
                                "context_lines": 50,
                            },
                            "messages": [{"role": "user", "content": "old prompt"}],
                        }
                    ],
                }
            ),
            encoding="utf-8",
        )
        self._set_history_path(history_path)

        state = TermFixState()

        restored = state.prompts[0]
        self.assertEqual(restored.source_session_id, "old-session")
        self.assertEqual(restored.context["cwd"], "/tmp/project")
        self.assertEqual(restored.context["terminal_output_line_count"], 2)
        self.assertNotIn("command", restored.context)
        self.assertNotIn("terminal_output", restored.context)

    def test_attach_prompt_history_does_not_rebind_restored_entries(self):
        history_path = self.tmp_path / "prompt_history.json"
        self._set_history_path(history_path)
        state = TermFixState()
        restored = PromptEntry(
            session_id="",
            context={},
            messages=[{"role": "user", "content": "restored prompt"}],
            status="done",
            restored=True,
        )
        empty = PromptEntry(session_id="", context={}, status="input")
        state.prompts = [restored, empty]
        session = object()

        ui._attach_prompt_history_to_session(state, "current-session", session)

        self.assertEqual(restored.session_id, "")
        self.assertIsNone(restored.session)
        self.assertEqual(empty.session_id, "current-session")
        self.assertIs(empty.session, session)

    def test_prompt_hotkey_tracks_open_popovers_per_session(self):
        history_path = self.tmp_path / "prompt_history.json"
        self._set_history_path(history_path)
        state = TermFixState()
        opened_sessions = []

        class _App:
            def get_session_by_id(self, session_id):  # noqa: ANN001
                return types.SimpleNamespace(session_id=session_id)

        async def _open_popover(connection, session_id, state, html, size=None):  # noqa: ANN001
            opened_sessions.append(session_id)

        with mock.patch.object(ui, "_build_prompt_html", return_value="<html>"), mock.patch.object(
            ui,
            "_open_popover",
            side_effect=_open_popover,
        ), mock.patch.object(
            ui.iterm2,
            "Size",
            lambda width, height: (width, height),
            create=True,
        ):
            self._main_loop.run_until_complete(
                ui._handle_prompt_hotkey(object(), _App(), "session-a", state)
            )
            self._main_loop.run_until_complete(
                ui._handle_prompt_hotkey(object(), _App(), "session-b", state)
            )
            self._main_loop.run_until_complete(
                ui._handle_prompt_hotkey(object(), _App(), "session-a", state)
            )

        session_a_popover = ui._prompt_popover_id("session-a")
        session_b_popover = ui._prompt_popover_id("session-b")
        self.assertIn(session_a_popover, state.popover_last_seen)
        self.assertIn(session_b_popover, state.popover_last_seen)
        self.assertIn(session_a_popover, state.popover_close_requests)
        self.assertNotIn(session_b_popover, state.popover_close_requests)
        self.assertEqual(opened_sessions, ["session-a", "session-b"])

    def test_resume_prompt_binds_only_selected_restored_entry(self):
        history_path = self.tmp_path / "prompt_history.json"
        self._set_history_path(history_path)
        state = TermFixState()
        selected = PromptEntry(
            session_id="",
            context={"cwd": "/old"},
            messages=[{"role": "user", "content": "selected"}],
            status="done",
            restored=True,
        )
        other = PromptEntry(
            session_id="",
            context={"cwd": "/other"},
            messages=[{"role": "user", "content": "other"}],
            status="done",
            restored=True,
        )
        session = object()
        state.prompts = [selected, other]
        state.prompt_sessions["current-session"] = session

        self.assertIs(
            self._main_loop.run_until_complete(
                ui._resume_prompt_in_session(selected, state, "current-session")
            ),
            True,
        )

        self.assertEqual(selected.session_id, "current-session")
        self.assertIs(selected.session, session)
        self.assertEqual(selected.context, {})
        self.assertIs(selected.restored, False)
        self.assertEqual(other.session_id, "")
        self.assertEqual(other.context["cwd"], "/other")

    def test_resume_prompt_rejects_stale_stored_session(self):
        history_path = self.tmp_path / "prompt_history.json"
        self._set_history_path(history_path)
        state = TermFixState()
        selected = PromptEntry(
            session_id="",
            context={"cwd": "/old"},
            messages=[{"role": "user", "content": "selected"}],
            status="done",
            restored=True,
        )
        state.prompt_sessions["current-session"] = types.SimpleNamespace(
            session_id="old-session"
        )

        self.assertIs(
            self._main_loop.run_until_complete(
                ui._resume_prompt_in_session(selected, state, "current-session")
            ),
            False,
        )

        self.assertEqual(selected.session_id, "")
        self.assertIsNone(selected.session)
        self.assertEqual(selected.context["cwd"], "/old")

    def test_resume_prompt_falls_back_to_active_session_when_popover_session_closed(self):
        history_path = self.tmp_path / "prompt_history.json"
        self._set_history_path(history_path)
        state = TermFixState()
        state.connection = object()
        selected = PromptEntry(
            session_id="",
            context={"cwd": "/old"},
            messages=[{"role": "user", "content": "selected"}],
            status="done",
            restored=True,
        )
        active_session = types.SimpleNamespace(session_id="active-session")

        class _Tab:
            current_session = active_session

        class _Window:
            current_tab = _Tab()

        class _App:
            current_window = _Window()

            def get_session_by_id(self, session_id):  # noqa: ANN001
                if session_id == "active-session":
                    return active_session
                return None

            async def async_refresh_focus(self):
                return None

        async def _async_get_app(_connection):  # noqa: ANN001
            return _App()

        old_async_get_app = getattr(ui.iterm2, "async_get_app", None)
        ui.iterm2.async_get_app = _async_get_app
        try:
            self.assertIs(
                self._main_loop.run_until_complete(
                    ui._resume_prompt_in_session(selected, state, "closed-session")
                ),
                True,
            )
        finally:
            if old_async_get_app is None:
                delattr(ui.iterm2, "async_get_app")
            else:
                ui.iterm2.async_get_app = old_async_get_app

        self.assertEqual(selected.session_id, "active-session")
        self.assertIs(selected.session, active_session)
        self.assertEqual(selected.context, {})


if __name__ == "__main__":
    unittest.main()
