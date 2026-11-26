"""Tests for the Fyers data gaps (PR fix/jev-fyers-data-gaps, 25 Nov 2026).

Each section maps to one step of the prioritised list in
research_notes/fyers-data-options-trading-20261125-1548/report.md.
Step 1 (chain fields already received) is covered in test_gex_iv.py.
All fakes are in-memory; no network is used.
"""
import json
import os
import tempfile
import unittest
from datetime import date, datetime, timedelta, timezone
from unittest import mock

from config import JevConfig
from engine_state_store import EngineStateStore
from execution_broker_interface import ExecutionRouter
from india_gex_engine import IndiaGEXEngine, OptionStrikeData
from lag_neutralized_engine import FastBar
from main_jev_orchestrator import LegTickTracker
from market_data import (
    archive_chain_snapshot,
    check_market_open,
    compute_gex_snapshot,
    fetch_expired_option_history,
    fetch_option_history,
    get_batch_quotes,
    get_market_depth,
    get_option_quote,
    validate_weekly_symbol,
)

IST = timezone(timedelta(hours=5, minutes=30))
LEG = "NSE:NIFTY26N0125400CE"


# ------------------------------------------------------------------ step 2
class TestLegTickTracker(unittest.TestCase):
    def test_latest_ltp_fresh_and_stale(self):
        tracker = LegTickTracker()
        assert tracker.latest_ltp(LEG) is None  # nothing tracked yet
        tracker.track(LEG, 68.5, 68.0, 69.0, 75, now=1000.0)
        assert tracker.latest_ltp(LEG, now=1000.0) == 68.5
        assert tracker.latest_ltp(LEG, now=1119.0) == 68.5   # 119 s: fresh
        assert tracker.latest_ltp(LEG, now=1121.0) is None   # 121 s: stale
        tracker.track(LEG, 70.0, 69.5, 70.5, 75, now=1121.0)
        assert tracker.latest_ltp(LEG, now=1121.0) == 70.0

    def test_flush_bar_builds_ohlc_and_clears_ticks(self):
        tracker = LegTickTracker()
        for ltp in (68.0, 70.5, 69.0, 71.0):
            tracker.track(LEG, ltp, ltp - 0.5, ltp + 0.5, 75, now=1000.0)
        bar = FastBar("10:00", 25000.0, 25100.0, 24900.0, 25050.0, 100, 300)
        tracker.flush_bar(bar)
        assert len(tracker.bars[LEG]) == 1
        leg_bar = tracker.bars[LEG][0]
        assert (leg_bar.open, leg_bar.high, leg_bar.low, leg_bar.close) == (68.0, 71.0, 68.0, 71.0)
        assert leg_bar.volume == 300
        assert tracker.state[LEG]["ticks"] == []
        tracker.flush_bar(bar)  # no ticks: no second bar
        assert len(tracker.bars[LEG]) == 1

    def test_bars_capped_at_100(self):
        tracker = LegTickTracker()
        bar = FastBar("10:00", 1.0, 1.0, 1.0, 1.0, 1, 300)
        for _ in range(105):
            tracker.track(LEG, 68.0, 67.5, 68.5, 75, now=1000.0)
            tracker.flush_bar(bar)
        assert len(tracker.bars[LEG]) == 100


# ------------------------------------------------------------------ step 3
class FakeQuotes:
    """Fyers quotes() returning canned rows; records the call.

    Only rows for the requested symbols are returned, like the real
    endpoint."""

    def __init__(self, rows):
        self.rows = rows
        self.seen = []

    def quotes(self, data):
        self.seen.append(dict(data))
        wanted = set(data.get("symbols", "").split(","))
        return {"s": "ok",
                "d": [r for r in self.rows if r.get("symbol") in wanted]}


