from __future__ import annotations

import asyncio
import sys
import types
import unittest

sys.modules.setdefault("iterm2", types.SimpleNamespace())

from termfixlib.config import DEFAULT_CONTEXT_LINES, MAX_CONTEXT_LINES, MIN_CONTEXT_LINES
from termfixlib.context import _get_terminal_output, normalize_context_lines
from termfixlib.ui import _sync_knobs


class _Line:
    def __init__(self, text: str) -> None:
        self.string = text


class _Contents:
    def __init__(self, lines: list[str]) -> None:
        self._lines = [_Line(line) for line in lines]
        self.number_of_lines = len(lines)

    def line(self, index: int) -> _Line:
        return self._lines[index]


class _LineInfo:
    def __init__(self, line_count: int) -> None:
        self.overflow = 0
        self.scrollback_buffer_height = line_count
        self.mutable_area_height = 0


class _Session:
    def __init__(self, lines: list[str]) -> None:
        self._lines = lines

    async def async_get_line_info(self) -> _LineInfo:
        return _LineInfo(len(self._lines))

    async def async_get_contents(self, _start: int, _count: int) -> _Contents:
        return _Contents(self._lines)


class _State:
    def __init__(self) -> None:
        self.base_url = ""
        self.api_key = ""
        self.model = ""
        self.context_lines = DEFAULT_CONTEXT_LINES


class ContextLinesTests(unittest.TestCase):
    def test_normalize_context_lines_clamps_to_documented_range(self) -> None:
        self.assertEqual(normalize_context_lines("0"), MIN_CONTEXT_LINES)
        self.assertEqual(normalize_context_lines("-10"), MIN_CONTEXT_LINES)
        self.assertEqual(normalize_context_lines("9999"), MAX_CONTEXT_LINES)
        self.assertEqual(normalize_context_lines("not-a-number"), DEFAULT_CONTEXT_LINES)

    def test_sync_knobs_clamps_zero_and_preserves_previous_for_invalid_input(self) -> None:
        state = _State()

        _sync_knobs(state, {"context_lines": "0"})
        self.assertEqual(state.context_lines, MIN_CONTEXT_LINES)

        _sync_knobs(state, {"context_lines": "9999"})
        self.assertEqual(state.context_lines, MAX_CONTEXT_LINES)

        _sync_knobs(state, {"context_lines": "invalid"})
        self.assertEqual(state.context_lines, MAX_CONTEXT_LINES)

    def test_terminal_output_zero_context_lines_returns_no_output(self) -> None:
        output = asyncio.run(_get_terminal_output(_Session(["one", "two", "three"]), 0))

        self.assertEqual(output, "")

    def test_terminal_output_invalid_context_lines_returns_no_output(self) -> None:
        lines = [f"line {i}" for i in range(DEFAULT_CONTEXT_LINES + 10)]

        output = asyncio.run(_get_terminal_output(_Session(lines), "invalid"))

        self.assertEqual(output, "")

    def test_terminal_output_clamps_large_context_lines(self) -> None:
        lines = [f"line {i}" for i in range(MAX_CONTEXT_LINES + 100)]

        output = asyncio.run(_get_terminal_output(_Session(lines), MAX_CONTEXT_LINES + 1000))

        self.assertEqual(output.splitlines(), lines[-MAX_CONTEXT_LINES:])


if __name__ == "__main__":
    unittest.main()
