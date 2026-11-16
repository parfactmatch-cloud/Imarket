# Jev Terminal

A standalone, deployable version of the Jev Terminal — the dashboard for
the Jev Engine (NIFTY 50, paper mode). Live spot and 5-minute bars,
PDH / PDL / PDC, weekly POC / VAH / VAL, call / put walls, ATM Greeks,
premium scenarios, the engine's signal conditions, a gamma-blast watch,
an engine session log, and a post-market execution plan for the next
session.

It is a small server app, not a static page, on purpose: Yahoo Finance
sends no CORS headers, so a browser page cannot fetch `^NSEI` directly.
This server fetches Yahoo server-side and serves the page + JSON to the
browser. **Python standard library only — no dependencies to install.**

## Honest limit

This is **not tick-level real time**. It runs on Yahoo Finance snapshots,
refreshed at most every 5 minutes during market hours (9:15–15:30 IST,
weekdays), and the page says so in its header. The engine's real-time
source is its Fyers WebSocket, whose token is minted fresh each morning
inside GitHub Actions and expires daily — a web page cannot hold it.

Walls / PCR / max pain / Week POC come from the engine's own Telegram
messages, logged into the terminal (see "Log the engine's levels"),
not from a live chain feed. The dealer-gamma + CVD signal condition is
engine-only and is marked as such on the page. True real-time would need
the engine to push ticks to the terminal — not built.

## Run it locally

```bash
cd terminal
python3 server.py          # http://127.0.0.1:8000
PORT=8080 python3 server.py
```

Open http://127.0.0.1:8000. The first load uses the seed in `data/seed.json`
(the last verified session, Mon 16 Nov 2026: NIFTY 25,291.10, Week POC
25,131.55, call wall 26,000, put wall 25,000, max pain 25,200, PCR 0.90)
until a fresh Yahoo snapshot is fetched in market hours.

## Deploy it

### Render (free tier, easiest)

1. In Render: **New + → Web Service → pick the Imarket repo**.
2. Root Directory: `terminal`. Build Command: leave empty.
   Start Command: `python server.py`. (Or use the `render.yaml`
   in this folder — Render picks it up as a Blueprint.)
3. Open the URL Render gives you. Done — nothing else to set up.

Note: on the free tier the disk is ephemeral, so snapshots, plans and
logged levels reset on a redeploy. Re-log the engine levels after a
redeploy (below), or attach a persistent disk and set
`JEV_TERMINAL_DATA_DIR` to it.

### Railway / any VPS

```bash
git clone https://github.com/parfactmatch-cloud/Imarket.git
cd Imarket/terminal
PORT=8000 nohup python3 server.py &
```

Railway sets `$PORT` for you; on a VPS, put it behind nginx/Caddy or
run it with `systemd`. No secrets are needed — the terminal reads only
public Yahoo data. Never put Fyers or Telegram credentials here.

## Log the engine's levels (after the 9:15 run)

Each morning, after the engine's run, paste the Week POC from its
System Online Telegram message and the call wall / put wall / max pain /
PCR from its live chain into the **Log engine levels** panel on the page
(or `POST /api/chain`):

```bash
curl -X POST localhost:8000/api/chain -H 'Content-Type: application/json' \
  -d '{"weekPoc": 25131.55, "callWall": 26000, "putWall": 25000, "maxPain": 25200, "pcr": 0.90}'
```

The terminal then shows the engine's POC (not its own time-at-price
estimate) and re-judges the POC chop condition against it.

## API

| Endpoint | What it does |
| --- | --- |
| `GET /` | The terminal page |
| `GET /api/health` | `{"ok": true, ...}` |
| `GET /api/snapshot` | Latest snapshot (`?force=1` refreshes now) |
| `GET /api/terminal` | Snapshot + chain + plan + events (what the page loads) |
| `GET /api/plan` | Latest post-market plan |
| `POST /api/chain` | Log the engine's levels for today |
| `POST /api/events` | Add an engine session-log entry `{kind, text}` |
| `DELETE /api/events?id=N` | Delete one entry |
| `POST /api/plan/build` | Rebuild the post-market plan from the latest snapshot |
| `POST /api/plan/note` | Save a note on a plan `{planDate, note}` |

State lives in JSON files under `terminal/data/` (or
`$JEV_TERMINAL_DATA_DIR`). Only `data/seed.json` is committed.

## How the numbers are computed

* **Levels:** PDH / PDL / PDC from the previous trading day's Yahoo
  5-minute bars. Weekly POC / VAH / VAL from a time-at-price profile
  of the last 5 days of bars (10-point bins, 70% value area) — an
  estimate, overridden by the engine-logged Week POC whenever present.
* **Greeks:** Black-Scholes (r = 7%), IV from India VIX, time to the
  Tuesday 15:30 IST expiry close. ATM strike = round(spot / 50) × 50.
* **Signal conditions:** bias vs PDC, 12-bar trend, break of the
  rolling 6-bar ceiling/base, clear of the ±0.1% POC chop zone, bar
  at least 260s into the 5-minute bar. Gamma + CVD: engine-only.
* **Gamma blast:** watch only, 14:00–15:08 IST on expiry day. The
  detector is **not** wired into the live engine (library module
  only), so any blast play is manual, sized for a ~30% hit rate.
* **Post-market plan:** built from today's completed session —
  tomorrow's PDH / PDL / PDC, bias, value area, expiry context and
  the execution checklist, under the engine's risk rules (paper mode,
  max 3 trades/day, max 1 open position, max daily loss ₹10,000,
  SL 50% / T1 150% / T2 200%, breakeven after T1, square-off 15:20 IST).

## Known mismatch — engine expiry day (needs a separate fix)

**The engine's `expiry.py` still computes the next Thursday as the
weekly expiry. That is wrong now: NIFTY 50 weekly options expire every
Tuesday on NSE since 1 Sep 2025 (SEBI/NSE schedule change; previously
Thursday).** This terminal uses Tuesday — which is why its first version
showed Tue 17 Nov 2026 as expiry, while the engine's run #46 reported
Thu 19 Nov 2026. If the engine buys the Thursday series, it is buying a
series that does not exist as a NIFTY weekly.

This terminal PR deliberately does **not** change `expiry.py` — that is
a trading-logic change and belongs in its own fix branch + PR, with a
check against the live Fyers symbol master. Flagging it here so it is
not lost.

## Tests

```bash
python3 -m pytest terminal/tests/test_terminal.py
# or: python3 -m unittest discover -s terminal/tests -v
```

Tests are pure (no network): Greeks / put-call parity, Tuesday expiry,
Yahoo parsing, levels, signal conditions, gamma-blast window, plan.
