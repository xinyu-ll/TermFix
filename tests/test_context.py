from __future__ import annotations

import asyncio
import sys
import types
import unittest
from unittest import mock

sys.modules.setdefault(
    "iterm2",
    types.SimpleNamespace(Connection=object, ScreenContents=object, Session=object),
)

from termfixlib import context


class FakeLine:
    def __init__(self, text: str) -> None:
        self.string = text


class FakeContents:
    def __init__(self, lines: list[str]) -> None:
        self._lines = [FakeLine(line) for line in lines]

    @property
    def number_of_lines(self) -> int:
        return len(self._lines)

    def line(self, index: int) -> FakeLine:
        return self._lines[index]


class FakeLineInfo:
    def __init__(self, overflow: int, scrollback: int, mutable: int) -> None:
        self.overflow = overflow
        self.scrollback_buffer_height = scrollback
        self.mutable_area_height = mutable


class FakeSession:
    def __init__(self, overflow: int, lines: list[str]) -> None:
        self._overflow = overflow
        self._lines = lines
        self.contents_calls: list[tuple[int, int]] = []

    async def async_get_line_info(self) -> FakeLineInfo:
        return FakeLineInfo(
            overflow=self._overflow,
            scrollback=max(len(self._lines) - 5, 0),
            mutable=min(len(self._lines), 5),
        )

    async def async_get_contents(
        self,
        first_line: int,
        number_of_lines: int,
    ) -> FakeContents:
        self.contents_calls.append((first_line, number_of_lines))
        start = max(first_line - self._overflow, 0)
        return FakeContents(self._lines[start : start + number_of_lines])


class ListContentsSession(FakeSession):
    async def async_get_contents(
        self,
        first_line: int,
        number_of_lines: int,
    ) -> list[FakeLine]:
        self.contents_calls.append((first_line, number_of_lines))
        start = max(first_line - self._overflow, 0)
        return [FakeLine(line) for line in self._lines[start : start + number_of_lines]]


class ScreenOnlySession:
    def __init__(self, lines: list[str]) -> None:
        self._lines = lines

    async def async_get_screen_contents(self) -> FakeContents:
        return FakeContents(self._lines)


class ExplodingSession:
    async def async_get_line_info(self) -> None:
        raise AssertionError("line info should not be fetched")

    async def async_get_contents(self, first_line: int, number_of_lines: int) -> None:
        raise AssertionError("contents should not be fetched")

    async def async_get_screen_contents(self) -> None:
        raise AssertionError("screen contents should not be fetched")


class TerminalOutputTests(unittest.TestCase):
    def test_fetches_only_requested_tail_window(self) -> None:
        lines = [f"line-{line_number}" for line_number in range(5, 20)]
        session = FakeSession(overflow=5, lines=lines)

        output = asyncio.run(context._get_terminal_output(None, session, 3))

        self.assertEqual(output, "line-17\nline-18\nline-19")
        self.assertEqual(session.contents_calls, [(17, 3)])

    def test_supports_documented_list_line_contents_shape(self) -> None:
        lines = [f"line-{line_number}" for line_number in range(10)]
        session = ListContentsSession(overflow=0, lines=lines)

        output = asyncio.run(context._get_terminal_output(None, session, 4))

        self.assertEqual(output, "line-6\nline-7\nline-8\nline-9")
        self.assertEqual(session.contents_calls, [(6, 4)])

    def test_wraps_bounded_fetch_in_transaction_when_available(self) -> None:
        events: list[object] = []
        connection = object()

        class Transaction:
            def __init__(self, tx_connection: object) -> None:
                events.append(("init", tx_connection))

            async def __aenter__(self) -> None:
                events.append("enter")

            async def __aexit__(self, exc_type, exc, traceback) -> None:  # noqa: ANN001
                events.append("exit")

        class TransactionSession(FakeSession):
            async def async_get_line_info(self) -> FakeLineInfo:
                events.append("line-info")
                return await super().async_get_line_info()

            async def async_get_contents(
                self,
                first_line: int,
                number_of_lines: int,
            ) -> FakeContents:
                events.append(("contents", first_line, number_of_lines))
                return await super().async_get_contents(first_line, number_of_lines)

        session = TransactionSession(
            overflow=0,
            lines=[f"line-{line_number}" for line_number in range(5)],
        )

        with mock.patch.object(context.iterm2, "Transaction", Transaction, create=True):
            output = asyncio.run(context._get_terminal_output(connection, session, 2))

        self.assertEqual(output, "line-3\nline-4")
        self.assertEqual(
            events,
            [
                ("init", connection),
                "enter",
                "line-info",
                ("contents", 3, 2),
                "exit",
            ],
        )

    def test_clamps_large_context_requests(self) -> None:
        lines = [f"line-{line_number}" for line_number in range(1_500)]
        session = FakeSession(overflow=0, lines=lines)

        output = asyncio.run(context._get_terminal_output(None, session, 10_000))

        self.assertEqual(len(output.splitlines()), context.MAX_TERMINAL_CONTEXT_LINES)
        self.assertEqual(
            session.contents_calls,
            [(1_000, context.MAX_TERMINAL_CONTEXT_LINES)],
        )

    def test_ignores_zero_negative_and_invalid_limits(self) -> None:
        session = ExplodingSession()

        self.assertEqual(asyncio.run(context._get_terminal_output(None, session, 0)), "")
        self.assertEqual(asyncio.run(context._get_terminal_output(None, session, -5)), "")
        self.assertEqual(
            asyncio.run(context._get_terminal_output(None, session, "not-a-number")),
            "",
        )

    def test_screen_fallback_trims_without_unbounded_slice(self) -> None:
        session = ScreenOnlySession([f"screen-{index}" for index in range(10)])

        output = asyncio.run(context._get_terminal_output(None, session, 4))

        self.assertEqual(output, "screen-6\nscreen-7\nscreen-8\nscreen-9")


if __name__ == "__main__":
    unittest.main()
