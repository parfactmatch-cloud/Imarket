"""
main_jev_orchestrator.py
Production Entry Point linking Ingestion, Auction Footprint, Multi-Timeframe Context, and Execution.

Fixes applied over v3.6 (see README "What was fixed"):
- 5-minute bars are accumulated and passed to the lag engine, so its
  trend/boundary checks can actually fire (previously it was always
  called with an empty history and could only return HOLD).
- The authenticated Fyers client is wired into the ExecutionRouter
  after login (previously it stayed None, so live execution was
  unreachable).
- Orders are priced off the *option premium* fetched from Fyers, not
  the NIFTY spot; stop-loss/targets are premium levels.
- Dealer gamma comes from IndiaGEXEngine on the live option chain,
  not a hardcoded constant; if the chain cannot be fetched, no
  signal is generated.
- Option symbols use the computed weekly (Tuesday) expiry, not a
  hardcoded "26NOV".
- A position monitor exits on stop-loss / targets and squares off
  before the close; /panic exits at the current market price.
- Risk controls (max daily loss, max trades/day, one open position,
  cash/balance checks) are enforced by the router.
- If historical levels cannot be fetched, trading is paused and an
  alert is sent instead of trading on stale hardcoded defaults.
- Shutdown is graceful: the state store is checkpointed and closed
  instead of os._exit(0).
"""

import logging
import os
import threading
import time
from datetime import datetime, timedelta, timezone

from fyers_apiv3 import fyersModel
from fyers_apiv3.FyersWebsocket import data_ws

from config import (
    BAR_SECONDS,
    EARLY_TRIGGER_SECONDS,
    MONITOR_CHECK_SECONDS,
    SL_FRACTION,
    T1_FRACTION,
    T2_FRACTION,
    JevConfig,
    setup_logging,
)
from engine_state_store import EngineStateStore
from execution_broker_interface import ExecutionRouter
from expiry import next_weekly_expiry, option_symbol
from fyers_auth_automation import FyersAuthManager
from lag_neutralized_engine import LagNeutralizedEngine, FastBar
from advanced_auction_footprint_engine import AdvancedAuctionFootprintEngine
from market_data import compute_gex_snapshot, get_option_quote
from position_monitor import PositionMonitor
from telegram_remote_controller import TelegramController

try:
    from mtf_profile_context_builder import MTFProfileContextBuilder, ProfileLevels
    MTF_AVAILABLE = True
except ImportError:
    MTF_AVAILABLE = False

logger = logging.getLogger(__name__)

# IST Timezone (UTC + 5:30)
IST = timezone(timedelta(hours=5, minutes=30))


def get_ist_now() -> datetime:
    return datetime.now(IST)


def sanitize_tg_text(text: str) -> str:
    """Removes Markdown characters that break Telegram API entity parsing."""
    return str(text).replace("_", " ").replace("*", "").replace("`", "")


def _fmt_level(value) -> str:
    """Format a price level for Telegram: whole strikes as ints, else 2dp."""
    if value is None:
        return "n/a"
    value = float(value)
    return f"{value:.0f}" if value == int(value) else f"{value:.2f}"


def _signal_alert_details(*, spot, imb_tag, ctx_tag, bar_delta, strike,
                          option_type, premium, expiry, sl, t1, t2, qty,
                          call_wall=None, put_wall=None, week_poc=None):
    """Details text for the NIFTY BUY/SELL SIGNAL Telegram alert.

    Includes the stop-loss and targets, not just the premium, so the
    risk on the trade is visible in the message itself, plus the call
    wall, put wall and weekly POC the engine checked before signalling
    -- so the levels can be cross-checked against the live chain.
    Missing values print as "n/a" rather than being guessed.
    """
    return (
        f"Spot: {spot} | {imb_tag}\nContext: {ctx_tag} | "
        f"Bar Delta: {bar_delta}\nStrike: NIFTY {strike} {option_type} "
        f"| Premium: {premium} | Expiry: {expiry.isoformat()}\n"
        f"SL: {sl} | T1: {t1} | T2: {t2} | Qty: {qty}\n"
        f"Walls: C {_fmt_level(call_wall)} / P {_fmt_level(put_wall)} "
        f"| Week POC: {_fmt_level(week_poc)}"
    )


