from __future__ import annotations

import io
import json
import logging
import sys
import types
import unittest
import urllib.error
import urllib.request
from unittest import mock

sys.modules.setdefault("iterm2", types.ModuleType("iterm2"))

from termfixlib import ui  # noqa: E402


class DummyState:
    def __init__(self) -> None:
        self.status_server = None
        self.status_server_url = ""
        self.status_server_token = ""
        self.loop = None
        self.seen: list[str] = []
        self.closed: list[str] = []
        self.api_key = ""
        self.base_url = "https://api.example.test"
        self.model = "model-x"

    def get_prompt(self, entry_id: str):
        return None

    def get_error(self, entry_id: str):
        return None

    def mark_popover_seen(self, entry_id: str) -> None:
        self.seen.append(entry_id)

    def mark_popover_closed(self, entry_id: str) -> None:
        self.closed.append(entry_id)

    def consume_popover_close_request(self, entry_id: str) -> bool:
        return False


class PopoverStatusServerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.state = DummyState()
        ui._ensure_status_server(self.state)
        self.addCleanup(self.state.status_server.server_close)
        self.addCleanup(self.state.status_server.shutdown)

    def request(self, path: str, origin: str | None = None, method: str = "GET"):
        headers = {}
        if origin is not None:
            headers["Origin"] = origin
        request = urllib.request.Request(
            f"{self.state.status_server_url}{path}",
            headers=headers,
            method=method,
        )
        try:
            with urllib.request.urlopen(request, timeout=2) as response:
                return response.status, response.headers, json.loads(response.read())
        except urllib.error.HTTPError as exc:
            return exc.code, exc.headers, json.loads(exc.read())

    def token_path(self, route: str) -> str:
        return f"/{self.state.status_server_token}{route}"

    def test_status_server_binds_to_localhost_and_requires_token(self) -> None:
        self.assertEqual(self.state.status_server.server_address[0], "127.0.0.1")
        self.assertTrue(self.state.status_server_token)

        status, headers, payload = self.request("/wrong-token/state", origin="null")

        self.assertEqual(status, 403)
        self.assertFalse(payload["ok"])
        self.assertNotIn("Access-Control-Allow-Origin", headers)

    def test_cors_allows_only_opaque_popover_origin(self) -> None:
        status, headers, payload = self.request(
            self.token_path("/state?entry=missing"),
            origin="null",
        )

        self.assertEqual(status, 200)
        self.assertFalse(payload["ok"])
        self.assertEqual(headers["Access-Control-Allow-Origin"], "null")
        self.assertEqual(headers["Vary"], "Origin")
        self.assertIn("GET", headers["Access-Control-Allow-Methods"])

        status, headers, payload = self.request(
            self.token_path("/state?entry=missing"),
            origin="https://example.test",
        )

        self.assertEqual(status, 200)
        self.assertFalse(payload["ok"])
        self.assertNotIn("Access-Control-Allow-Origin", headers)
        self.assertNotIn("Access-Control-Allow-Methods", headers)

    def test_request_logging_does_not_include_path_token(self) -> None:
        stream = io.StringIO()
        handler = logging.StreamHandler(stream)
        logger = logging.getLogger("termfixlib.ui")
        old_level = logger.level
        logger.addHandler(handler)
        logger.setLevel(logging.DEBUG)
        try:
            self.request(self.token_path("/state?entry=missing"), origin="null")
        finally:
            logger.removeHandler(handler)
            logger.setLevel(old_level)

        self.assertNotIn(self.state.status_server_token, stream.getvalue())

    def test_test_connection_endpoint_reports_missing_api_key_without_provider_call(self) -> None:
        with mock.patch.object(ui, "check_provider_connection") as provider_test:
            status, headers, payload = self.request(
                self.token_path("/test-connection"),
                origin="null",
                method="POST",
            )

        self.assertEqual(status, 200)
        self.assertFalse(payload["ok"])
        self.assertEqual(payload["kind"], "missing_api_key")
        self.assertIn("API key", payload["error"])
        self.assertEqual(headers["Access-Control-Allow-Origin"], "null")
        provider_test.assert_not_called()

    def test_test_connection_endpoint_returns_provider_payload_without_exposing_key(self) -> None:
        self.state.api_key = "sk-secret"
        calls = []

        def fake_provider_test(api_key, base_url, model):  # noqa: ANN001
            calls.append((api_key, base_url, model))
            return {
                "ok": True,
                "message": "Connection succeeded.",
                "base_url": base_url,
                "model": model,
            }

        with mock.patch.object(ui, "check_provider_connection", side_effect=fake_provider_test):
            status, headers, payload = self.request(
                self.token_path("/test-connection"),
                origin="null",
                method="POST",
            )

        self.assertEqual(status, 200)
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["message"], "Connection succeeded.")
        self.assertEqual(payload["base_url"], "https://api.example.test")
        self.assertEqual(payload["model"], "model-x")
        self.assertEqual(headers["Access-Control-Allow-Origin"], "null")
        self.assertEqual(calls, [("sk-secret", "https://api.example.test", "model-x")])
        self.assertNotIn("sk-secret", json.dumps(payload))


if __name__ == "__main__":
    unittest.main()
