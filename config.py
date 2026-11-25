"""
config.py
Central configuration for Jev Engine.

All trading parameters live here, in one place, instead of being scattered
as magic numbers through the orchestrator and router.

Safety defaults (deliberate, do not relax without a review):
- PAPER_MODE defaults to True. Live trading only happens when the operator
  explicitly sets PAPER_MODE=False *and* a real Fyers client is wired in.
- One open position at a time, a small daily trade cap, and a daily loss
  cap. These are paper-mode defaults; they apply in live mode too.

Every value can be overridden with an environment variable (see
.env.example). No secrets are read here -- credentials come from the
environment / GitHub Secrets and are never logged.
"""

import logging
import os
from dataclasses import dataclass

# --- Trading constants (single source of truth) ---
LOT_SIZE = 75  # NIFTY weekly option lot size
UNDERLYING = "NIFTY"
UNDERLYING_SYMBOL = "NSE:NIFTY50-INDEX"

# Premium-based risk/reward on an option buy (fractions of entry premium).
# The old code used NIFTY spot levels (+/-30 / +/-75 points) as option
# stop-loss/targets, which is meaningless for an option premium.
SL_FRACTION = 0.50   # stop-loss at 50% of entry premium
T1_FRACTION = 1.50   # target 1 at 150% of entry premium
T2_FRACTION = 2.00   # target 2 at 200% of entry premium

# Bar timing (seconds into a 300 s / 5-minute bar).
BAR_SECONDS = 300
EARLY_TRIGGER_SECONDS = 260
MONITOR_CHECK_SECONDS = 60

# Exit management.
SQUARE_OFF_TIME = "15:20"   # HH:MM IST -- close all positions before 15:30
MARKET_CLOSE_TIME = "15:31"  # HH:MM IST -- graceful shutdown


def _env_bool(name: str, default: bool) -> bool:
    return os.environ.get(name, str(default)).strip().lower() in ("true", "1", "yes")


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, str(default)).strip())
    except ValueError:
        return default


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, str(default)).strip())
    except ValueError:
        return default


def setup_logging(level: str = "INFO") -> None:
    """Configure module logging. Call once, at process start."""
    logging.basicConfig(
        level=getattr(logging, level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )


@dataclass(frozen=True)
class JevConfig:
    """Runtime configuration, resolved from the environment."""

    paper_mode: bool = True
    lot_size: int = LOT_SIZE
    max_daily_loss_rs: float = 10000.0
    max_trades_per_day: int = 3
    max_open_positions: int = 1
    virtual_cash_initial: float = 200000.0
    square_off_time: str = SQUARE_OFF_TIME
    market_close_time: str = MARKET_CLOSE_TIME
    # Optional YYYY-MM-DD override for the weekly expiry, e.g. when the
    # Tuesday expiry is a trading holiday. Empty means "compute it".
    expiry_override: str = ""
    log_level: str = "INFO"
    # Jev Terminal live feed (optional): the engine POSTs its Fyers
    # snapshot to the terminal once per 5-minute bar. Empty URL disables
    # it. The token must match the terminal's JEV_TERMINAL_TOKEN env var.
    terminal_url: str = ""
    terminal_token: str = ""
    # Bid/ask-spread entry filter (steps 1 and 5, 25 Nov 2026): a trade
    # is skipped when the option leg's spread is wider than this, in
    # rupees. A leg with no bid/ask on the feed never blocks on spread
    # alone — the live depth check is the guard for that case.
    max_option_spread_rs: float = 1.0
    # How often the narrow (strikecount=15) chain is re-fetched, in
    # seconds (step 4). 60 s = ~375 calls/day on the Standard plan's
    # 5,000/day data budget; the wide chain (step 7) polls every 5 min.
    gex_poll_seconds: int = 60

    @classmethod
    def from_env(cls) -> "JevConfig":
        return cls(
            paper_mode=_env_bool("PAPER_MODE", True),
            lot_size=_env_int("LOT_SIZE", LOT_SIZE),
            max_daily_loss_rs=_env_float("MAX_DAILY_LOSS_RS", 10000.0),
            max_trades_per_day=_env_int("MAX_TRADES_PER_DAY", 3),
            max_open_positions=_env_int("MAX_OPEN_POSITIONS", 1),
            virtual_cash_initial=_env_float("VIRTUAL_CASH_INITIAL", 200000.0),
            square_off_time=os.environ.get("SQUARE_OFF_TIME", SQUARE_OFF_TIME).strip(),
            market_close_time=os.environ.get("MARKET_CLOSE_TIME", MARKET_CLOSE_TIME).strip(),
            expiry_override=os.environ.get("FYERS_EXPIRY_OVERRIDE", "").strip(),
            log_level=os.environ.get("LOG_LEVEL", "INFO").strip(),
            terminal_url=os.environ.get("JEV_TERMINAL_URL", "").strip(),
            terminal_token=os.environ.get("JEV_TERMINAL_TOKEN", "").strip(),
            max_option_spread_rs=_env_float("MAX_OPTION_SPREAD_RS", 1.0),
            gex_poll_seconds=_env_int("GEX_POLL_SECONDS", 60),
        )
