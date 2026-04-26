import asyncio

from termfixlib.context import _get_terminal_output


class FakeLine:
    def __init__(self, text):
        self.string = text


class FakeContents:
    def __init__(self, lines):
        self._lines = [FakeLine(line) for line in lines]
        self.number_of_lines = len(self._lines)

    def line(self, index):
        return self._lines[index]


class FakeSession:
    def __init__(self, lines):
        self._contents = FakeContents(lines)

    async def async_get_contents(self, start, count):
        return self._contents


def test_get_terminal_output_returns_last_requested_lines():
    session = FakeSession(["one  \n", "two", "three", "four"])

    assert asyncio.run(_get_terminal_output(session, 2)) == "three\nfour"
