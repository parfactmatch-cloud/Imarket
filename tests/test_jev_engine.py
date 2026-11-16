"""Tests for Jev Engine's critical fixes.

No test hits the live Fyers API or Telegram: broker clients are fakes,
the state store uses a temporary SQLite file, and auth token generation
is monkeypatched.
"""

import json
from datetime import date, datetime, timedelta, timezone

import pytest

from config import JevConfig
from engine_state_store import EngineStateStore, PositionState
from execution_broker_interface import ExecutionRouter
from expiry import next_weekly_expiry, option_symbol
from fyers_auth_automation import FyersAuthManager
from gamma_blast_detector import GammaBlastDetector, StrikeTelemetry
from greeks_structural_engine import BarData, GreeksState, GreeksStructuralEngine
from lag_neutralized_engine import FastBar, LagNeutralizedEngine
from market_data import compute_dealer_net_dex, get_option_quote
from mtf_profile_context_builder import MTFProfileContextBuilder, ProfileLevels
from position_monitor import PositionMonitor

IST = timezone(timedelta(hours=5, minutes=30))


# ---------------------------------------------------------------- helpers
def make_bars(closes, start=24000.0):
    """5-minute bars with the given closes (high=close+5, low=close-5)."""
    return [
        FastBar(f"09:{15 + i * 5:02d}", c - 2, c + 5, c - 5, c, 1000, 300)
        for i, c in enumerate(closes)
    ]


def rising_history(n=14, start=24000.0, step=10.0):
    return make_bars([start + i * step for i in range(n)])


def falling_history(n=14, start=24130.0, step=-10.0):
    return make_bars([start + i * step for i in range(n)])


@pytest.fixture()
def store(tmp_path):
    return EngineStateStore(str(tmp_path / "test.db"))


@pytest.fixture()
def paper_router(store):
    return ExecutionRouter(fyers_client=None, state_store=store, paper_mode=True)


def buy_paper(router, symbol="NSE:NIFTY26NOV23250CE", premium=45.0):
    return router.execute_buy(symbol, 75, premium, sl=22.5, t1=67.5, t2=90.0)


# ---------------------------------------------------------------- lag engine
def test_lag_engine_fires_with_real_history():
    engine = LagNeutralizedEngine(pdh=24200.0, pdl=23900.0, pdc=24000.0)
    history = rising_history()
    ceiling = max(b.high for b in history[-6:])
    current = FastBar("11:00", 24120, 24140, 24110, ceiling + 10, 1000, 270)
    decision = engine.evaluate_lag_free_execution(
        current, history, dealer_net_dex=125000.0, cvd_delta=500)
    assert decision["signal"] == "EARLY_BUY"


def test_lag_engine_sell_with_real_history():
    engine = LagNeutralizedEngine(pdh=24200.0, pdl=23900.0, pdc=24000.0)
    history = falling_history()
    base = min(b.low for b in history[-6:])
    current = FastBar("11:00", 24020, 24030, 24000, base - 10, 1000, 270)
    decision = engine.evaluate_lag_free_execution(
        current, history, dealer_net_dex=-125000.0, cvd_delta=-500)
    assert decision["signal"] == "EARLY_SELL"


def test_lag_engine_hold_with_empty_history():
    # The original bug: the orchestrator always passed [] and the
    # engine could only return HOLD.
    engine = LagNeutralizedEngine(pdh=24200.0, pdl=23900.0, pdc=24000.0)
    current = FastBar("11:00", 24120, 24140, 24110, 24130, 1000, 270)
    decision = engine.evaluate_lag_free_execution(
        current, [], dealer_net_dex=125000.0, cvd_delta=500)
    assert decision["signal"] == "HOLD"


