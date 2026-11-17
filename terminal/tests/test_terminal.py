"""Unit tests for the standalone Jev Terminal core logic.

Run from the repo root:  python3 -m pytest terminal/tests/test_terminal.py
or:                     python3 -m unittest discover -s terminal/tests -v
No network is used — Yahoo payloads are synthetic.
"""

import math
import sys
import unittest
from datetime import date, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import jev_terminal as jt


def _bars(closes, start_epoch=1794800700):
    return [{"t": start_epoch + i * 300, "o": c, "h": c + 5, "l": c - 5, "c": c}
            for i, c in enumerate(closes)]


class TestGreeks(unittest.TestCase):
    def test_atm_delta_about_half(self):
        g = jt.black_scholes_greeks(25291, 25300, 0.8 / 365, 0.1568)
        self.assertAlmostEqual(g["ceDelta"], 0.49, delta=0.05)
        self.assertAlmostEqual(g["peDelta"], -0.51, delta=0.05)
        self.assertGreater(g["cePremium"], 0)
        self.assertGreater(g["pePremium"], 0)

    def test_put_call_parity(self):
        spot, strike, t, iv, r = 25291, 25300, 2 / 365, 0.1568, 0.07
        g = jt.black_scholes_greeks(spot, strike, t, iv, r)
        lhs = g["cePremium"] - g["pePremium"]
        rhs = spot - strike * math.exp(-r * t)
        self.assertAlmostEqual(lhs, rhs, delta=1.0)

    def test_theta_negative_and_grows_into_expiry(self):
        near = jt.black_scholes_greeks(25291, 25300, 2 / 365, 0.1568)
        far = jt.black_scholes_greeks(25291, 25300, 3 / 365, 0.1568)
        self.assertLess(near["ceTheta"], 0)
        self.assertLess(near["ceTheta"], far["ceTheta"])

    def test_atm_strike_rounds_to_50(self):
        self.assertEqual(jt.atm_strike(25291), 25300)
        self.assertEqual(jt.atm_strike(25274), 25250)

    def test_scenarios_move_with_spot(self):
        rows = jt.scenario_table(25291, 25300, 0.8 / 365, 0.1568)
        self.assertEqual([r["move"] for r in rows], [-100, -50, 0, 50, 100])
        self.assertLess(rows[0]["ce"], rows[-1]["ce"])
        self.assertGreater(rows[0]["pe"], rows[-1]["pe"])


class TestExpiry(unittest.TestCase):
    def test_monday_16_nov_2026_expires_tuesday_17_nov(self):
        # NIFTY weekly expiry is Tuesday since 1 Sep 2025 (NSE/SEBI).
        now = datetime(2026, 11, 16, 10, 0, tzinfo=jt.IST)
        self.assertEqual(jt.next_weekly_expiry(now), date(2026, 11, 17))

    def test_tuesday_after_close_rolls_to_next_tuesday(self):
        now = datetime(2026, 11, 17, 16, 0, tzinfo=jt.IST)
        self.assertEqual(jt.next_weekly_expiry(now), date(2026, 11, 24))

    def test_next_trading_day_skips_weekend(self):
        self.assertEqual(jt.next_trading_day(date(2026, 11, 13)), date(2026, 11, 16))


class TestYahooParsing(unittest.TestCase):
    def test_parse_chart(self):
        payload = {"chart": {"result": [{
            "meta": {"regularMarketPrice": 25291.1, "previousClose": 25144.3},
            "timestamp": [1000, 1300, 1600],
            "indicators": {"quote": [{"close": [25200, 25250, 25291.1],
                                      "open": [25190, 25200, 25250],
                                      "high": [25210, 25260, 25300],
                                      "low": [25180, 25190, 25240]}]},
        }]}}
        out = jt.parse_yahoo_chart(payload)
        self.assertEqual(len(out["bars"]), 3)
        self.assertEqual(out["price"], 25291.1)
        self.assertEqual(out["previous_close"], 25144.3)

    def test_parse_chart_rejects_empty(self):
        with self.assertRaises(ValueError):
            jt.parse_yahoo_chart({"chart": {"result": []}})


