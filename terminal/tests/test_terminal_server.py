"""Tests for the Jev Terminal HTTP server, incl. the /api/engine feed.

Runs a real server on localhost (ephemeral port) with a temporary data
directory. No external network is used: the base snapshot is written
fresh, so refresh_snapshot() never needs to fetch Yahoo.
"""

import json
import os
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from http.server import ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

os.environ["JEV_TERMINAL_DATA_DIR"] = tempfile.mkdtemp(prefix="jev-terminal-test-")
os.environ["JEV_TERMINAL_TOKEN"] = "test-token-123"

import server as terminal_server  # noqa: E402


def _bars(closes, start_epoch=1794800700):
    return [{"t": start_epoch + i * 300, "o": c, "h": c + 5, "l": c - 5, "c": c}
            for i, c in enumerate(closes)]


def _base_snapshot():
    now_utc = datetime.now(timezone.utc).isoformat()
    return {"spot": 25473.75, "change": 182.65, "changePct": 0.72,
            "dayOpen": 25344.45, "dayHigh": 25496.65, "dayLow": 25303.65,
            "tradingDate": "2026-11-17", "pdc": 25291.10,
            "pdh": 25337.60, "pdl": 25172.10, "weekPoc": 25131.55,
            "pocSource": "engine", "vah": 25400, "val": 25100,
            "atmStrike": 25450, "expiryDate": "2026-11-17",
            "bars": _bars([25350, 25400, 25450, 25473]),
            "vix": {"price": 15.49, "changePct": -1.18},
            "bankNifty": {"price": 62380.20, "changePct": 1.31},
            "sensex": {"price": 82862.11, "changePct": 0.92},
            "feed": "snapshot", "asOf": now_utc,
            "istTime": "12:32:29 IST", "marketStatus": "live",
            "conditions": [], "greeks": {}, "scenarios": []}