class TestBatchQuotes(unittest.TestCase):
    def test_batch_returns_ltps_for_all_symbols(self):
        fake = FakeQuotes([
            {"symbol": LEG, "lp": 68.5, "bid": 68.0, "ask": 69.0},
            {"symbol": "NSE:NIFTY50-INDEX", "lp": 25455.9},
            {"symbol": "NSE:INDIAVIX-INDEX", "lp": 17.31},
        ])
        out = get_batch_quotes(fake, [LEG, "NSE:NIFTY50-INDEX",
                                      "NSE:INDIAVIX-INDEX"])
        assert out == {LEG: 68.5, "NSE:NIFTY50-INDEX": 25455.9,
                       "NSE:INDIAVIX-INDEX": 17.31}
        assert fake.seen[0]["symbols"] == ",".join(
            [LEG, "NSE:NIFTY50-INDEX", "NSE:INDIAVIX-INDEX"])

    def test_batch_skips_symbols_without_a_price(self):
        fake = FakeQuotes([
            {"symbol": LEG, "lp": 68.5},
            {"symbol": "NSE:NIFTY50-INDEX"},  # no lp at all
            {"symbol": "NSE:INDIAVIX-INDEX", "lp": 0},  # zero is not a price
        ])
        out = get_batch_quotes(fake, [LEG, "NSE:NIFTY50-INDEX",
                                      "NSE:INDIAVIX-INDEX"])
        assert out == {LEG: 68.5}

    def test_batch_failure_returns_empty_dict(self):
        class Failing:
            def quotes(self, data):
                raise RuntimeError("network down")

        assert get_batch_quotes(Failing(), [LEG]) == {}
        assert get_batch_quotes(None, [LEG]) == {}

    def test_batch_truncates_to_50_symbols(self):
        fake = FakeQuotes([])
        symbols = [f"NSE:SYM{i}" for i in range(60)]
        get_batch_quotes(fake, symbols)
        assert len(fake.seen[0]["symbols"].split(",")) == 50

    def test_single_symbol_quote_still_works(self):
        fake = FakeQuotes([{"symbol": LEG, "lp": 68.5}])
        assert get_option_quote(fake, LEG) == 68.5
        assert get_option_quote(fake, "NSE:MISSING") is None


# ------------------------------------------------------------------ step 4
class FakeHistory:
    """Fyers history() returning canned candles; records the call."""

    def __init__(self, candles):
        self.candles = candles
        self.seen = []

    def history(self, data):
        self.seen.append(dict(data))
        return {"s": "ok", "candles": list(self.candles)}


def _ts(hour, minute):
    return int(datetime(2026, 11, 25, hour, minute, tzinfo=IST).timestamp())


class TestOptionHistory(unittest.TestCase):
    def test_bars_vwap_and_atr(self):
        fake = FakeHistory([
            [_ts(9, 15), 60.0, 70.0, 55.0, 65.0, 1000, 5000],
            [_ts(9, 20), 65.0, 75.0, 60.0, 70.0, 2000, 5200],
        ])
        out = fetch_option_history(fake, LEG, day=date(2026, 11, 25))
        assert out is not None
        assert len(out["bars"]) == 2
        assert out["bars"][0].timestamp == "09:15"
        # VWAP: (63.33*1000 + 68.33*2000) / 3000 = 66.67
        assert out["vwap"] == 66.67
        # ATR: TRs 15 and max(15, |75-65|, |60-65|) = 15 -> 15.0
        assert out["atr"] == 15.0
        assert fake.seen[0]["oi_flag"] == "1"
        assert fake.seen[0]["symbol"] == LEG
        assert fake.seen[0]["resolution"] == "5"

    def test_empty_or_failed_history_returns_none(self):
        assert fetch_option_history(FakeHistory([]), LEG) is None

        class Failing:
            def history(self, data):
                raise RuntimeError("boom")

        assert fetch_option_history(Failing(), LEG) is None
        assert fetch_option_history(None, LEG) is None


class TestChainArchive(unittest.TestCase):
    def _chain(self):
        return [OptionStrikeData(strike=25400, call_oi=100, put_oi=200,
                                 call_iv=0.13, put_iv=0.13, call_ltp=60.0,
                                 put_ltp=50.0, call_oich=10, put_oich=20,
                                 call_gamma=0.0002, put_gamma=0.0002)]

    def test_archive_writes_one_json_line_per_snapshot(self):
        with tempfile.TemporaryDirectory() as tmp:
            snap = IndiaGEXEngine().process_chain(
                "NIFTY", 25455.90, 0.002, self._chain())
            path = archive_chain_snapshot(
                self._chain(), spot=25455.90, expiry=date(2026, 12, 1),
                snapshot=snap, vix=17.31, archive_dir=tmp)
            assert path is not None and path.endswith(
                f"chain_{datetime.now(IST).date().isoformat()}.jsonl")
            with open(path, encoding="utf-8") as fh:
                record = json.loads(fh.readline())
            assert record["spot"] == 25455.9
            assert record["vix"] == 17.31
            assert record["expiry"] == "2026-12-01"
            assert record["strikes"][0]["call_oi"] == 100
            assert record["strikes"][0]["call_oich"] == 10
            assert record["call_wall"] == 25400

    def test_archive_never_raises(self):
        # Unwritable dir: returns None, no exception.
        assert archive_chain_snapshot(
            self._chain(), spot=25455.90, expiry=date(2026, 12, 1),
            archive_dir="/proc/definitely-not-a-dir") is None
        assert archive_chain_snapshot(
            [], spot=25455.90, expiry=date(2026, 12, 1)) is None

    def test_snapshot_archives_only_when_asked(self):
        class FlatChain:
            def optionchain(self, data):
                rows = [{"strike_price": -1, "option_type": ""}]
                for strike in (25300, 25350, 25400, 25450, 25500):
                    for opt, oi in (("CE", 1000), ("PE", 2000)):
                        rows.append({"strike_price": strike, "option_type": opt,
                                     "oi": oi, "ltp": 50.0,
                                     "greeks": {"iv": 0.13}})
                return {"s": "ok", "data": {"optionsChain": rows}}

        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.dict(os.environ, {"CHAIN_ARCHIVE_DIR": tmp}):
                snap = compute_gex_snapshot(
                    FlatChain(), 25455.90, date(2026, 12, 1), archive=True)
                assert snap is not None
                assert os.listdir(tmp) == [
                    f"chain_{datetime.now(IST).date().isoformat()}.jsonl"]
            with tempfile.TemporaryDirectory() as tmp2:
                with mock.patch.dict(os.environ, {"CHAIN_ARCHIVE_DIR": tmp2}):
                    compute_gex_snapshot(
                        FlatChain(), 25455.90, date(2026, 12, 1))
                    assert os.listdir(tmp2) == []  # default: no files