class TestLevelsAndSignals(unittest.TestCase):
    def test_weekly_profile_poc_is_most_visited_bin(self):
        bars = _bars([25200] * 10 + [25300] * 4 + [25100] * 2)
        prof = jt.weekly_profile(bars)
        self.assertAlmostEqual(prof["weekPoc"], 25205, delta=10)
        self.assertGreaterEqual(prof["vah"], prof["weekPoc"])
        self.assertLessEqual(prof["val"], prof["weekPoc"])

    def test_compute_levels_prev_day(self):
        prev = _bars([25100, 25200, 25150])
        today = _bars([25250, 25291])
        levels = jt.compute_levels(today, prev, prev + today)
        self.assertEqual(levels["pdh"], 25205)
        self.assertEqual(levels["pdl"], 25095)
        self.assertEqual(levels["pdc"], 25150)

    def test_engine_chain_overrides_poc(self):
        prev = _bars([25100, 25200])
        levels = jt.compute_levels(prev, prev, prev, chain={"weekPoc": 25131.55})
        self.assertEqual(levels["weekPoc"], 25131.55)
        self.assertEqual(levels["pocSource"], "engine")

    def test_signal_conditions_bullish_and_clear_of_poc(self):
        closes = [25000 + i * 10 for i in range(14)]  # steady uptrend
        bars = _bars(closes)
        levels = {"pdc": 25100, "weekPoc": 24000, "pocSource": "engine"}
        now = datetime(2026, 11, 16, 10, 30, tzinfo=jt.IST)
        conds, trend_dir, breakout = jt.signal_conditions(25291, bars, levels, now)
        by_key = {c["key"]: c for c in conds}
        self.assertEqual(by_key["bias"]["state"], "met")
        self.assertEqual(trend_dir, "up")
        self.assertEqual(by_key["poc"]["state"], "met")
        self.assertEqual(by_key["gamma-cvd"]["state"], "engine")

    def test_poc_chop_zone_blocks(self):
        bars = _bars([25290] * 14)
        levels = {"pdc": 25100, "weekPoc": 25291, "pocSource": "engine"}
        now = datetime(2026, 11, 16, 10, 30, tzinfo=jt.IST)
        conds, _, _ = jt.signal_conditions(25291, bars, levels, now)
        by_key = {c["key"]: c for c in conds}
        self.assertEqual(by_key["poc"]["state"], "unmet")

    def test_gamma_blast_watch_window(self):
        expiry = date(2026, 11, 17)
        in_window = jt.gamma_blast_watch(25291, {"callWall": 25300}, expiry,
                                         datetime(2026, 11, 17, 14, 30, tzinfo=jt.IST))
        self.assertTrue(in_window["inWindow"])
        out = jt.gamma_blast_watch(25291, {"callWall": 25300}, expiry,
                                   datetime(2026, 11, 17, 10, 30, tzinfo=jt.IST))
        self.assertFalse(out["inWindow"])


class TestPlan(unittest.TestCase):
    def test_plan_for_next_trading_day(self):
        snap = {"spot": 25291.10, "change": 146.8, "changePct": 0.58,
                "dayOpen": 25206.15, "dayHigh": 25337.6, "dayLow": 25172.1,
                "tradingDate": "2026-11-16", "pdc": 25144.3,
                "weekPoc": 25131.55, "pocSource": "engine",
                "vah": 25400, "val": 25100, "bias": "bullish",
                "expiryDate": "2026-11-17", "vix": {"price": 15.68}}
        plan = jt.build_postmarket_plan(snap)
        self.assertEqual(plan["planDate"], "2026-11-17")
        self.assertTrue(plan["isExpiryDay"])
        self.assertEqual(plan["pdc"], 25291.10)  # tomorrow's PDC is today's close
        self.assertEqual(plan["pdh"], 25337.6)
        self.assertEqual(plan["pdl"], 25172.1)
        self.assertTrue(plan["checklist"])
        self.assertIn("Paper mode", plan["riskRules"][0])


