"""
close_jev_session.py
Second close job: finish what the 6-hour GitHub Actions cap kills.

Why this exists: the scheduled engine job starts at 09:15 IST and
GitHub-hosted runners are hard-capped at 6 hours, so the job is killed
at ~15:15 IST — before square-off (15:20), the close (15:31) and the
Post-Market Playbook. Raising ``timeout-minutes`` cannot fix it: GitHub
ignores any value above the runner's own 6-hour limit. So the workflow
runs this script as a second job (``needs: run-engine``,
``if: always()``) right after the engine job ends, and this script
does the close sequence the engine never reached:

  1. Read the engine's last snapshot from the Jev Terminal and take
     the open paper positions from it. The engine's SQLite state store
     is fresh on every Actions runner, so the terminal snapshot is the
     only place the two jobs share state.
  2. Square the open positions off at current market prices.
  3. Wait until the market close and send the Post-Market Playbook.
  4. Push one final snapshot (with no open positions) to the terminal.

Idempotency (the whole point of the design): the script sends nothing
at all when the snapshot has no open positions. That covers a manual
run that already closed cleanly (its final push carries no positions),
a run that died before trading, and a re-run of this job itself (its
own final push carries no positions), so a playbook can never be sent
twice. The only day this job stays silent is a scheduled run that was
killed with no open positions — a flat day gets no playbook from
either job; that is the accepted cost of never duplicating one.

Paper mode is honoured exactly like the engine: PAPER_MODE defaults
to True and live orders only happen when the operator explicitly sets
PAPER_MODE=False.
"""

import json
import logging
import os
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone

from fyers_apiv3 import fyersModel

from config import UNDERLYING_SYMBOL, JevConfig, setup_logging
from engine_state_store import EngineStateStore, PositionState
from execution_broker_interface import ExecutionRouter
from expiry import next_weekly_expiry
from fyers_auth_automation import FyersAuthManager
from lag_neutralized_engine import FastBar
from market_data import compute_gex_snapshot, get_option_quote
from position_monitor import PositionMonitor
from telegram_remote_controller import TelegramController
from terminal_push import build_engine_payload, push_engine_snapshot

logger = logging.getLogger(__name__)

IST = timezone(timedelta(hours=5, minutes=30))
TERMINAL_FETCH_TIMEOUT_SECONDS = 10
# The playbook waits for the real close only when the close is near.
# If the engine job died hours earlier with a position open, holding a
# runner until 15:31 for a playbook is not worth it: the square-off
# card has already told the user, so the playbook is skipped instead.
MAX_PLAYBOOK_WAIT_MINUTES = 90


def get_ist_now() -> datetime:
    return datetime.now(IST)


def _parse_hhmm(value: str):
    hour, minute = value.split(":")
    return int(hour), int(minute)


def _fmt_level(value) -> str:
    """Format a price level for Telegram: whole strikes as ints, else 2dp."""
    if value is None:
        return "n/a"
    value = float(value)
    return f"{value:.0f}" if value == int(value) else f"{value:.2f}"


def fetch_terminal_engine(url: str,
                          timeout: int = TERMINAL_FETCH_TIMEOUT_SECONDS):
    """GET <url>/api/terminal and return its ``engine`` record.

    Returns None when the terminal has no engine record yet. Raises
    RuntimeError on a transport, HTTP or JSON failure — the caller
    treats that as a failed run, not as "nothing to close", so a dead
    terminal never looks like a quiet day.
    """
    if not url:
        raise RuntimeError("no terminal URL configured (JEV_TERMINAL_URL)")
    try:
        request = urllib.request.Request(url.rstrip("/") + "/api/terminal",
                                         method="GET")
        with urllib.request.urlopen(request, timeout=timeout) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except Exception as exc:
        raise RuntimeError(f"terminal fetch failed: {exc}") from exc
    if not isinstance(data, dict):
        raise RuntimeError("terminal returned a non-JSON-object response")
    engine = data.get("engine")
    if engine is None:
        return None
    if not isinstance(engine, dict):
        raise RuntimeError("terminal engine record is not an object")
    return engine


