"""Tests for the second close job (close_jev_session.py).

Pure unit tests — Fyers, Telegram and the terminal are all faked, so
no network is used. The fake clock makes the wait-until-close test
instant: sleep advances the clock instead of the wall time.
"""

import unittest
from datetime import date, datetime, timedelta, timezone
from types import SimpleNamespace
from unittest import mock

from close_jev_session import (CloseJob, build_playbook_rows,
                               open_positions_from_engine)
from config import JevConfig

IST = timezone(timedelta(hours=5, minutes=30))
TODAY = date(2026, 11, 23)  # a Monday; the November monthly expiry
OPTION_SYMBOL = "NSE:NIFTY26NOV25300CE"


def _position(**overrides):
    pos = {"symbol": OPTION_SYMBOL, "qty": 75, "entry_price": 100.0,
           "stop_loss": 50.0, "target_1": 150.0, "target_2": 200.0,
           "is_paper": True, "status": "OPEN",
           "sl_moved_to_breakeven": False, "exit_order_id": ""}
    pos.update(overrides)
    return pos


def _engine(**overrides):
    record = {"spot": 25350.0, "tradingDate": TODAY.isoformat(),
              "pdh": 25337.6, "pdl": 25172.1, "pdc": 25291.1,
              "weekPoc": 25131.55, "expiryDate": "2026-11-23",
              "positions": []}
    record.update(overrides)
    return record


class FakeTelegram:
    """Captures cards/alerts instead of sending them."""

    def __init__(self):
        self.cards = []   # (title, rows, footer_extra)
        self.alerts = []  # (title, details)

    def send_card(self, title, rows, footer_extra=None):
        self.cards.append((title, list(rows), footer_extra))

    def send_alert(self, title, details):
        self.alerts.append((title, details))

    def titles(self):
        return [title for title, _, _ in self.cards]


class FakeFyers:
    """Minimal Fyers client: quotes, history. No option-chain method."""

    def __init__(self, option_ltp=120.0, index_ltp=25340.0, bars=None):
        self.option_ltp = option_ltp
        self.index_ltp = index_ltp
        self.bars = bars if bars is not None else []

    def quotes(self, data):
        symbol = data.get("symbols", "")
        if "NIFTY50-INDEX" in symbol:
            return {"lp": self.index_ltp}
        return {"lp": self.option_ltp}

    def history(self, data=None):
        return {"candles": self.bars}


class FakeClock:
    """now_fn/sleep pair: sleep advances the clock, not wall time."""

    def __init__(self, start):
        self.current = start
        self.slept_seconds = 0.0

    def now(self):
        return self.current

    def sleep(self, seconds):
        self.current += timedelta(seconds=seconds)
        self.slept_seconds += seconds


def _gex():
    return SimpleNamespace(call_wall=25400, put_wall=25200,
                           gamma_flip_level=25300.0,
                           market_regime="SHORT_GAMMA",
                           max_pain=25300, pcr=1.1)


def _bars():
    def ts(hour, minute):
        return int(datetime(2026, 11, 23, hour, minute,
                            tzinfo=IST).timestamp())
    return [[ts(9, 15), 25300.0, 25350.0, 25290.0, 25340.0, 100],
            [ts(15, 25), 25340.0, 25360.0, 25330.0, 25340.0, 120]]


class TestOpenPositionsFromEngine(unittest.TestCase):
    def test_open_position_parsed(self):
        positions = open_positions_from_engine(
            _engine(positions=[_position()]))
        self.assertEqual(list(positions), [OPTION_SYMBOL])
        self.assertEqual(positions[OPTION_SYMBOL].qty, 75)
        self.assertEqual(positions[OPTION_SYMBOL].entry_price, 100.0)

    def test_closed_positions_skipped(self):
        positions = open_positions_from_engine(_engine(positions=[
            _position(status="CLOSED"), _position(symbol="OTHER", qty=75)]))
        self.assertEqual(list(positions), ["OTHER"])

    def test_missing_positions_field_is_empty(self):
        self.assertEqual(open_positions_from_engine(_engine()), {})
        self.assertEqual(open_positions_from_engine({}), {})
        self.assertEqual(open_positions_from_engine(None), {})

    def test_unknown_keys_dropped(self):
        entry = _position(future_field="ignored")
        positions = open_positions_from_engine(_engine(positions=[entry]))
        self.assertFalse(hasattr(positions[OPTION_SYMBOL], "future_field"))