class TestEngineEndpoint(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.data_dir = Path(os.environ["JEV_TERMINAL_DATA_DIR"])
        (cls.data_dir / "snapshot.json").write_text(
            json.dumps(_base_snapshot()), encoding="utf-8")
        cls.httpd = ThreadingHTTPServer(
            ("127.0.0.1", 0), terminal_server.Handler)
        cls.port = cls.httpd.server_address[1]
        cls.thread = threading.Thread(target=cls.httpd.serve_forever,
                                      daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.httpd.server_close()

    def _request(self, path, method="GET", body=None, token=None):
        headers = {"Content-Type": "application/json"}
        if token is not None:
            headers["X-Jev-Token"] = token
        req = urllib.request.Request(
            f"http://127.0.0.1:{self.port}{path}",
            data=json.dumps(body).encode("utf-8") if body is not None else None,
            headers=headers, method=method)
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                return resp.status, json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            return exc.code, json.loads(exc.read().decode("utf-8"))

    def _engine_payload(self):
        return {"spot": 25400.0,
                "asOf": datetime.now(timezone.utc).isoformat(),
                "tradingDate": "2026-11-17", "pdh": 25337.60,
                "pdl": 25172.10, "pdc": 25291.10, "weekPoc": 25131.55,
                "callWall": 26000, "putWall": 25000, "maxPain": 25200,
                "pcr": 0.90, "expiryDate": "2026-11-17",
                "bars": _bars([25380, 25400, 25410, 25400])}

    def test_engine_post_without_token_is_rejected(self):
        status, _ = self._request("/api/engine", method="POST",
                                  body=self._engine_payload())
        self.assertEqual(status, 401)

    def test_engine_post_with_wrong_token_is_rejected(self):
        status, _ = self._request("/api/engine", method="POST",
                                  body=self._engine_payload(),
                                  token="not-the-token")
        self.assertEqual(status, 401)

    def test_engine_post_without_spot_is_rejected(self):
        payload = self._engine_payload()
        del payload["spot"]
        status, _ = self._request("/api/engine", method="POST",
                                  body=payload, token="test-token-123")
        self.assertEqual(status, 400)

    def test_engine_push_shows_fyers_feed(self):
        status, body = self._request("/api/engine", method="POST",
                                     body=self._engine_payload(),
                                     token="test-token-123")
        self.assertEqual(status, 200)
        self.assertTrue(body["ok"])
        status, data = self._request("/api/terminal")
        self.assertEqual(status, 200)
        self.assertEqual(data["snapshot"]["feed"], "fyers")
        self.assertEqual(data["snapshot"]["spot"], 25400.0)
        self.assertEqual(data["snapshot"]["atmStrike"], 25400)
        self.assertEqual(data["chainLog"]["callWall"], 26000)
        self.assertEqual(data["chainLog"]["maxPain"], 25200)
        self.assertEqual(data["engine"]["feed"], "fyers")

    def test_stale_engine_push_falls_back_to_snapshot(self):
        self._request("/api/engine", method="POST",
                      body=self._engine_payload(), token="test-token-123")
        stale = json.loads((self.data_dir / "engine.json").read_text())
        stale["receivedAt"] = (
            datetime.now(timezone.utc) - timedelta(minutes=20)).isoformat()
        (self.data_dir / "engine.json").write_text(
            json.dumps(stale), encoding="utf-8")
        _, data = self._request("/api/terminal")
        self.assertEqual(data["snapshot"]["feed"], "snapshot")
        self.assertEqual(data["engine"]["feed"], "snapshot")


class TestWriteEndpointAuth(unittest.TestCase):
    """Every write endpoint except /api/engine used to take no token."""

    @classmethod
    def setUpClass(cls):
        cls.data_dir = Path(os.environ["JEV_TERMINAL_DATA_DIR"])
        (cls.data_dir / "snapshot.json").write_text(
            json.dumps(_base_snapshot()), encoding="utf-8")
        cls.httpd = ThreadingHTTPServer(
            ("127.0.0.1", 0), terminal_server.Handler)
        cls.port = cls.httpd.server_address[1]
        cls.thread = threading.Thread(target=cls.httpd.serve_forever,
                                      daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.httpd.server_close()

    def _request(self, path, method="GET", body=None, token=None):
        headers = {"Content-Type": "application/json"}
        if token is not None:
            headers["X-Jev-Token"] = token
        req = urllib.request.Request(
            f"http://127.0.0.1:{self.port}{path}",
            data=json.dumps(body).encode("utf-8") if body is not None else None,
            headers=headers, method=method)
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                return resp.status, json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            return exc.code, json.loads(exc.read().decode("utf-8"))

    def test_chain_post_without_token_is_rejected(self):
        status, _ = self._request("/api/chain", method="POST",
                                  body={"callWall": 26000, "putWall": 25000})
        self.assertEqual(status, 401)

    def test_chain_post_with_wrong_token_is_rejected(self):
        status, _ = self._request("/api/chain", method="POST",
                                  body={"callWall": 26000, "putWall": 25000},
                                  token="not-the-token")
        self.assertEqual(status, 401)

    def test_chain_post_with_token_succeeds(self):
        status, body = self._request(
            "/api/chain", method="POST",
            body={"callWall": 26000, "putWall": 25000, "maxPain": 25200,
                  "pcr": 0.90},
            token="test-token-123")
        self.assertEqual(status, 200)
        self.assertTrue(body["ok"])
        _, data = self._request("/api/terminal")
        self.assertEqual(data["chainLog"]["callWall"], 26000)
        self.assertEqual(data["chainLog"]["maxPain"], 25200)

    def test_events_post_without_token_is_rejected(self):
        status, _ = self._request("/api/events", method="POST",
                                  body={"kind": "note", "text": "hello"})
        self.assertEqual(status, 401)

    def test_events_delete_without_token_is_rejected(self):
        _, created = self._request("/api/events", method="POST",
                                   body={"kind": "note", "text": "to delete"},
                                   token="test-token-123")
        event_id = created["event"]["id"]
        status, _ = self._request(f"/api/events?id={event_id}",
                                  method="DELETE")
        self.assertEqual(status, 401)
        _, data = self._request("/api/terminal")
        self.assertIn(event_id, [e["id"] for e in data["events"]])

    def test_events_delete_with_token_succeeds(self):
        _, created = self._request("/api/events", method="POST",
                                   body={"kind": "note", "text": "to delete"},
                                   token="test-token-123")
        event_id = created["event"]["id"]
        status, body = self._request(f"/api/events?id={event_id}",
                                     method="DELETE", token="test-token-123")
        self.assertEqual(status, 200)
        self.assertTrue(body["ok"])
        _, data = self._request("/api/terminal")
        self.assertNotIn(event_id, [e["id"] for e in data["events"]])

    def test_plan_build_without_token_is_rejected(self):
        status, _ = self._request("/api/plan/build", method="POST", body={})
        self.assertEqual(status, 401)

    def test_plan_note_without_token_is_rejected(self):
        status, _ = self._request("/api/plan/note", method="POST",
                                  body={"planDate": "2026-11-17",
                                        "note": "hello"})
        self.assertEqual(status, 401)


if __name__ == "__main__":
    unittest.main()