def open_positions_from_engine(engine: dict) -> dict:
    """Open positions from a terminal engine record.

    The engine pushes its positions as serialised PositionState dicts
    (see terminal_push.build_engine_payload). Rows already CLOSED are
    skipped, and unknown keys are dropped so records written by other
    versions still load — the same pattern as
    EngineStateStore.load_open_positions.
    """
    positions = {}
    raw = engine.get("positions") if isinstance(engine, dict) else None
    if not isinstance(raw, list):
        return positions
    known = {f for f in PositionState.__dataclass_fields__}
    for entry in raw:
        if not isinstance(entry, dict):
            continue
        if entry.get("status") == "CLOSED":
            continue
        try:
            pos = PositionState(
                **{k: v for k, v in entry.items() if k in known})
        except TypeError:
            continue
        positions[pos.symbol] = pos
    return positions


def build_playbook_rows(*, close, prev_close, today_high, today_low,
                        gex, expiry) -> list:
    """Rows for the Post-Market Playbook card.

    This mirrors the card ``_send_playbook_card`` in
    main_jev_orchestrator.py row for row — same labels, same values —
    so the user gets the same card whichever job sends it. The
    orchestrator's builder is a closure inside ``start_system`` and
    cannot be imported, so it is duplicated here as a pure function
    (which also makes it unit-testable).
    """
    change = float(close) - float(prev_close)
    bias = "Bullish" if float(close) >= float(prev_close) else "Bearish"
    if gex is not None:
        walls = f"{gex.call_wall:,.0f} / {gex.put_wall:,.0f}"
        pain = f"{gex.max_pain:,.0f} / {gex.pcr:.2f}"
        flip = f"{gex.gamma_flip_level:,.2f} / {gex.market_regime}"
    else:
        walls = pain = flip = "n/a — GEX unavailable"
    return [
        ("NIFTY Close", f"{float(close):,.2f} ({change:+,.2f} vs prev close)"),
        ("Day range", f"{float(today_low):,.2f} - {float(today_high):,.2f}"),
        ("Call wall / Put wall", walls),
        ("Max pain / PCR", pain),
        ("Gamma flip / Regime", flip),
        ("Tomorrow PDH / PDL", f"{float(today_high):,.2f} / {float(today_low):,.2f}"),
        ("Tomorrow PDC / Bias", f"{float(close):,.2f} / {bias}"),
        ("Next expiry", expiry.strftime("%a %d %b %Y")),
    ]


def _fetch_5m_bars(fyers_client, trading_date) -> list:
    """Today's completed 5-minute NIFTY bars from Fyers history.

    Same request as the orchestrator's ``fetch_5m_history``; an empty
    list on failure — callers fall back to the snapshot's levels.
    """
    bars = []
    try:
        day = trading_date.strftime("%Y-%m-%d")
        response = fyers_client.history(data={
            "symbol": UNDERLYING_SYMBOL,
            "resolution": "5",
            "date_format": "1",
            "range_from": day,
            "range_to": day,
            "cont_flag": "1",
        })
        for candle in response.get("candles", []):
            ts, open_p, high_p, low_p, close_p, volume = candle[:6]
            ts_str = datetime.fromtimestamp(ts, tz=IST).strftime("%H:%M")
            bars.append(FastBar(ts_str, float(open_p), float(high_p),
                                float(low_p), float(close_p), int(volume), 300))
    except Exception as exc:
        logger.warning("5-minute history fetch failed (using snapshot levels): %s", exc)
    return bars


