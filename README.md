# Jev Engine - Indian Equity Derivatives Edition

Automated intraday options-trading system for Indian index derivatives,
built around NIFTY 50 weekly options via the Fyers broker.

Each morning it logs into Fyers automatically (login APIs + TOTP),
subscribes to the live NIFTY spot feed, and watches 5-minute
bars for a breakout/breakdown. When a breakout is confirmed by
order-flow/footprint signals, it buys an at-the-money NIFTY weekly
option (1 lot = 75 qty) — a CE on a bullish breakout, a PE on a
bearish breakdown — with a premium-based stop-loss and two targets.
It is controlled remotely via Telegram (`/status`, `/pause`,
`/resume`, `/panic`).

> **Paper mode is the default.** Nothing here trades with real money
> unless you explicitly set `PAPER_MODE=False` *and* wire in a real
> Fyers account. Read "Live mode checklist" before doing that.

## How it works (live path)

1. `fyers_auth_automation.py` — daily login through Fyers' login
   APIs (no browser: the login page gates Continue behind a human
   check automation cannot complete), TOTP + PIN, token cached for
   the current day only (Fyers tokens expire daily).
2. `main_jev_orchestrator.py` — entry point. Fetches real PDH/PDL/PDC
   and weekly/monthly profile levels, seeds today's completed
   5-minute bars from history, then streams NIFTY spot ticks.
3. `advanced_auction_footprint_engine.py` — tick footprint: 3:1
   diagonal imbalances, stacked levels, absorption, single
   time-framing.
4. `lag_neutralized_engine.py` — the signal engine. All five must
   hold: price vs previous close, a 12-bar trend, a break of the
   rolling 6-bar ceiling/base, CVD delta in the same direction, and
   the bar at least 260 seconds in.
5. `india_gex_engine.py` (via `market_data.py`) — dealer gamma from
   the live option chain. If the chain cannot be fetched, no signal
   is generated.
6. `execution_broker_interface.py` — order router (paper simulator
   or live Fyers orders) with risk checks.
7. `position_monitor.py` — exit management: stop-loss, target 1
   (partial exit for multi-lot positions, otherwise stop moved to
   breakeven), target 2, and end-of-day square-off at 15:20 IST.
8. `telegram_remote_controller.py` — remote control and alerts.
9. `engine_state_store.py` — SQLite (WAL) persistence for positions,
   flags, and daily trading statistics.