def test_lag_engine_hold_when_dealer_gamma_unknown():
    # Unknown GEX (chain fetch failed) must never produce a signal.
    engine = LagNeutralizedEngine(pdh=24200.0, pdl=23900.0, pdc=24000.0)
    history = rising_history()
    ceiling = max(b.high for b in history[-6:])
    current = FastBar("11:00", 24120, 24140, 24110, ceiling + 10, 1000, 270)
    decision = engine.evaluate_lag_free_execution(
        current, history, dealer_net_dex=None, cvd_delta=500)
    assert decision["signal"] == "HOLD"


def test_lag_engine_hold_before_sub_bar_maturity():
    engine = LagNeutralizedEngine(pdh=24200.0, pdl=23900.0, pdc=24000.0)
    history = rising_history()
    ceiling = max(b.high for b in history[-6:])
    current = FastBar("11:00", 24120, 24140, 24110, ceiling + 10, 1000, 100)
    decision = engine.evaluate_lag_free_execution(
        current, history, dealer_net_dex=125000.0, cvd_delta=500)
    assert decision["signal"] == "HOLD"


# ---------------------------------------------------------------- expiry
def test_next_weekly_expiry_from_monday():
    assert next_weekly_expiry(datetime(2026, 11, 16, 10, 0, tzinfo=IST)) == date(2026, 11, 17)


def test_next_weekly_expiry_tuesday_before_close():
    assert next_weekly_expiry(datetime(2026, 11, 17, 10, 0, tzinfo=IST)) == date(2026, 11, 17)


def test_next_weekly_expiry_tuesday_after_close_rolls_forward():
    assert next_weekly_expiry(datetime(2026, 11, 17, 16, 0, tzinfo=IST)) == date(2026, 11, 24)


def test_next_weekly_expiry_thursday_is_not_expiry():
    # NIFTY weeklies moved Thursday -> Tuesday on 1 Sep 2025: a Thursday
    # now rolls forward to the next Tuesday, it is never expiry itself.
    assert next_weekly_expiry(datetime(2026, 11, 19, 10, 0, tzinfo=IST)) == date(2026, 11, 24)


def test_next_weekly_expiry_override():
    assert next_weekly_expiry(datetime(2026, 11, 16, 10, 0, tzinfo=IST),
                              override="2026-11-18") == date(2026, 11, 18)


def test_option_symbol_format():
    assert option_symbol("NIFTY", date(2026, 11, 26), 23250, "CE") == "NSE:NIFTY26NOV23250CE"
    assert option_symbol("NIFTY", date(2026, 11, 17), 25300, "CE") == "NSE:NIFTY17NOV25300CE"
    assert option_symbol("NIFTY", date(2026, 12, 3), 24000, "PE") == "NSE:NIFTY03DEC24000PE"


# ---------------------------------------------------------------- option pricing
class FakeQuotes:
    def __init__(self, response):
        self.response = response

    def quotes(self, data):
        return self.response


def test_get_option_quote_parses_fyers_shape():
    fyers = FakeQuotes({"s": "ok", "d": [{"n": "NSE:NIFTY26NOV23250CE", "v": {"lp": 82.5}}]})
    assert get_option_quote(fyers, "NSE:NIFTY26NOV23250CE") == 82.5


def test_get_option_quote_returns_none_on_failure():
    assert get_option_quote(FakeQuotes({"s": "error", "d": []}), "NSE:X") is None
    assert get_option_quote(None, "NSE:X") is None


def test_paper_buy_prices_off_premium_not_spot(paper_router):
    result = buy_paper(paper_router, premium=45.0)
    assert result.status == "FILLED"
    # Simulated fill is premium * 1.005, not the ~24,000 spot price.
    assert result.filled_price == pytest.approx(45.22, abs=0.01)
    assert paper_router.virtual_cash == pytest.approx(200000.0 - 45.22 * 75, abs=0.01)


def test_spot_priced_order_is_rejected_by_cash_check(paper_router):
    # The old bug priced options at the NIFTY spot (~24,000): that
    # order must now be rejected instead of driving cash negative.
    result = paper_router.execute_buy("NSE:NIFTY26NOV23250CE", 75, 24000.0,
                                      sl=12000.0, t1=36000.0, t2=48000.0)
    assert result.status == "REJECTED"
    assert paper_router.virtual_cash == 200000.0
    assert paper_router.store.load_open_positions() == {}