class TestBuildPlaybookRows(unittest.TestCase):
    def test_rows_match_orchestrator_labels(self):
        rows = build_playbook_rows(
            close=25340.0, prev_close=25291.1, today_high=25360.0,
            today_low=25290.0, gex=_gex(), expiry=TODAY)
        labels = [label for label, _ in rows]
        self.assertEqual(labels, ["NIFTY Close", "Day range",
                                  "Call wall / Put wall", "Max pain / PCR",
                                  "Gamma flip / Regime", "Tomorrow PDH / PDL",
                                  "Tomorrow PDC / Bias", "Next expiry"])
        by_label = dict(rows)
        self.assertEqual(by_label["NIFTY Close"],
                         "25,340.00 (+48.90 vs prev close)")
        self.assertEqual(by_label["Call wall / Put wall"], "25,400 / 25,200")
        self.assertEqual(by_label["Max pain / PCR"], "25,300 / 1.10")
        self.assertEqual(by_label["Tomorrow PDC / Bias"],
                         "25,340.00 / Bullish")
        self.assertEqual(by_label["Next expiry"], "Mon 23 Nov 2026")

    def test_no_gex_means_na_not_guessed(self):
        rows = build_playbook_rows(
            close=25340.0, prev_close=25291.1, today_high=25360.0,
            today_low=25290.0, gex=None, expiry=TODAY)
        by_label = dict(rows)
        self.assertEqual(by_label["Call wall / Put wall"],
                         "n/a — GEX unavailable")
        self.assertEqual(by_label["Max pain / PCR"], "n/a — GEX unavailable")


class TestCloseJobQuietPaths(unittest.TestCase):
    def _job(self, engine, clock_start=None):
        clock = FakeClock(clock_start or datetime(2026, 11, 23, 15, 16,
                                                  tzinfo=IST))
        tg = FakeTelegram()
        job = CloseJob(JevConfig(terminal_url="https://example.invalid",
                                 terminal_token="tok"),
                       fyers_client=FakeFyers(), telegram=tg,
                       fetch_engine=lambda url: engine,
                       sleep=clock.sleep, now_fn=clock.now)
        return job, tg, clock

    def test_no_engine_record_exits_quietly(self):
        job, tg, _ = self._job(None)
        self.assertEqual(job.run(), 0)
        self.assertEqual(tg.cards, [])
        self.assertEqual(tg.alerts, [])

    def test_stale_snapshot_exits_quietly(self):
        job, tg, _ = self._job(_engine(tradingDate="2026-11-20"))
        self.assertEqual(job.run(), 0)
        self.assertEqual(tg.cards, [])
        self.assertEqual(tg.alerts, [])

    def test_no_open_positions_exits_quietly(self):
        job, tg, _ = self._job(_engine(positions=[]))
        self.assertEqual(job.run(), 0)
        self.assertEqual(tg.cards, [])
        self.assertEqual(tg.alerts, [])

    def test_terminal_fetch_failure_exits_nonzero_with_alert(self):
        tg = FakeTelegram()
        job = CloseJob(JevConfig(terminal_url="https://example.invalid"),
                       fyers_client=FakeFyers(), telegram=tg,
                       fetch_engine=mock.Mock(
                           side_effect=RuntimeError("terminal down")))
        self.assertEqual(job.run(), 1)
        self.assertEqual(len(tg.alerts), 1)
        self.assertEqual(tg.alerts[0][0], "Close Job Failed")
        self.assertEqual(tg.cards, [])

    def test_login_failure_exits_nonzero_with_alert(self):
        tg = FakeTelegram()
        job = CloseJob(JevConfig(terminal_url="https://example.invalid"),
                       telegram=tg,
                       fetch_engine=lambda url: _engine(
                           positions=[_position()]))
        with mock.patch.object(CloseJob, "_login_fyers", return_value=None):
            self.assertEqual(job.run(), 1)
        self.assertEqual(len(tg.alerts), 1)
        self.assertEqual(tg.cards, [])