def _profile_from_candles(candles):
    """Crude value-area proxy from a candle window (see README limits).

    This is NOT a volume-derived value area: VAH/VAL are the high/low
    inset by 15% of the range and POC is the midpoint. It is kept only
    because the MTF builder consumes it; do not treat it as a real
    volume profile.
    """
    high = max(float(c[2]) for c in candles)
    low = min(float(c[3]) for c in candles)
    rng = high - low
    return {
        "vah": round(high - (0.15 * rng), 2),
        "val": round(low + (0.15 * rng), 2),
        "poc": round((high + low) / 2.0, 2),
    }


def fetch_historical_levels(fyers_client):
    """
    Fetches actual PDH, PDL, PDC and Weekly/Monthly profile levels.

    Returns (levels, ok). ``ok`` is False when the history call fails
    or returns too few candles -- callers must NOT trade on the
    defaults in that case (see start_system).
    """
    levels = {
        "pdh": 24000.0, "pdl": 23800.0, "pdc": 23900.0,
        "week_vah": 24100.0, "week_val": 23750.0, "week_poc": 23920.0,
        "prev_week_vah": 24100.0, "prev_week_val": 23750.0, "prev_week_poc": 23920.0,
        "month_vah": 24300.0, "month_val": 23600.0, "month_poc": 23950.0,
        "prev_month_vah": 24300.0, "prev_month_val": 23600.0, "prev_month_poc": 23950.0,
    }
    try:
        today = get_ist_now()
        range_from = (today - timedelta(days=42)).strftime("%Y-%m-%d")
        range_to = today.strftime("%Y-%m-%d")
        data = {
            "symbol": "NSE:NIFTY50-INDEX",
            "resolution": "D",
            "date_format": "1",
            "range_from": range_from,
            "range_to": range_to,
            "cont_flag": "1",
        }
        response = fyers_client.history(data=data)
        candles = response.get("candles", [])
        if len(candles) < 2:
            logger.error("Historical fetch returned only %d candles; levels unusable", len(candles))
            return levels, False

        prev_candle = candles[-2]
        levels["pdh"] = float(prev_candle[2])
        levels["pdl"] = float(prev_candle[3])
        levels["pdc"] = float(prev_candle[4])

        recent_week = candles[-6:-1] if len(candles) >= 6 else candles[:-1]
        prev_week = candles[-11:-6] if len(candles) >= 11 else recent_week
        recent_month = candles[-21:-1] if len(candles) >= 21 else candles[:-1]
        prev_month = candles[-42:-21] if len(candles) >= 42 else recent_month

        week = _profile_from_candles(recent_week)
        pweek = _profile_from_candles(prev_week)
        month = _profile_from_candles(recent_month)
        pmonth = _profile_from_candles(prev_month)
        for prefix, prof in (("week", week), ("prev_week", pweek),
                             ("month", month), ("prev_month", pmonth)):
            levels[f"{prefix}_vah"] = prof["vah"]
            levels[f"{prefix}_val"] = prof["val"]
            levels[f"{prefix}_poc"] = prof["poc"]

        logger.info("[Levels] Real Nifty Levels -> PDH: %s, PDL: %s, Week POC: %s",
                    levels["pdh"], levels["pdl"], levels["week_poc"])
        return levels, True
    except Exception as exc:
        logger.error("[Levels] Historical fetch failed: %s", exc)
        return levels, False