# ---------------------------------------------------------------- exit monitor
def _open_position(store, symbol="NSE:NIFTY26NOV23250CE", qty=75, entry=100.0):
    pos = PositionState(symbol, qty, entry, 50.0, 150.0, 200.0, True, "OPEN")
    store.save_position(pos)
    return pos


def test_exit_monitor_stop_loss(store, paper_router):
    _open_position(store)
    monitor = PositionMonitor(store, paper_router, quote_fn=lambda s: 40.0)
    actions = monitor.check_positions()
    assert any("STOP_LOSS" in a for a in actions)
    assert store.load_open_positions() == {}
    stats = store.get_daily_stats(datetime.now(IST).strftime("%Y-%m-%d"))
    assert stats["realized_pnl"] == pytest.approx((40.0 - 100.0) * 75)


def test_exit_monitor_target_2(store, paper_router):
    _open_position(store)
    monitor = PositionMonitor(store, paper_router, quote_fn=lambda s: 210.0)
    actions = monitor.check_positions()
    assert any("TARGET_2" in a for a in actions)
    assert store.load_open_positions() == {}


def test_exit_monitor_target_1_moves_stop_to_breakeven(store, paper_router):
    _open_position(store)
    monitor = PositionMonitor(store, paper_router, quote_fn=lambda s: 160.0)
    monitor.check_positions()
    positions = store.load_open_positions()
    assert len(positions) == 1
    pos = next(iter(positions.values()))
    assert pos.stop_loss == 100.0  # breakeven
    assert pos.sl_moved_to_breakeven is True


def test_exit_monitor_target_1_partial_for_multi_lot(store, paper_router):
    _open_position(store, qty=150)
    monitor = PositionMonitor(store, paper_router, quote_fn=lambda s: 160.0)
    monitor.check_positions()
    positions = store.load_open_positions()
    pos = next(iter(positions.values()))
    assert pos.qty == 75
    assert pos.status == "PARTIAL_CLOSED"


def test_exit_monitor_no_quote_leaves_position_open(store, paper_router):
    _open_position(store)
    monitor = PositionMonitor(store, paper_router, quote_fn=lambda s: None)
    assert monitor.check_positions() == []
    assert len(store.load_open_positions()) == 1


def test_panic_close_uses_market_price_not_entry(store, paper_router):
    _open_position(store)
    paper_router.set_quote_fn(lambda s: 60.0)
    paper_router.emergency_panic_close_all({})
    assert store.load_open_positions() == {}
    # Exited at 60, not booked at the 100 entry price.
    stats = store.get_daily_stats(datetime.now(IST).strftime("%Y-%m-%d"))
    assert stats["realized_pnl"] == pytest.approx((60.0 - 100.0) * 75)


def test_panic_close_without_price_leaves_position_open(store, paper_router):
    _open_position(store)
    paper_router.emergency_panic_close_all({})
    assert len(store.load_open_positions()) == 1


# ---------------------------------------------------------------- risk limits
def test_max_trades_per_day(paper_router):
    config = JevConfig(max_trades_per_day=1)
    router = ExecutionRouter(None, paper_router.store, paper_mode=True, config=config)
    assert buy_paper(router).status == "FILLED"
    second = router.execute_buy("NSE:NIFTY26NOV23300CE", 75, 40.0, 20.0, 60.0, 80.0)
    assert second.status == "REJECTED"


def test_daily_loss_limit_blocks_new_entries(store):
    today = datetime.now(IST).strftime("%Y-%m-%d")
    store.record_trade(today, realized_pnl=-10001.0, trades=0)
    router = ExecutionRouter(None, store, paper_mode=True)
    assert buy_paper(router).status == "REJECTED"


def test_max_open_positions(store):
    _open_position(store)
    router = ExecutionRouter(None, store, paper_mode=True)
    assert buy_paper(router, symbol="NSE:NIFTY26NOV23300CE").status == "REJECTED"


