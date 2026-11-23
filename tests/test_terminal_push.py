"""Tests for the Jev Terminal live-feed push (terminal_push.py).

Pure unit tests — the HTTP call is mocked, so no network is used.
"""

import json
import os
import unittest
from datetime import date, datetime, timedelta, timezone
from types import SimpleNamespace
from unittest import mock

import urllib.error

from config import JevConfig
from lag_neutralized_engine import FastBar
from terminal_push import (bars_to_chart_bars, build_engine_payload,
                           push_engine_snapshot)

IST = timezone(timedelta(hours=5, minutes=30))


def _gex():
    return SimpleNamespace(call_wall=24200, put_wall=23800,
                           gamma_flip_level=24050.0,
                           market_regime="SHORT_GAMMA_EXPANSION",
                           max_pain=24000, pcr=0.8333)


class TestBarsToChartBars(unittest.TestCase):
    def test_hhmm_converts_to_ist_epoch(self):
        bars = [FastBar("09:15", 25300.0, 25350.0, 25290.0, 25340.0, 100, 300),
                FastBar("09:20", 25340.0, 25360.0, 25330.0, 25355.0, 120, 300)]
        out = bars_to_chart_bars(bars, date(2026, 11, 17))
        self.assertEqual(len(out), 2)
        self.assertEqual(
            out[0]["t"],
            int(datetime(2026, 11, 17, 9, 15, tzinfo=IST).timestamp()))
        self.assertEqual(out[0], {"t": out[0]["t"], "o": 25300.0,
                                  "h": 25350.0, "l": 25290.0, "c": 25340.0})

    def test_bad_timestamp_is_skipped_not_fatal(self):
        bars = [FastBar("not-a-time", 1.0, 1.0, 1.0, 1.0, 1, 300),
                FastBar("09:15", 2.0, 2.0, 2.0, 2.0, 1, 300)]
        out = bars_to_chart_bars(bars, date(2026, 11, 17))
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0]["c"], 2.0)


class TestBuildEnginePayload(unittest.TestCase):
    def test_payload_carries_walls_max_pain_pcr(self):
        payload = build_engine_payload(
            spot=25400.0, pdh=25337.6, pdl=25172.1, pdc=25291.1,
            week_poc=25131.55, gex_snapshot=_gex(), expiry=date(2026, 11, 17),
            bars=[FastBar("09:15", 25300.0, 25350.0, 25290.0, 25340.0, 100, 300)],
            trading_date=date(2026, 11, 17))
        self.assertEqual(payload["spot"], 25400.0)
        self.assertEqual(payload["callWall"], 24200)
        self.assertEqual(payload["putWall"], 23800)
        self.assertEqual(payload["maxPain"], 24000)
        self.assertEqual(payload["pcr"], 0.83)
        self.assertEqual(payload["expiryDate"], "2026-11-17")
        self.assertEqual(len(payload["bars"]), 1)

    def test_no_gex_snapshot_means_no_walls(self):
        payload = build_engine_payload(
            spot=25400.0, pdh=25337.6, pdl=25172.1, pdc=25291.1,
            week_poc=25131.55, gex_snapshot=None, expiry=date(2026, 11, 17),
            bars=[], trading_date=date(2026, 11, 17))
        self.assertNotIn("callWall", payload)
        self.assertNotIn("maxPain", payload)
        self.assertNotIn("bars", payload)

    def test_positions_carried_when_given(self):
        # The close job reads open positions from this field.
        position = {"symbol": "NSE:NIFTY26NOV25300CE", "qty": 75,
                    "entry_price": 100.0, "status": "OPEN"}
        payload = build_engine_payload(
            spot=25400.0, pdh=25337.6, pdl=25172.1, pdc=25291.1,
            week_poc=25131.55, gex_snapshot=None, expiry=date(2026, 11, 17),
            bars=[], trading_date=date(2026, 11, 17), positions=[position])
        self.assertEqual(payload["positions"], [position])

    def test_empty_positions_list_is_kept(self):
        # [] means "already closed" to the close job; it must not be
        # dropped the way a None value is.
        payload = build_engine_payload(
            spot=25400.0, pdh=25337.6, pdl=25172.1, pdc=25291.1,
            week_poc=25131.55, gex_snapshot=None, expiry=date(2026, 11, 17),
            bars=[], trading_date=date(2026, 11, 17), positions=[])
        self.assertEqual(payload["positions"], [])

    def test_positions_absent_when_not_given(self):
        payload = build_engine_payload(
            spot=25400.0, pdh=25337.6, pdl=25172.1, pdc=25291.1,
            week_poc=25131.55, gex_snapshot=None, expiry=date(2026, 11, 17),
            bars=[], trading_date=date(2026, 11, 17))
        self.assertNotIn("positions", payload)


class TestPushEngineSnapshot(unittest.TestCase):
    def test_posts_json_with_token_header(self):
        seen = {}

        class FakeResponse:
            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

            def read(self):
                return b'{"ok": true}'

        def fake_urlopen(request, timeout=None):
            seen["url"] = request.full_url
            seen["token"] = request.headers.get("X-jev-token")
            seen["body"] = json.loads(request.data.decode("utf-8"))
            return FakeResponse()

        with mock.patch("terminal_push.urllib.request.urlopen",
                        side_effect=fake_urlopen):
            ok, err = push_engine_snapshot(
                "https://imarket-3.onrender.com", "secret-token",
                {"spot": 25400.0})
        self.assertTrue(ok)
        self.assertIsNone(err)
        self.assertEqual(seen["url"],
                         "https://imarket-3.onrender.com/api/engine")
        self.assertEqual(seen["token"], "secret-token")
        self.assertEqual(seen["body"], {"spot": 25400.0})

    def test_network_failure_returns_false_never_raises(self):
        with mock.patch("terminal_push.urllib.request.urlopen",
                        side_effect=urllib.error.URLError("down")):
            ok, err = push_engine_snapshot("https://example.invalid", "t",
                                           {"spot": 1.0})
        self.assertFalse(ok)
        self.assertIn("down", err)

    def test_empty_url_is_a_clean_no(self):
        ok, err = push_engine_snapshot("", "t", {"spot": 1.0})
        self.assertFalse(ok)
        self.assertIn("no terminal URL", err)


class TestTerminalConfig(unittest.TestCase):
    def test_config_reads_terminal_env(self):
        env = {"JEV_TERMINAL_URL": "https://imarket-3.onrender.com",
               "JEV_TERMINAL_TOKEN": "secret-token"}
        with mock.patch.dict(os.environ, env, clear=False):
            config = JevConfig.from_env()
        self.assertEqual(config.terminal_url,
                         "https://imarket-3.onrender.com")
        self.assertEqual(config.terminal_token, "secret-token")

    def test_config_defaults_to_disabled(self):
        env = {"JEV_TERMINAL_URL": "", "JEV_TERMINAL_TOKEN": ""}
        with mock.patch.dict(os.environ, env, clear=False):
            config = JevConfig.from_env()
        self.assertEqual(config.terminal_url, "")
        self.assertEqual(config.terminal_token, "")


if __name__ == "__main__":
    unittest.main()