def fetch_5m_history(fyers_client):
    """Seed today's completed 5-minute bars from Fyers history.

    Returns a list of FastBar (possibly empty). An empty list is a
    safe outcome: the lag engine then returns HOLD until 12 bars have
    accumulated live.
    """
    bars = []
    try:
        today = get_ist_now().strftime("%Y-%m-%d")
        response = fyers_client.history(data={
            "symbol": "NSE:NIFTY50-INDEX",
            "resolution": "5",
            "date_format": "1",
            "range_from": today,
            "range_to": today,
            "cont_flag": "1",
        })
        for candle in response.get("candles", []):
            ts, open_p, high_p, low_p, close_p, volume = candle[:6]
            ts_str = datetime.fromtimestamp(ts, tz=IST).strftime("%H:%M")
            bars.append(FastBar(ts_str, float(open_p), float(high_p), float(low_p),
                                float(close_p), int(volume), BAR_SECONDS))
        logger.info("Seeded %d completed 5-minute bars from history", len(bars))
    except Exception as exc:
        logger.warning("5-minute history seed failed (starting empty): %s", exc)
    return bars


def _wait_for_shutdown(shutdown_event, poll_seconds=5):
    """Block until the market-close watcher signals shutdown.

    ``FyersDataSocket.connect()`` returns immediately after spawning the
    socket thread, so without this wait the main thread falls through to
    the ``finally`` that closes the state store — about a second after
    starting — while ticks are still arriving. Waiting here keeps the
    session (and the database) alive until 15:30 IST.
    """
    while not shutdown_event.is_set():
        shutdown_event.wait(poll_seconds)


def _close_fyers_socket(ws):
    """Close a FyersDataSocket with the method fyers-apiv3 actually has.

    The socket has no ``close`` method — its shutdown method is
    ``close_connection`` (fyers-apiv3 3.2.1), which closes the WebSocket,
    joins the socket/message/ping threads and stops the keep-alive loop.
    The old code looked up ``close``, found nothing, and silently closed
    nothing, so the job never exited. Falls back to ``close`` for other
    socket objects, and never raises.
    """
    if ws is None:
        return
    for name in ("close_connection", "close"):
        close_fn = getattr(ws, name, None)
        if callable(close_fn):
            try:
                close_fn()
            except Exception as exc:
                logger.warning("WebSocket close failed: %s", exc)
            return


def _run_streaming_session(ws, shutdown_event, state_store):
    """Run the live socket session until the close watcher shuts it down.

    ``connect()`` spawns the socket thread and returns at once; the wait
    below is what keeps the process (and the state store) alive for the
    session instead of ending about a second after starting.
    """
    # The SDK only marks its socket thread a daemon when write_to_file is
    # set, which we do not use — so mark it daemon here. Then the process
    # can exit even if the socket's own close ever fails to stop it.
    ws.background_flag = True
    try:
        ws.connect()
        _wait_for_shutdown(shutdown_event)
    finally:
        # Graceful shutdown: flush/close the DB instead of os._exit(0).
        shutdown_event.set()
        state_store.close()
        logger.info(">>> Jev Engine lifecycle finished.")