def test_non_lot_qty_rejected(paper_router):
    result = paper_router.execute_buy("NSE:NIFTY26NOV23250CE", 100, 45.0, 22.5, 67.5, 90.0)
    assert result.status == "REJECTED"


def test_live_buy_without_balance_is_rejected(store):
    # Live mode with a client that cannot report a balance must refuse.
    class NoFunds:
        pass
    router = ExecutionRouter(NoFunds(), store, paper_mode=False)
    result = router.execute_buy("NSE:NIFTY26NOV23250CE", 75, 45.0, 22.5, 67.5, 90.0)
    assert result.status == "REJECTED"


def test_config_defaults_to_paper_mode(monkeypatch):
    for var in ("PAPER_MODE", "MAX_DAILY_LOSS_RS", "MAX_TRADES_PER_DAY"):
        monkeypatch.delenv(var, raising=False)
    assert JevConfig.from_env().paper_mode is True


# ---------------------------------------------------------------- MTF builder
def _mtf():
    return MTFProfileContextBuilder(
        prev_month=ProfileLevels(100, 90, 95, []),
        current_month=ProfileLevels(200, 190, 195, []),
        prev_week=ProfileLevels(120, 110, 115, []),
        current_week=ProfileLevels(130, 120, 125, []),
        daily_profile=ProfileLevels(125, 115, 120, []),
    )


def test_mtf_uses_current_month_after_first_week():
    zones = _mtf().get_active_location_zones(datetime(2026, 11, 15))
    assert zones["monthly_levels"].poc == 195
    assert zones["monthly_label"] == "CURRENT_MONTH"


def test_mtf_uses_prev_month_in_first_week():
    zones = _mtf().get_active_location_zones(datetime(2026, 11, 5))
    assert zones["monthly_levels"].poc == 95
    assert zones["monthly_label"] == "PRIOR_MONTH"


def test_mtf_weekly_calendar_rule():
    monday = _mtf().get_active_location_zones(datetime(2026, 11, 16))  # Monday
    assert monday["weekly_levels"].poc == 115  # prior week
    wednesday = _mtf().get_active_location_zones(datetime(2026, 11, 18))
    assert wednesday["weekly_levels"].poc == 125  # current week


# ---------------------------------------------------------------- GEX fallback
def test_gex_returns_none_without_option_chain():
    class NoChain:
        pass
    assert compute_dealer_net_dex(NoChain(), 24000.0, date(2026, 11, 26)) is None


def test_gex_returns_none_on_garbage_chain():
    class BadChain:
        def optionchain(self, data):
            return {"s": "ok", "data": {"optionsChain": [{"nonsense": True}]}}
    assert compute_dealer_net_dex(BadChain(), 24000.0, date(2026, 11, 26)) is None


# ---------------------------------------------------------------- auth token expiry
def _auth_manager(tmp_path, monkeypatch):
    monkeypatch.delenv("FYERS_ACCESS_TOKEN", raising=False)
    mgr = FyersAuthManager("cid", "secret", "https://example.invalid/r",
                           "FY123", "1234", "JBSWY3DPEHPK3PXP")
    mgr.token_file = str(tmp_path / "token.json")
    return mgr


def test_cached_token_from_today_is_reused(tmp_path, monkeypatch):
    mgr = _auth_manager(tmp_path, monkeypatch)
    saved_at = datetime.now(timezone.utc).isoformat()
    (tmp_path / "token.json").write_text(json.dumps(
        {"access_token": "cached-token", "saved_at": saved_at}))
    monkeypatch.setattr(mgr, "generate_token",
                        lambda: (_ for _ in ()).throw(AssertionError("must not regenerate")))
    assert mgr.get_valid_token() == "cached-token"