## Setup

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env   # fill in your credentials; never commit .env
python main_jev_orchestrator.py
```

Python 3.10–3.12. Dependencies are pinned in `requirements.txt`
(verified to install together; note `fyers-apiv3==3.2.1` pins
`requests==2.31.0`). No browser is needed for the Fyers login any
more; Playwright remains in `requirements.txt` but is unused.

## Configuration

All settings are environment variables (see `.env.example`); in
GitHub Actions they come from repository Secrets.

| Variable | Default | Notes |
| --- | --- | --- |
| `FYERS_APP_ID`, `FYERS_SECRET_KEY`, `FYERS_ID`, `FYERS_PIN`, `FYERS_TOTP_KEY` | — | Fyers credentials (Secrets) |
| `FYERS_ACCESS_TOKEN` | — | Optional pre-obtained token, skips the API login |
| `FYERS_EXPIRY_OVERRIDE` | — | `YYYY-MM-DD` override for the weekly expiry, e.g. a holiday Tuesday |
| `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID` | — | Telegram alerts / commands |
| `PAPER_MODE` | `True` | `False` only for live trading with real funds |
| `LOT_SIZE` | `75` | NIFTY option lot size |
| `MAX_DAILY_LOSS_RS` | `10000` | New entries blocked once realised daily loss passes this |
| `MAX_TRADES_PER_DAY` | `3` | |
| `MAX_OPEN_POSITIONS` | `1` | |
| `VIRTUAL_CASH_INITIAL` | `200000` | Paper-mode starting cash (Rs) |
| `SQUARE_OFF_TIME` / `MARKET_CLOSE_TIME` | `15:20` / `15:31` | IST |
| `LOG_LEVEL` | `INFO` | |

## Telegram commands

- `/status` — mode, paused state, open positions, virtual balance
- `/pause` — block new entries
- `/resume` — allow entries again
- `/panic` — pause the engine and close all positions at the current
  market price. If a position has no available market price it is
  left open and reported, never booked at its entry price.

## Paper vs live mode

- **Paper (default):** fills are simulated at premium × 1.005,
  virtual cash is debited/credited, and every risk check still
  applies. No broker order is placed.
- **Live (`PAPER_MODE=False`):** the authenticated Fyers client is
  wired into the router. Orders are marketable limits (+2% ceiling).
  An order is only reported `FILLED` when the broker's order book
  confirms it; a placed-but-unconfirmed order is `PLACED` and the
  position stays open so it cannot be duplicated. A live buy is
  refused when the account balance cannot be determined.

## Running it

Locally: `python main_jev_orchestrator.py`. In GitHub Actions the
workflow (`.github/workflows/jev_engine_workflow.yml`) runs the test
suite on every PR, and runs the engine on schedule (weekdays 09:15
IST, 6-hour timeout) in paper mode.

Tests (no live API or Telegram calls — everything is mocked):

```bash
pip install -r requirements-dev.txt
python -m pytest -q
```

## What was fixed (branch `fix/jev-engine-critical-fixes`)

The November 2026 code review found the live path could not trade.
These fixes address every finding:

- **Signals can now fire:** 5-minute bars are accumulated (seeded
  from today's history) and passed to the lag engine, which used to
  always receive an empty history and return HOLD.
- **Live execution reachable:** the authenticated Fyers client is
  wired into the router after login (it used to stay `None`).
- **Premium pricing:** orders use the option's own LTP from Fyers,
  with premium-based SL/T1/T2 (50% / 150% / 200% of premium). The old
  code priced options at the NIFTY spot (~24,000) and used spot
  levels as option targets. A spot-priced order is now rejected by
  the cash check.
- **Real dealer gamma:** computed from the live option chain via
  `IndiaGEXEngine`; the hardcoded `125000` constant is gone. If the
  chain cannot be fetched, the engine returns HOLD.
- **Weekly expiry:** symbols are built from the next Tuesday expiry
  (before 15:30 IST on Tuesday itself), not a hardcoded `26NOV`.
  Set `FYERS_EXPIRY_OVERRIDE` when that Tuesday is a holiday.
  NIFTY weeklies moved from Thursday to Tuesday on 1 Sep 2025 (NSE/SEBI).
- **Exit management:** the position monitor exits on stop-loss and
  targets, moves the stop to breakeven at target 1, and squares off
  at 15:20 IST. `/panic` exits at market price.
- **Risk controls:** max daily loss (Rs 10,000), max 3 trades/day,
  max 1 open position, whole-lot quantities, and cash/balance checks.
- **MTF builder:** the monthly rule now actually uses the current
  month after the first week (both branches used to return the
  previous month), and the orchestrator passes distinct prior/current
  week and month levels.
- **Stale levels:** if historical levels cannot be fetched, trading
  is paused and an alert is sent instead of trading on hardcoded
  defaults.
- **Auth:** cached tokens are only trusted when saved today (IST);
  the token exchange has a 30 s timeout and HTTP status checks; bare
  `except: pass` blocks were removed.
- **Threading/shutdown:** SQLite access is locked; shutdown is
  graceful (WAL checkpoint + close) instead of `os._exit(0)`.
- **No-token fallback:** the engine now idles and alerts once
  instead of sending fake "Signal Triggered" alerts and paper trades
  from mock bars.
- **Hygiene:** `print()` replaced with `logging`, `.gitignore`
  (token file, state DB, `.env`), `.env.example`, pinned
  requirements, a pytest suite (38 tests), and a test job in CI.

## Known limitations

- **Weekly "value areas" are not volume-derived.** VAH/VAL are the
  high/low inset by 15% of the range and POC is the midpoint. Do not
  treat them as a real volume profile.
- **Expiry holidays are not known from the calendar.** If the
  Tuesday expiry is a trading holiday, set `FYERS_EXPIRY_OVERRIDE`.
- **Option symbol format** follows Fyers' symbology docs (updated
  14 Oct 2026): weekly `NSE:NIFTY26N1725300CE` (YYMdd, month code
  1-9/O/N/D) and monthly `NSE:NIFTY26NOV23250CE` (YYMMM) on the last
  Tuesday of the month. The old DDMMM format matched no Fyers format
  and would have been rejected as an invalid symbol.
- **GEX option-chain parsing is defensive** and depends on the
  Fyers client's option-chain method/response shape; if it cannot be
  parsed, GEX is unavailable and the engine holds.
- **Live fill confirmation** depends on the broker's order-book
  response; an unconfirmed fill stays `PLACED`, not `FILLED`.
- **No backtester.** Strategies can only be validated on live/paper
  bars or via the unit tests.
- The engine trades **NIFTY only**, one symbol, one timeframe
  (5-minute bars from spot ticks), despite the README title naming
  FINNIFTY/SENSEX.

## What is not in the live path

These modules are standalone library code, not called by the
orchestrator (except GEX, which is wired in via `market_data.py`):

- `greeks_structural_engine.py` — NR4/NR7 + Greeks confluence
- `gamma_blast_detector.py` — 0DTE 14:00–15:08 IST gamma-squeeze detector
- `time_price_multi_layer_engine.py` — 1D→2H→30M→15M→5M confluence
- `india_order_flow_engine.py` — Lee-Ready CVD (the footprint engine
  provides CVD in the live path instead)
- `fyers_live_feed.py` — standalone WebSocket streamer (the
  orchestrator re-implements streaming inline)