def start_system():
    config = JevConfig.from_env()
    setup_logging(config.log_level)
    logger.info("=" * 60)
    logger.info(">>> Jev Engine (Auction + Footprint + MTF Live) Starting...")
    logger.info("=" * 60)
    logger.info("Mode: %s", "Paper Mode" if config.paper_mode else "Live Real Funds")

    # 1. Fetch Credentials from Environment Variables / GitHub Secrets
    fyers_client_id = os.environ.get("FYERS_APP_ID", "").strip()
    fyers_secret_key = os.environ.get("FYERS_SECRET_KEY", "").strip()
    fyers_id = os.environ.get("FYERS_ID", "").strip()
    fyers_pin = os.environ.get("FYERS_PIN", "").strip()
    fyers_totp_key = os.environ.get("FYERS_TOTP_KEY", "").strip()

    telegram_bot_token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    telegram_chat_id = os.environ.get("TELEGRAM_CHAT_ID", "").strip()

    # 2. Initialize State Store & Execution Router
    state_store = EngineStateStore("engine_state.db")
    router = ExecutionRouter(
        fyers_client=None, state_store=state_store,
        paper_mode=config.paper_mode, config=config,
    )
    shutdown_event = threading.Event()

    # 3. Setup Telegram Controller
    tg = TelegramController(
        bot_token=telegram_bot_token,
        chat_id=telegram_chat_id,
        state_store=state_store,
        router=router,
    )
    if tg.bot:
        tg_thread = threading.Thread(target=tg.run_polling, daemon=True)
        tg_thread.start()

    mode_label = "Paper Mode" if config.paper_mode else "Live Real Funds"

    # 4. Generate/Retrieve Daily Fyers Access Token
    token = os.environ.get("FYERS_ACCESS_TOKEN", "").strip()
    if not token and fyers_client_id and fyers_totp_key:
        try:
            auth_mgr = FyersAuthManager(
                client_id=fyers_client_id,
                secret_key=fyers_secret_key,
                redirect_uri="https://trade.fyers.in/api-login/redirect-uri/index.html",
                fyers_id=fyers_id,
                pin=fyers_pin,
                totp_key=fyers_totp_key,
            )
            token = auth_mgr.get_valid_token()
            logger.info("[Auth] Fyers access token secured successfully.")
        except Exception as exc:
            clean_err = sanitize_tg_text(str(exc))[:140]
            logger.error("[Auth Error] Failed to generate token: %s", exc)
            tg.send_alert("Auth Error", f"Token generation failed: {clean_err}")

    # 5. Connect Live Stream if Token is Available
    if token:
        fyers = fyersModel.FyersModel(client_id=fyers_client_id, token=token, is_async=False, log_path="")
        # Wire the authenticated client into the router (fix: it used
        # to stay None forever, making live execution unreachable).
        router.set_fyers_client(fyers)
        quote_fn = lambda sym: get_option_quote(fyers, sym)  # noqa: E731
        router.set_quote_fn(quote_fn)

        levels, levels_ok = fetch_historical_levels(fyers)
        if not levels_ok:
            # Never trade on stale hardcoded defaults. Pause and alert.
            state_store.set_system_flag("SYSTEM_PAUSED", "1")
            tg.send_alert(
                "Levels Unavailable -- Trading Paused",
                "Historical levels could not be fetched from Fyers. "
                "No trades will be placed on stale defaults.",
            )
            logger.error("Historical levels unavailable; trading paused, shutting down")
            state_store.close()
            return
        pdh, pdl, pdc = levels["pdh"], levels["pdl"], levels["pdc"]

        mtf_builder = None
        if MTF_AVAILABLE:
            mtf_builder = MTFProfileContextBuilder(
                prev_month=ProfileLevels(levels["prev_month_vah"], levels["prev_month_val"], levels["prev_month_poc"], []),
                current_month=ProfileLevels(levels["month_vah"], levels["month_val"], levels["month_poc"], []),
                prev_week=ProfileLevels(levels["prev_week_vah"], levels["prev_week_val"], levels["prev_week_poc"], []),
                current_week=ProfileLevels(levels["week_vah"], levels["week_val"], levels["week_poc"], []),
                daily_profile=ProfileLevels(pdh, pdl, pdc, []),
            )

        tg.send_alert(
            "System Online (Live Feed)",
            f"Jev Engine ({mode_label})\nNifty PDH: {pdh} | PDL: {pdl}\n"
            f"Week POC: {levels['week_poc']} | Status: Live Scanning",
        )

        lag_engine = LagNeutralizedEngine(pdh=pdh, pdl=pdl, pdc=pdc)
        fp_engine = AdvancedAuctionFootprintEngine(tick_size=0.05, imbalance_ratio=3.0, stacked_count=3)
        monitor = PositionMonitor(state_store, router, quote_fn=quote_fn, config=config)
        expiry = next_weekly_expiry(now=get_ist_now(), override=config.expiry_override)
        logger.info("Weekly expiry in use: %s", expiry.isoformat())

        bar_ticks = []
        bars_5m_history = fetch_5m_history(fyers)
        bar_start_time = time.time()
        last_heartbeat_time = time.time()
        last_monitor_time = time.time()
        latest_ltp = pdc
        early_triggered_this_bar = False
        ws_holder = {"ws": None}
        gex_cache = {"value": None, "at": 0.0}  # caches the GEXSnapshot

        def get_gex_snapshot(spot: float):
            """Cached GEX snapshot; None when the chain cannot be fetched."""
            if time.time() - gex_cache["at"] < BAR_SECONDS and gex_cache["at"] != 0.0:
                return gex_cache["value"]
            value = compute_gex_snapshot(fyers, spot, expiry)
            gex_cache["value"] = value
            gex_cache["at"] = time.time()
            if value is None:
                logger.warning("Dealer gamma unavailable; signals will be HOLD until it is")
            return value

        def get_dealer_net_dex(spot: float):
            """Cached GEX; None when the chain cannot be fetched (no signal)."""
            snapshot = get_gex_snapshot(spot)
            return float(snapshot.net_gex) if snapshot is not None else None

        def finish_bar(fast_bar: FastBar):
            nonlocal bar_ticks, bar_start_time, early_triggered_this_bar
            bars_5m_history.append(fast_bar)
            if len(bars_5m_history) > 100:
                del bars_5m_history[:-100]
            bar_ticks.clear()
            bar_start_time = time.time()
            early_triggered_this_bar = False

        def _premium_levels(premium: float):
            sl = round(premium * SL_FRACTION, 2)
            t1 = round(premium * T1_FRACTION, 2)
            t2 = round(premium * T2_FRACTION, 2)
            return sl, t1, t2

        def _buy_option(option_type: str, fast_bar: FastBar, fp_bar, mtf_context, tag: str):
            nonlocal early_triggered_this_bar
            atm_strike = round(fast_bar.close / 50) * 50
            symbol = option_symbol("NIFTY", expiry, atm_strike, option_type)
            # Price off the OPTION premium, never the NIFTY spot.
            premium = get_option_quote(fyers, symbol)
            if premium is None:
                logger.error("No option quote for %s; trade skipped", symbol)
                tg.send_alert("Quote Unavailable", f"No quote for {symbol}; trade skipped.")
                return
            sl, t1, t2 = _premium_levels(premium)
            imb_tag = tag
            ctx_tag = mtf_context.get("bias", "BREAKOUT")
            # Same cached snapshot the signal decision used, so the walls
            # printed are the walls the trade was judged against.
            snapshot = get_gex_snapshot(fast_bar.close)
            tg.send_alert(
                f"NIFTY {'BUY' if option_type == 'CE' else 'SELL'} SIGNAL",
                _signal_alert_details(
                    spot=fast_bar.close, imb_tag=imb_tag, ctx_tag=ctx_tag,
                    bar_delta=fp_bar.bar_delta, strike=atm_strike,
                    option_type=option_type, premium=premium,
                    expiry=expiry, sl=sl, t1=t1, t2=t2,
                    qty=config.lot_size,
                    call_wall=snapshot.call_wall if snapshot else None,
                    put_wall=snapshot.put_wall if snapshot else None,
                    week_poc=mtf_context.get("poc"),
                ),
            )
            result = router.execute_buy(
                symbol=symbol, qty=config.lot_size, ltp=premium, sl=sl, t1=t1, t2=t2,
            )
            if result.status == "REJECTED":
                logger.warning("Buy rejected for %s: %s", symbol, result.reason)
                tg.send_alert("Order Rejected", f"{symbol}: {sanitize_tg_text(result.reason)}")
            else:
                early_triggered_this_bar = True
                logger.info("Buy %s x%d @ premium %.2f status=%s",
                            symbol, config.lot_size, premium, result.status)

        def evaluate_and_execute(elapsed_sec: int, is_early: bool):
            nonlocal bar_ticks, bar_start_time, early_triggered_this_bar

            if not bar_ticks:
                return

            prices = [t["ltp"] for t in bar_ticks]
            total_vol = sum(t["qty"] for t in bar_ticks)
            ts_str = get_ist_now().strftime("%H:%M")

            fast_bar = FastBar(
                timestamp=ts_str,
                open=prices[0], high=max(prices), low=min(prices),
                close=prices[-1], volume=total_vol, elapsed_seconds=elapsed_sec,
            )
            fp_bar = fp_engine.evaluate_bar(
                timestamp=ts_str, open_p=fast_bar.open, high_p=fast_bar.high,
                low_p=fast_bar.low, close_p=fast_bar.close, volume=total_vol,
            )

            if state_store.get_system_flag("SYSTEM_PAUSED", "0") == "1":
                if not is_early:
                    finish_bar(fast_bar)
                return

            # Check MTF Context & Chop Filter
            mtf_context = {}
            if mtf_builder:
                mtf_context = mtf_builder.evaluate_auction_context(fast_bar.close, get_ist_now())
                if mtf_context.get("is_chop_risk"):
                    logger.info("[Veto] Price %s is inside Weekly POC Chop Zone. Trade skipped.",
                                fast_bar.close)
                    if not is_early:
                        finish_bar(fast_bar)
                    return

            decision = lag_engine.evaluate_lag_free_execution(
                current_5m=fast_bar,
                bars_5m_history=bars_5m_history,
                dealer_net_dex=get_dealer_net_dex(fast_bar.close),
                cvd_delta=fp_bar.cumulative_delta,
            )

            # Bullish Breakout + Footprint Confirmation
            if decision["signal"] == "EARLY_BUY" and not early_triggered_this_bar:
                if fp_bar.is_buying_absorption:
                    logger.info("[Veto] Buying Absorption at %s. Fake breakout blocked.", fast_bar.high)
                elif fp_engine.is_single_time_framing(3, "DOWN"):
                    logger.info("[Veto] Active Single Time-Framing DOWN. Counter-buy blocked.")
                else:
                    imb_tag = (f"Stacked Buy: {len(fp_bar.stacked_buy_imbalances)} lvls"
                               if fp_bar.stacked_buy_imbalances else "CVD Confirmed")
                    _buy_option("CE", fast_bar, fp_bar, mtf_context, imb_tag)

            # Bearish Breakdown + Footprint Confirmation
            elif decision["signal"] == "EARLY_SELL" and not early_triggered_this_bar:
                if fp_bar.is_selling_absorption:
                    logger.info("[Veto] Selling Absorption at %s. Fake breakdown blocked.", fast_bar.low)
                elif fp_engine.is_single_time_framing(3, "UP"):
                    logger.info("[Veto] Active Single Time-Framing UP. Counter-sell blocked.")
                else:
                    imb_tag = (f"Stacked Sell: {len(fp_bar.stacked_sell_imbalances)} lvls"
                               if fp_bar.stacked_sell_imbalances else "CVD Confirmed")
                    _buy_option("PE", fast_bar, fp_bar, mtf_context, imb_tag)

            if not is_early:
                finish_bar(fast_bar)

        def on_message(message):
            nonlocal latest_ltp, last_heartbeat_time, last_monitor_time

            if not isinstance(message, dict):
                return
            ltp = message.get("ltp")
            if not ltp:
                return
            ltp = float(ltp)
            latest_ltp = ltp
            qty = int(message.get("last_traded_qty", 1))
            bid = float(message.get("bid", ltp))
            ask = float(message.get("ask", ltp))

            fp_engine.process_tick(ltp=ltp, qty=qty, best_bid=bid, best_ask=ask)
            bar_ticks.append({"ltp": ltp, "qty": qty})

            now_ts = time.time()
            elapsed = int(now_ts - bar_start_time)

            # Exit management: re-price open positions about once a minute.
            if (now_ts - last_monitor_time) >= MONITOR_CHECK_SECONDS:
                last_monitor_time = now_ts
                for action in monitor.check_positions():
                    tg.send_alert("Position Update", sanitize_tg_text(action))

            # 30-Minute Live Heartbeat Telemetry
            if (now_ts - last_heartbeat_time) >= 1800:
                last_heartbeat_time = now_ts
                tg.send_alert(
                    "Engine Heartbeat",
                    f"Nifty Spot: {latest_ltp}\nRunning CVD: {fp_engine.running_cvd}\n"
                    f"Status: Active & Scanning",
                )

            if elapsed >= BAR_SECONDS:
                evaluate_and_execute(elapsed, is_early=False)
            elif elapsed >= EARLY_TRIGGER_SECONDS and not early_triggered_this_bar:
                evaluate_and_execute(elapsed, is_early=True)

        def on_error(msg):
            logger.error("[WS Error] %s", msg)

        def on_close(msg):
            logger.info("[WS Closed] %s", msg)

        def on_open():
            logger.info("[WS Connected] Subscribing to NSE:NIFTY50-INDEX...")
            ws = ws_holder["ws"]
            if ws is not None:
                ws.subscribe(symbols=["NSE:NIFTY50-INDEX"], data_type="symbolUpdate")
                ws.keep_running()

        def _parse_hhmm(value: str):
            hour, minute = value.split(":")
            return int(hour), int(minute)

        # Market Close Watcher: square off, then shut down gracefully.
        def market_close_watcher():
            sq_hour, sq_min = _parse_hhmm(config.square_off_time)
            cl_hour, cl_min = _parse_hhmm(config.market_close_time)
            squared_off = False
            while not shutdown_event.is_set():
                now_ist = get_ist_now()
                if not squared_off and (now_ist.hour, now_ist.minute) >= (sq_hour, sq_min):
                    squared_off = True
                    actions = monitor.square_off_all()
                    tg.send_alert(
                        "End-of-Day Square-Off",
                        "All open positions closed at market.\n" + "\n".join(actions)
                        if actions else "No open positions to square off.",
                    )
                    logger.info("End-of-day square-off done: %s", actions)
                if (now_ist.hour, now_ist.minute) >= (cl_hour, cl_min):
                    tg.send_alert(
                        "Session Closed",
                        f"Market closed at 3:30 PM IST.\nFinal Nifty Spot: {latest_ltp} | "
                        f"Session CVD: {fp_engine.running_cvd}",
                    )
                    # Close the socket BEFORE signalling shutdown: no more
                    # ticks may arrive once the main thread closes the
                    # state store. (The old code set the event first and
                    # then called ws.close(), which does not exist on
                    # FyersDataSocket, so the database was closed under a
                    # live socket and nothing ever shut the job down.)
                    try:
                        _close_fyers_socket(ws_holder["ws"])
                    finally:
                        shutdown_event.set()
                    break
                shutdown_event.wait(30)
            # If the loop ended without the close block above (shutdown was
            # requested another way), still try to close the socket. A
            # second close is a no-op: close_connection returns at once
            # once the socket is already closed.
            _close_fyers_socket(ws_holder["ws"])

        threading.Thread(target=market_close_watcher, daemon=True).start()

        logger.info("[Streaming] Connecting to live Fyers WebSocket feed...")
        full_token = f"{fyers_client_id}:{token}"
        ws = data_ws.FyersDataSocket(
            access_token=full_token,
            log_path="",
            litemode=False,
            write_to_file=False,
            on_connect=on_open,
            on_close=on_close,
            on_error=on_error,
            on_message=on_message,
        )
        ws_holder["ws"] = ws
        _run_streaming_session(ws, shutdown_event, state_store)

    else:
        # No live token: do NOT trade. The old fallback loop ran mock
        # bars and could send genuine-looking Telegram "Signal Triggered"
        # alerts and paper trades from fake data. Now it only waits,
        # alerting once that the engine is idle.
        tg.send_alert(
            "System Online (No Live Token)",
            f"Jev Engine started in {mode_label} with no Fyers token. "
            "Engine is idle: no signals, no trades.",
        )
        logger.warning("[Fallback] No live token; engine idle (no mock trading)")
        try:
            max_iterations = int(os.environ.get("MAX_ITERATIONS", "10"))
            for _ in range(max_iterations):
                if shutdown_event.is_set():
                    break
                time.sleep(1)
        except KeyboardInterrupt:
            tg.send_alert("System Shutdown", "Jev Engine manually terminated.")
        finally:
            state_store.close()
            logger.info(">>> Jev Engine lifecycle finished.")


if __name__ == "__main__":
    start_system()