class TestCloseJobFullRun(unittest.TestCase):
    def test_square_off_playbook_and_final_push(self):
        clock = FakeClock(datetime(2026, 11, 23, 15, 16, tzinfo=IST))
        tg = FakeTelegram()
        pushes = []

        def fake_push(url, token, payload):
            pushes.append((url, token, payload))
            return True, None

        job = CloseJob(JevConfig(terminal_url="https://example.invalid",
                                 terminal_token="tok"),
                       fyers_client=FakeFyers(bars=_bars()), telegram=tg,
                       fetch_engine=lambda url: _engine(
                           positions=[_position()]),
                       sleep=clock.sleep, now_fn=clock.now)
        with mock.patch("close_jev_session.compute_gex_snapshot",
                        return_value=_gex()), \
             mock.patch("close_jev_session.push_engine_snapshot",
                        side_effect=fake_push):
            self.assertEqual(job.run(), 0)

        # The clock was advanced to the 15:31 close by the wait.
        self.assertGreaterEqual(clock.current.hour * 60 + clock.current.minute,
                                15 * 60 + 31)
        self.assertEqual(tg.titles(), ["End-of-Day Square-Off",
                                       "Session Closed",
                                       "Post-Market Playbook"])
        square_off = tg.cards[0][1]
        self.assertIn((f"{OPTION_SYMBOL}: squared off @ 120.0"),
                      [value for _, value in square_off])
        playbook = dict(tg.cards[2][1])
        self.assertEqual(playbook["NIFTY Close"],
                         "25,340.00 (+48.90 vs prev close)")
        self.assertEqual(playbook["Day range"], "25,290.00 - 25,360.00")
        self.assertEqual(playbook["Call wall / Put wall"], "25,400 / 25,200")
        self.assertEqual(playbook["Next expiry"], "Mon 23 Nov 2026")

        # One final push, with no open positions left.
        self.assertEqual(len(pushes), 1)
        self.assertEqual(pushes[0][0], "https://example.invalid")
        self.assertEqual(pushes[0][1], "tok")
        self.assertEqual(pushes[0][2]["spot"], 25340.0)
        self.assertEqual(pushes[0][2]["positions"], [])

        # A re-run reading that final push stays silent: no duplicate
        # playbook, no duplicate square-off card.
        tg2 = FakeTelegram()
        job2 = CloseJob(JevConfig(terminal_url="https://example.invalid"),
                        fyers_client=FakeFyers(), telegram=tg2,
                        fetch_engine=lambda url: _engine(
                            positions=pushes[0][2]["positions"]),
                        sleep=clock.sleep, now_fn=clock.now)
        self.assertEqual(job2.run(), 0)
        self.assertEqual(tg2.cards, [])
        self.assertEqual(tg2.alerts, [])

    def test_wait_skipped_when_close_is_hours_away(self):
        # Engine job died at 10:00 with a position open: square off
        # now, but do not hold a runner until 15:31 for the playbook.
        clock = FakeClock(datetime(2026, 11, 23, 10, 0, tzinfo=IST))
        tg = FakeTelegram()
        job = CloseJob(JevConfig(terminal_url="https://example.invalid"),
                       fyers_client=FakeFyers(bars=_bars()), telegram=tg,
                       fetch_engine=lambda url: _engine(
                           positions=[_position()]),
                       sleep=clock.sleep, now_fn=clock.now)
        with mock.patch("close_jev_session.compute_gex_snapshot",
                        return_value=_gex()):
            self.assertEqual(job.run(), 0)
        self.assertEqual(tg.titles(), ["End-of-Day Square-Off",
                                       "Session Closed"])
        self.assertEqual(clock.slept_seconds, 0.0)


if __name__ == "__main__":
    unittest.main()