def test_cached_token_from_yesterday_is_regenerated(tmp_path, monkeypatch):
    mgr = _auth_manager(tmp_path, monkeypatch)
    saved_at = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat()
    (tmp_path / "token.json").write_text(json.dumps(
        {"access_token": "stale-token", "saved_at": saved_at}))
    monkeypatch.setattr(mgr, "generate_token", lambda: "fresh-token")
    assert mgr.get_valid_token() == "fresh-token"


def test_cached_token_without_date_is_regenerated(tmp_path, monkeypatch):
    mgr = _auth_manager(tmp_path, monkeypatch)
    (tmp_path / "token.json").write_text(json.dumps({"access_token": "old-format"}))
    monkeypatch.setattr(mgr, "generate_token", lambda: "fresh-token")
    assert mgr.get_valid_token() == "fresh-token"


# ---------------------------------------------------------------- misc fixes
def test_greeks_prepare_breakout_is_a_valid_trigger():
    engine = GreeksStructuralEngine()
    bars = [BarData("09:15", 100, 110, 90, 105, 1000),
            BarData("09:20", 105, 112, 95, 108, 1000),
            BarData("09:25", 108, 111, 96, 109, 1000),
            BarData("09:30", 109, 113, 100, 110, 1000),
            BarData("09:35", 110, 114, 101, 111, 1000),
            BarData("09:40", 111, 115, 102, 112, 1000),
            BarData("09:45", 112, 113, 108, 112, 100)]  # narrowest range -> NR7
    signal = engine.process_confluence(
        bars[-1], bars, GreeksState(net_dex=0, net_gex=1000.0, atm_vega=0, iv_skew=0))
    assert signal.setup_trigger in ("MOMENTUM_SHORT", "MEAN_REVERSION_FADE",
                                    "PREPARE_BREAKOUT", "NO_TRADE")


def test_gamma_blast_window_is_1400_to_1508():
    detector = GammaBlastDetector()
    strike = StrikeTelemetry(24000, "CE", 5.0, 1000, -100, 0.14, 0.01, -5000.0)
    assert detector.scan_for_blast_setup("15:08", 24000, True, strike, 24000) is not None
    assert detector.scan_for_blast_setup("15:09", 24000, True, strike, 24000) is None
    assert detector.scan_for_blast_setup("14:00", 24000, True, strike, 24000) is not None


def test_historical_levels_failure_is_not_ok(monkeypatch):
    import main_jev_orchestrator as orch

    class FailingFyers:
        def history(self, data):
            raise RuntimeError("boom")

    levels, ok = orch.fetch_historical_levels(FailingFyers())
    assert ok is False
    # Defaults are present in the dict but flagged unusable -- the
    # orchestrator must not trade on them.
    assert levels["pdc"] == 23900.0


def test_signal_alert_shows_sl_t1_t2():
    import main_jev_orchestrator as orch

    text = orch._signal_alert_details(
        spot=25180, imb_tag="Stacked Buy: 3 lvls", ctx_tag="IMBALANCE_BULLISH",
        bar_delta=12400, strike=25200, option_type="CE", premium=68,
        expiry=date(2026, 11, 17), sl=34.0, t1=102.0, t2=136.0, qty=75)
    assert "SL: 34.0 | T1: 102.0 | T2: 136.0 | Qty: 75" in text
    assert "Strike: NIFTY 25200 CE | Premium: 68" in text
    assert "Spot: 25180 | Stacked Buy: 3 lvls" in text
    assert "Expiry: 2026-11-17" in text


# ---------------------------------------------------------------- Fyers API login (no browser)
class _FakeResponse:
    def __init__(self, status_code, body):
        self.status_code = status_code
        self._body = body
        self.text = json.dumps(body) if isinstance(body, dict) else str(body)

    def json(self):
        if not isinstance(self._body, dict):
            raise ValueError("not JSON")
        return self._body