class CloseJob:
    """Run the close sequence once. Returns a process exit code."""

    def __init__(self, config: JevConfig, *, fyers_client=None,
                 telegram=None, fetch_engine=None,
                 sleep=time.sleep, now_fn=get_ist_now):
        self.config = config
        self._fyers_client = fyers_client
        self._telegram = telegram
        self._fetch_engine = fetch_engine or fetch_terminal_engine
        self._sleep = sleep
        self._now = now_fn

    # -- collaborators --------------------------------------------------
    def _telegram_for(self, store, router):
        if self._telegram is not None:
            return self._telegram
        return TelegramController(
            bot_token=os.environ.get("TELEGRAM_BOT_TOKEN", "").strip(),
            chat_id=os.environ.get("TELEGRAM_CHAT_ID", "").strip(),
            state_store=store,
            router=router,
        )

    def _login_fyers(self):
        """Fresh Fyers client, reusing the engine's login flow.

        Returns None (never raises) when no token can be obtained.
        """
        client_id = os.environ.get("FYERS_APP_ID", "").strip()
        token = os.environ.get("FYERS_ACCESS_TOKEN", "").strip()
        if not token:
            try:
                auth_mgr = FyersAuthManager(
                    client_id=client_id,
                    secret_key=os.environ.get("FYERS_SECRET_KEY", "").strip(),
                    redirect_uri="https://trade.fyers.in/api-login/redirect-uri/index.html",
                    fyers_id=os.environ.get("FYERS_ID", "").strip(),
                    pin=os.environ.get("FYERS_PIN", "").strip(),
                    totp_key=os.environ.get("FYERS_TOTP_KEY", "").strip(),
                )
                token = auth_mgr.get_valid_token()
            except Exception as exc:
                logger.error("[Auth] Close-job Fyers login failed: %s", exc)
                return None
        if not token:
            return None
        return fyersModel.FyersModel(client_id=client_id, token=token,
                                     is_async=False, log_path="")

    def _wait_until_close(self) -> bool:
        """Sleep until the configured market-close time.

        Returns False when the wait is longer than
        MAX_PLAYBOOK_WAIT_MINUTES — the playbook is then skipped
        instead of holding a runner for hours (see the module
        docstring). True when the close has been reached.
        """
        try:
            hour, minute = _parse_hhmm(self.config.market_close_time)
        except (AttributeError, ValueError):
            logger.warning("Bad MARKET_CLOSE_TIME %r; sending playbook now",
                           self.config.market_close_time)
            return True
        now = self._now()
        target = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
        if target <= now:
            return True
        wait_seconds = (target - now).total_seconds()
        if wait_seconds > MAX_PLAYBOOK_WAIT_MINUTES * 60:
            logger.warning(
                "Market close is %.0f minutes away; skipping the playbook "
                "(square-off card already sent)", wait_seconds / 60)
            return False
        while True:
            remaining = (target - self._now()).total_seconds()
            if remaining <= 0:
                return True
            self._sleep(min(30.0, remaining))

    # -- main flow ------------------------------------------------------
    def run(self) -> int:
        today = self._now().date()
        # A throwaway store/router pair for Telegram before Fyers exists:
        # send_card/send_alert never touch the router, and the real pair
        # is built below once positions are known.
        probe_store = EngineStateStore(":memory:")
        probe_router = ExecutionRouter(
            fyers_client=None, state_store=probe_store,
            paper_mode=self.config.paper_mode, config=self.config)
        tg = self._telegram_for(probe_store, probe_router)
        mode_label = "Paper Mode" if self.config.paper_mode else "Live Real Funds"

        try:
            engine = self._fetch_engine(self.config.terminal_url)
        except Exception as exc:
            logger.error("Close job cannot read the terminal: %s", exc)
            tg.send_alert("Close Job Failed",
                          f"Could not read the terminal snapshot: {exc}")
            return 1
        if not engine:
            logger.info("No engine snapshot on the terminal; nothing to close")
            return 0
        if engine.get("tradingDate") != today.isoformat():
            logger.info("Terminal snapshot is for %s, not today %s; nothing to close",
                        engine.get("tradingDate"), today.isoformat())
            return 0
        positions = open_positions_from_engine(engine)
        if not positions:
            # Already closed (or never traded): send nothing, so the
            # playbook can never be duplicated. See module docstring.
            logger.info("No open positions in today's snapshot; nothing to close")
            return 0

        fyers = self._fyers_client or self._login_fyers()
        if fyers is None:
            tg.send_alert("Close Job Failed",
                          "Fyers login failed; open positions could not be "
                          "squared off. Check the close-job run log.")
            return 1
        quote_fn = lambda sym: get_option_quote(fyers, sym)  # noqa: E731

        store = EngineStateStore(":memory:")
        for pos in positions.values():
            store.save_position(pos)
        router = ExecutionRouter(
            fyers_client=fyers, state_store=store,
            paper_mode=self.config.paper_mode, config=self.config)
        router.set_quote_fn(quote_fn)
        tg = self._telegram_for(store, router)
        monitor = PositionMonitor(store, router, quote_fn=quote_fn,
                                  config=self.config)

        actions = monitor.square_off_all()
        if actions:
            rows = [("Status", "All open positions closed at market")]
            rows += [("Closed", a) for a in actions]
        else:
            rows = [("Positions", "No open positions to square off")]
        tg.send_card("End-of-Day Square-Off", rows, footer_extra=mode_label)
        logger.info("Close-job square-off done: %s", actions)

        spot = get_option_quote(fyers, UNDERLYING_SYMBOL)
        if spot is None:
            spot = engine.get("spot")
        tg.send_card(
            "Session Closed",
            [
                ("Final NIFTY Spot", f"{float(spot):,.2f}" if spot else "n/a"),
                ("Positions closed", str(len(actions))),
                ("Market close", "3:30 PM IST"),
            ],
            footer_extra=mode_label,
        )

        if not self._wait_until_close():
            store.close()
            return 0

        # Real close data, fetched fresh at the close — never the
        # 15:10 snapshot's numbers.
        expiry = next_weekly_expiry(now=self._now(),
                                    override=self.config.expiry_override)
        bars = _fetch_5m_bars(fyers, today)
        if bars:
            today_high = max(b.high for b in bars)
            today_low = min(b.low for b in bars)
        else:
            today_high, today_low = engine.get("pdh"), engine.get("pdl")
        if spot is None:
            spot = engine.get("spot")
        if spot is None:
            # No close price from Fyers and none in the snapshot: a
            # playbook with a 0.00 close would be worse than none.
            tg.send_alert("Close Job Failed",
                          "No NIFTY close price available; playbook skipped. "
                          "Positions were squared off (see the card above).")
            store.close()
            return 1
        gex = None
        try:
            gex = compute_gex_snapshot(fyers, float(spot), expiry)
        except Exception as exc:
            logger.warning("Close-job GEX read failed: %s", exc)
        tg.send_card(
            "Post-Market Playbook",
            build_playbook_rows(
                close=float(spot),
                prev_close=float(engine.get("pdc") or spot),
                today_high=float(today_high) if today_high is not None else 0.0,
                today_low=float(today_low) if today_low is not None else 0.0,
                gex=gex, expiry=expiry),
            footer_extra=mode_label,
        )

        # One final terminal push, with no open positions: a re-run of
        # this job now reads an empty snapshot and exits quietly.
        if self.config.terminal_url:
            try:
                payload = build_engine_payload(
                    spot=float(spot),
                    pdh=engine.get("pdh"), pdl=engine.get("pdl"),
                    pdc=engine.get("pdc"), week_poc=engine.get("weekPoc"),
                    gex_snapshot=gex, expiry=expiry, bars=bars,
                    trading_date=today, positions=[])
                ok, err = push_engine_snapshot(
                    self.config.terminal_url, self.config.terminal_token, payload)
                if not ok:
                    logger.warning("Close-job final terminal push failed: %s", err)
            except Exception as exc:  # never fail the run for the terminal
                logger.warning("Close-job final terminal push failed: %s", exc)
        store.close()
        logger.info(">>> Close job finished.")
        return 0


def main() -> int:
    config = JevConfig.from_env()
    setup_logging(config.log_level)
    logger.info(">>> Jev Engine close job starting (mode: %s)",
                "Paper Mode" if config.paper_mode else "Live Real Funds")
    return CloseJob(config).run()


if __name__ == "__main__":
    sys.exit(main())
