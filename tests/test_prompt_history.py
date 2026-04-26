import asyncio
import json

from termfixlib import monitor


async def _new_state():
    return monitor.TermFixState()


def new_state():
    return asyncio.run(_new_state())


def test_prompt_history_round_trips_serializable_messages(tmp_path, monkeypatch):
    history_path = tmp_path / "prompt_history.json"
    monkeypatch.setattr(monitor, "PROMPT_HISTORY_PATH", str(history_path))
    monkeypatch.setattr(monitor, "PROMPT_HISTORY_LIMIT", 2)

    state = new_state()
    state.prompts = [
        monitor.PromptEntry(
            session_id="session-a",
            context={},
            id="old",
            timestamp=1,
            updated_at=1,
            messages=[{"role": "user", "content": "old"}],
        ),
        monitor.PromptEntry(
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
        monitor.PromptEntry(
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
    assert payload["version"] == 1
    assert [item["id"] for item in payload["prompts"]] == ["kept", "newest"]
    assert payload["prompts"][0]["messages"] == [
        {"role": "user", "content": "question"},
        {"role": "assistant", "content": "answer"},
    ]

    restored = new_state()

    assert [entry.id for entry in restored.prompts] == ["kept", "newest"]
    assert restored.prompts[0].session_id == ""
    assert restored.prompts[0].status == "done"
    assert restored.prompts[0].messages == [
        {"role": "user", "content": "question"},
        {"role": "assistant", "content": "answer"},
    ]


def test_prompt_history_ignores_missing_or_malformed_files(tmp_path, monkeypatch):
    monkeypatch.setattr(monitor, "PROMPT_HISTORY_PATH", str(tmp_path / "missing.json"))
    assert new_state().prompts == []

    bad_path = tmp_path / "bad.json"
    bad_path.write_text("{not-json", encoding="utf-8")
    monkeypatch.setattr(monitor, "PROMPT_HISTORY_PATH", str(bad_path))

    assert new_state().prompts == []