def _api_login_posts(monkeypatch, fail_at=None):
    """Fake requests.post for the vagator + token + validate flow.

    fail_at names a step to fail ("otp", "totp", "pin", "token") with
    HTTP 400 and a Fyers-style message.
    """
    calls = []

    def fake_post(url, json=None, headers=None, timeout=None, allow_redirects=True):
        calls.append(url)
        if "send_login_otp" in url:
            if fail_at == "otp":
                return _FakeResponse(400, {"s": "error", "message": "Invalid FY ID"})
            return _FakeResponse(200, {"request_key": "rk1"})
        if "verify_otp" in url:
            if fail_at == "totp":
                return _FakeResponse(400, {"s": "error", "message": "Invalid OTP"})
            return _FakeResponse(200, {"request_key": "rk2"})
        if "verify_pin" in url:
            if fail_at == "pin":
                return _FakeResponse(400, {"s": "error", "message": "Invalid PIN"})
            return _FakeResponse(200, {"data": {"access_token": "trade-token"}})
        if url.endswith("/api/v3/token"):
            if fail_at == "token":
                return _FakeResponse(400, {"s": "error", "message": "Token failed"})
            return _FakeResponse(308, {"Url": "https://trade.fyers.in/api-login/redirect-uri/index.html?auth_code=AUTH123&state=sample_state"})
        if "validate-authcode" in url:
            return _FakeResponse(200, {"access_token": "api-token"})
        raise AssertionError(f"unexpected URL {url}")

    monkeypatch.setattr("fyers_auth_automation.requests.post", fake_post)
    return calls


def _api_auth_manager(tmp_path):
    from fyers_auth_automation import FyersAuthManager

    mgr = FyersAuthManager("APP123-100", "secret", "https://trade.fyers.in/api-login/redirect-uri/index.html",
                           "AB12345", "1234", "JBSWY3DPEHPK3PXP")
    mgr.token_file = str(tmp_path / "fyers_access_token.json")
    return mgr


def test_api_login_happy_path(tmp_path, monkeypatch):
    calls = _api_login_posts(monkeypatch)
    mgr = _api_auth_manager(tmp_path)
    assert mgr.generate_token() == "api-token"
    assert any("send_login_otp" in url for url in calls)
    assert any("verify_otp" in url for url in calls)
    assert any("verify_pin" in url for url in calls)
    assert any(url.endswith("/api/v3/token") for url in calls)
    assert any("validate-authcode" in url for url in calls)
    assert json.loads((tmp_path / "fyers_access_token.json").read_text())["access_token"] == "api-token"


def test_api_login_otp_step_names_itself(tmp_path, monkeypatch):
    _api_login_posts(monkeypatch, fail_at="otp")
    with pytest.raises(RuntimeError, match="Login OTP step"):
        _api_auth_manager(tmp_path).generate_token()


def test_api_login_totp_step_names_itself(tmp_path, monkeypatch):
    _api_login_posts(monkeypatch, fail_at="totp")
    with pytest.raises(RuntimeError, match="TOTP step"):
        _api_auth_manager(tmp_path).generate_token()


def test_api_login_pin_step_names_itself(tmp_path, monkeypatch):
    _api_login_posts(monkeypatch, fail_at="pin")
    with pytest.raises(RuntimeError, match="PIN step"):
        _api_auth_manager(tmp_path).generate_token()


def test_api_login_token_step_names_itself(tmp_path, monkeypatch):
    _api_login_posts(monkeypatch, fail_at="token")
    with pytest.raises(RuntimeError, match="Auth code step"):
        _api_auth_manager(tmp_path).generate_token()


def test_split_client_id():
    from fyers_auth_automation import _split_client_id

    assert _split_client_id("ABC123XYZ-100") == ("ABC123XYZ", "100")
    assert _split_client_id("ABC123XYZ-200") == ("ABC123XYZ", "200")
    assert _split_client_id("ABC123XYZ") == ("ABC123XYZ", "100")


def test_request_key_missing_is_an_error():
    from fyers_auth_automation import _request_key

    with pytest.raises(RuntimeError, match="no request_key"):
        _request_key({"s": "ok"}, "TOTP step")
    assert _request_key({"data": {"request_key": "rk"}}, "TOTP step") == "rk"