class TestEngineSnapshot(unittest.TestCase):
    def _base(self):
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
                "feed": "snapshot", "source": "old label that must be dropped",
                "asOf": "2026-11-17T07:02:29+00:00",
                "istTime": "12:32:29 IST", "marketStatus": "live"}

    def _engine(self):
        return {"spot": 25400.0, "asOf": "2026-11-17T08:05:00+00:00",
                "tradingDate": "2026-11-17", "pdh": 25337.60,
                "pdl": 25172.10, "pdc": 25291.10, "weekPoc": 25131.55,
                "callWall": 26000, "putWall": 25000, "maxPain": 25200,
                "pcr": 0.90, "expiryDate": "2026-11-17",
                "bars": _bars([25380, 25400, 25410, 25400])}

    def test_feed_is_fyers_and_no_vendor_source(self):
        snap = jt.apply_engine_snapshot(
            self._base(), self._engine(),
            now=datetime(2026, 11, 17, 13, 35, tzinfo=jt.IST))
        self.assertEqual(snap["feed"], "fyers")
        self.assertNotIn("source", snap)
        self.assertEqual(snap["spot"], 25400.0)
        self.assertEqual(snap["weekPoc"], 25131.55)
        self.assertEqual(snap["pocSource"], "engine")
        self.assertEqual(snap["chain"]["callWall"], 26000)
        self.assertEqual(snap["chain"]["maxPain"], 25200)

    def test_greeks_recomputed_from_engine_spot(self):
        snap = jt.apply_engine_snapshot(
            self._base(), self._engine(),
            now=datetime(2026, 11, 17, 13, 35, tzinfo=jt.IST))
        self.assertEqual(snap["atmStrike"], 25400)
        self.assertEqual(snap["change"], 108.90)
        self.assertGreater(snap["greeks"]["cePremium"], 0)
        self.assertGreater(snap["greeks"]["pePremium"], 0)

    def test_engine_bars_replace_base_bars(self):
        snap = jt.apply_engine_snapshot(
            self._base(), self._engine(),
            now=datetime(2026, 11, 17, 13, 35, tzinfo=jt.IST))
        self.assertEqual([b["c"] for b in snap["bars"]],
                         [25380, 25400, 25410, 25400])
        self.assertEqual(snap["dayHigh"], 25415)

    def test_engine_without_chain_fields_is_still_fyers(self):
        engine = {"spot": 25400.0, "asOf": "2026-11-17T08:05:00+00:00",
                  "tradingDate": "2026-11-17", "pdc": 25291.10,
                  "expiryDate": "2026-11-17"}
        snap = jt.apply_engine_snapshot(
            self._base(), engine,
            now=datetime(2026, 11, 17, 13, 35, tzinfo=jt.IST))
        self.assertEqual(snap["feed"], "fyers")
        self.assertNotIn("chain", snap)

    def test_build_snapshot_feed_is_snapshot_not_yahoo(self):
        import unittest.mock as mock

        def fake_fetch(symbol, range_, interval):
            return {"bars": _bars([25200, 25300, 25291]),
                    "price": 25291.0, "previous_close": 25144.3}

        def fake_quote(symbol):
            return {"bars": _bars([15.0]), "price": 15.0,
                    "previous_close": 15.5}

        with mock.patch.object(jt, "fetch_quote", side_effect=fake_quote):
            snap = jt.build_snapshot(
                now=datetime(2026, 11, 16, 10, 30, tzinfo=jt.IST),
                fetcher=fake_fetch)
        self.assertEqual(snap["feed"], "snapshot")
        self.assertNotIn("source", snap)


if __name__ == "__main__":
    unittest.main()
