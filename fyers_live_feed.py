"""
fyers_live_feed.py
Real-Time WebSocket Feed Handler using Fyers Data Socket (v3).
Pipes live ticks directly into Order Flow and Lag-Neutralized Engine.

NOTE: this streamer is NOT used by the live path. main_jev_orchestrator
re-implements streaming inline (it subscribes to NIFTY spot only, while
this module also subscribes to BANKNIFTY). Kept as a standalone
reference implementation; see README "What is not in the live path".
"""

import logging
import time
from fyers_apiv3.FyersWebsocket import data_ws
from india_order_flow_engine import IndiaOrderFlowEngine, TickData
from lag_neutralized_engine import FastBar

logger = logging.getLogger(__name__)


class FyersLiveStreamer:
    def __init__(self, access_token: str, client_id: str, on_bar_completed_callback):
        """
        access_token: Fyers API valid access token (format: "APP_ID:ACCESS_TOKEN")
        on_bar_completed_callback: Function called when a 5M / Sub-bar threshold is crossed
        """
        self.access_token = access_token
        self.client_id = client_id
        self.callback = on_bar_completed_callback

        self.order_flow = IndiaOrderFlowEngine()
        self.current_bar_ticks = []
        self.bar_start_time = time.time()

        # Fyers v3 Data Socket connection string
        self.full_auth = f"{self.client_id}:{self.access_token}"
        self.ws = None

    def on_message(self, message):
        """
        Fyers WebSocket Tick Callback.
        """
        # Ticks filter: LTP, Volume, Bid, Ask
        if isinstance(message, dict) and message.get("response_type") == "depth":
            return

        ltp = message.get("ltp")
        vol = message.get("vol_traded_today", 0)
        bid = message.get("bid", ltp)
        ask = message.get("ask", ltp)

        if not ltp:
            return

        tick = TickData(
            timestamp=str(message.get("timestamp", time.time())),
            ltp=float(ltp),
            qty=int(message.get("last_traded_qty", 1)),
            best_bid=float(bid),
            best_ask=float(ask)
        )

        # 1. Update Order Flow Engine (Lee-Ready Tick Rule)
        self.order_flow.process_tick(tick)
        self.current_bar_ticks.append(tick)

        # 2. Sub-Candle Timing Tracker (5M = 300 seconds)
        elapsed = int(time.time() - self.bar_start_time)

        if elapsed >= 300:
            # Bar Complete
            self._flush_bar(elapsed, is_early=False)
        elif elapsed >= 260:
            # Sub-Bar Mature Window (260s early trigger evaluation)
            self._flush_bar(elapsed, is_early=True)

    def _flush_bar(self, elapsed_seconds: int, is_early: bool):
        if not self.current_bar_ticks:
            return

        prices = [t.ltp for t in self.current_bar_ticks]
        total_vol = sum(t.qty for t in self.current_bar_ticks)

        fast_bar = FastBar(
            timestamp=time.strftime("%H:%M"),
            open=prices[0],
            high=max(prices),
            low=min(prices),
            close=prices[-1],
            volume=total_vol,
            elapsed_seconds=elapsed_seconds
        )

        # Callback triggers LagNeutralizedEngine
        self.callback(fast_bar, self.order_flow.cumulative_delta, is_early)

        if not is_early:
            # Reset for next 5-minute candle
            self.current_bar_ticks.clear()
            self.bar_start_time = time.time()

    def on_error(self, message):
        logger.error("[WebSocket Error] %s", message)

    def on_close(self, message):
        logger.info("[WebSocket Closed] %s", message)

    def on_open(self):
        logger.info("[WebSocket Connected] Subscribing to Nifty Index & Key Options...")
        # Symbols to track: Nifty Spot and nearest weekly contracts
        symbols = [
            "NSE:NIFTY50-INDEX",
            "NSE:NIFTYBANK-INDEX"
        ]
        self.ws.subscribe(symbols=symbols, data_type="symbolUpdate")
        self.ws.keep_running()

    def start_streaming(self):
        self.ws = data_ws.FyersDataSocket(
            access_token=self.full_auth,
            log_path="",
            litemode=False,
            write_to_file=False,
            on_connect=self.on_open,
            on_close=self.on_close,
            on_error=self.on_error,
            on_message=self.on_message
        )
        self.ws.connect()