# ------------------------------------------------------------------ step 5
class FakeDepth:
    def __init__(self, book):
        self.book = book

    def depth(self, data):
        return {"s": "ok", "d": {data["symbol"]: self.book}}


class TestMarketDepth(unittest.TestCase):
    def test_best_bid_ask_and_spread(self):
        fake = FakeDepth({"bids": [[68.0, 100, 5], [67.5, 50, 2]],
                          "ask": [[69.0, 80, 3], [69.5, 40, 1]],
                          "ltp": 68.5, "totalbuyqty": 500,
                          "totalsellqty": 400, "oi": 12345})
        out = get_market_depth(fake, LEG)
        assert out == {"bid": 68.0, "ask": 69.0, "spread": 1.0,
                       "ltp": 68.5, "total_buy_qty": 500,
                       "total_sell_qty": 400, "oi": 12345}

    def test_empty_or_crossed_book_is_none(self):
        assert get_market_depth(FakeDepth({}), LEG) is None
        # Crossed book (ask <= bid) is not a spread.
        assert get_market_depth(
            FakeDepth({"bids": [[69.0, 10, 1]], "ask": [[68.0, 10, 1]]}),
            LEG) is None
        assert get_market_depth(None, LEG) is None


class TestBrokerDepthCheck(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._n = 0

    def tearDown(self):
        self._tmp.cleanup()

    def _router(self, depth_fn, max_spread=1.0):
        # Fresh store per router: each buy under test must not trip the
        # max-open-positions rule left over from a previous buy.
        self._n += 1
        store = EngineStateStore(
            os.path.join(self._tmp.name, f"t{self._n}.db"))
        config = JevConfig(max_option_spread_rs=max_spread)
        return ExecutionRouter(None, store, paper_mode=True,
                               config=config, depth_fn=depth_fn)

    def test_wide_spread_rejects_and_narrow_spread_fills(self):
        wide = self._router(lambda sym: {"bid": 68.0, "ask": 70.5,
                                         "spread": 2.5, "ltp": 69.0})
        result = wide.execute_buy(LEG, 75, 69.0, 34.5, 103.5, 138.0)
        assert result.status == "REJECTED"
        assert "spread" in result.reason

        narrow = self._router(lambda sym: {"bid": 68.0, "ask": 68.5,
                                           "spread": 0.5, "ltp": 68.2})
        assert narrow.execute_buy(LEG, 75, 68.2, 34.1, 102.3, 136.4).status == "FILLED"

    def test_depth_failure_is_fail_open(self):
        def failing(sym):
            raise RuntimeError("depth down")

        router = self._router(failing)
        assert router.execute_buy(LEG, 75, 68.0, 34.0, 102.0, 136.0).status == "FILLED"
        no_fn = self._router(None)
        assert no_fn.execute_buy(LEG, 75, 68.0, 34.0, 102.0, 136.0).status == "FILLED"


# ------------------------------------------------------------------ step 6
class FakeMarketStatus:
    def __init__(self, rows=None, exc=None):
        self.rows = rows
        self.exc = exc

    def market_status(self):
        if self.exc is not None:
            raise self.exc
        return {"s": "ok", "marketStatus": self.rows}


class TestMarketStatus(unittest.TestCase):
    def test_open_preopen_closed_unknown(self):
        assert check_market_open(FakeMarketStatus([{
            "exchange": "NSE", "segment": "CM", "market_type": "NORMAL",
            "status": "OPEN"}]))[0] == "open"
        assert check_market_open(FakeMarketStatus([{
            "exchange": "NSE", "segment": "FO", "market_type": "NORMAL",
            "status": "PREOPEN"}]))[0] == "open"
        assert check_market_open(FakeMarketStatus([{
            "exchange": "NSE", "segment": "CM", "market_type": "NORMAL",
            "status": "CLOSE"}]))[0] == "closed"
        assert check_market_open(FakeMarketStatus([{
            "exchange": "NSE", "segment": "CM", "market_type": "AUCTION",
            "status": "OPEN"}]))[0] == "unknown"  # not a NORMAL row
        assert check_market_open(FakeMarketStatus([]))[0] == "unknown"
        assert check_market_open(
            FakeMarketStatus(exc=RuntimeError("no permission")))[0] == "unknown"
        assert check_market_open(None)[0] == "unknown"


class TestSymbolValidation(unittest.TestCase):
    def _snapshot(self):
        chain = [OptionStrikeData(strike=s, call_oi=100, put_oi=200,
                                  call_iv=0.13, put_iv=0.13, call_ltp=50.0,
                                  put_ltp=50.0)
                 for s in (25300, 25350, 25400, 25450, 25500)]
        return IndiaGEXEngine().process_chain(
            "NIFTY", 25455.90, 0.002, chain,
            expiry_dates=(date(2026, 12, 1),))

    def test_valid_symbol_and_mismatches(self):
        snap = self._snapshot()
        ok, _ = validate_weekly_symbol(snap, LEG, date(2026, 12, 1), 25400)
        assert ok is True
        ok, why = validate_weekly_symbol(snap, LEG, date(2026, 12, 8), 25400)
        assert ok is False and "expiry" in why
        ok, why = validate_weekly_symbol(snap, LEG, date(2026, 12, 1), 26000)
        assert ok is False and "strike" in why

    def test_no_snapshot_means_skip_not_block(self):
        ok, _ = validate_weekly_symbol(None, LEG, date(2026, 12, 1), 25400)
        assert ok is True


# ------------------------------------------------------------------ step 7
class TestWideChain(unittest.TestCase):
    def test_strikecount_50_requested_and_parsed(self):
        seen = []

        class WideChain:
            def optionchain(self, data):
                seen.append(dict(data))
                rows = [{"strike_price": -1, "option_type": ""}]
                for i in range(51):
                    strike = 24000 + i * 50
                    for opt, oi in (("CE", 1000 + i), ("PE", 2000 + i)):
                        rows.append({"strike_price": strike, "option_type": opt,
                                     "oi": oi, "ltp": 50.0,
                                     "greeks": {"iv": 0.13}})
                return {"s": "ok", "data": {"optionsChain": rows}}

        snap = compute_gex_snapshot(WideChain(), 25455.90, date(2026, 12, 8),
                                    strikecount=50)
        assert snap is not None
        assert seen[0]["strikecount"] == 50
        assert len(snap.strikes) == 51


# ------------------------------------------------------------------ step 8
class FakeExpiredFno:
    def __init__(self, candles):
        self.candles = candles
        self.seen = []

    def fno_historical_data(self, data):
        self.seen.append(dict(data))
        return {"s": "ok", "candles": list(self.candles)}


class TestExpiredFno(unittest.TestCase):
    def test_candles_with_oi_and_greeks(self):
        fake = FakeExpiredFno([
            [1764000000, 60.0, 70.0, 55.0, 65.0, 1000, 5000, {"delta": 0.5}],
            [1764000300, 65.0, 75.0, 60.0, 70.0, 2000, 5200, {"delta": 0.55}],
        ])
        out = fetch_expired_option_history(
            fake, "NSE:NIFTY26N0125400CE", "2026-11-24", "2026-11-24")
        assert out is not None and len(out) == 2
        assert out[0]["oi"] == 5000
        assert out[0]["greeks"] == {"delta": 0.5}
        assert out[1]["close"] == 70.0
        assert fake.seen[0]["include_oi"] == 1
        assert fake.seen[0]["include_greeks"] == 1
        assert fake.seen[0]["symbol"] == "NSE:NIFTY26N0125400CE"

    def test_empty_or_failed_returns_none(self):
        assert fetch_expired_option_history(
            FakeExpiredFno([]), LEG, "2026-11-24", "2026-11-24") is None
        assert fetch_expired_option_history(
            None, LEG, "2026-11-24", "2026-11-24") is None

        class NoMethod:
            pass

        assert fetch_expired_option_history(
            NoMethod(), LEG, "2026-11-24", "2026-11-24") is None


if __name__ == "__main__":
    unittest.main()
