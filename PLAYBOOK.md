# Jev Engine — Action Playbook

Operating runbook for the NIFTY 50 Jev Engine (private repo
`parfactmatch-cloud/Imarket`). Written 16 Nov 2026, after run #46
proved the API login live. Paper mode is the default; nothing here
trades with real money unless you explicitly set `PAPER_MODE=False`.

## Quick facts

| | |
| --- | --- |
| Instrument | NIFTY 50 weekly options only (README also names FINNIFTY/SENSEX; the engine trades NIFTY) |
| Schedule | GitHub Actions, weekdays 09:15 IST (`45 3 * * 1-5` UTC), or manual dispatch |
| Mode | Paper by default. Virtual cash ₹200,000. 1 lot = 75 qty |
| Risk limits | Max daily loss ₹10,000 · max 3 trades/day · max 1 open position |
| Exits | Stop-loss, target 1, target 2, breakeven moves, square-off 15:20 IST, session ends 15:30 IST |
| Expiry | Next Thursday (override: `FYERS_EXPIRY_OVERRIDE`) |
| Control | Telegram `/status`, `/pause`, `/resume`, `/panic` |

## A normal day (what should happen)

1. **09:15 IST** — run starts. Auth takes ~2 seconds (run #46: 11:12:20–11:12:21 UTC).
2. Engine fetches real levels (PDH / PDL / Week POC), seeds 75 completed
   5-minute bars, connects the Fyers WebSocket, subscribes to `NSE:NIFTY50-INDEX`.
3. Telegram: **System Online (Live Feed)** — "Jev Engine (Paper Mode) …
   Status: Live Scanning". This is the "all good" message.
4. All day: the engine scans. A signal needs all five conditions
   (price vs previous close, 12-bar trend, 6-bar ceiling/base break,
   CVD in the same direction, bar at least 260 seconds in) plus dealer
   gamma from the live option chain. **A day with no signals is normal —
   it is not a failure.**
5. **15:20 IST** — **End-of-Day Square-Off**: all open positions closed
   at market (or "No open positions to square off").
6. **15:30 IST** — **Session Closed**: final Nifty spot and session CVD.
   The run should then finish.

## Telegram commands

- `/status` — running or paused, open positions, today's P&L.
- `/pause` — blocks new entries. Open positions are **not** closed.
- `/resume` — signal execution active again.
- `/panic` — closes all positions at market **and** pauses the engine.
  Use it when something looks wrong and you want out now.

## Alert decoder — what each message means, and what to do

| Alert | Meaning | Action |
| --- | --- | --- |
| **System Online (Live Feed)** | Token secured, levels fetched, live scanning. | None. This is the good one. |
| **System Online (No Live Token)** | Auth failed. Engine is idle: **no signals, no trades.** | Read the **Auth Error** alert that came with it and use the table below. Do not assume it traded. |
| **Auth Error — Token generation failed: …** | Login failed at a named step. | See "Auth failures" below. The step name tells you exactly where. |
| **Levels Unavailable — Trading Paused** | Real levels could not be fetched. Engine paused itself rather than trade on stale defaults. | None today. Check the next run; do not force a trade. |
| **NIFTY BUY / SELL SIGNAL** | All five conditions + gamma confirmed; an order was placed (paper). | Check the **Position Update** that follows. |
| **Order Rejected: …** | The router refused the order; the reason is in the message (usually a risk limit). | Read the reason. If it is a risk limit, that is the system working. |
| **Quote Unavailable** | No live quote for the option symbol; trade skipped. | None. It will try again on the next signal. |
| **Position Update** | An exit fired: stop-loss, target 1, target 2, or breakeven move. | None, unless the action reads FAILED — then check the run log. |
| **End-of-Day Square-Off** | 15:20 IST close-out. | None. |
| **Session Closed** | Market closed 3:30 PM IST. Final spot + session CVD. | None. Day is done. |
| **System Shutdown** | Engine manually terminated. | Only appears if someone stopped it. |

## Auth failures — step by step

Login is API-based since PR #6 (no browser — see "History" below).
Each failure names its step in the Telegram alert.

| Step (alert text) | Likely cause | Fix |
| --- | --- | --- |
| **Login OTP step** | `FYERS_ID` secret is wrong, or Fyers is unreachable. | Check the secret is the **trading login Client ID** — the 7-character ID from account opening (or registered mobile). **Not** the App ID ending `-100`, **not** the 8-digit demat/BO client ID. (Earlier runs typed 8 characters; that was the wrong value.) |
| **TOTP step** | `FYERS_TOTP_KEY` secret is wrong or out of sync. | Re-check the TOTP secret in GitHub Settings → Secrets. |
| **PIN step** | `FYERS_PIN` secret does not match the account PIN. | Re-check the PIN secret. |
| **Auth code step** | The login API endpoints changed (they are undocumented — see below) or Fyers rejected the token request. | Check Fyers status, then the run log. If the endpoints changed, the code needs updating — do not retry blindly. |

Other auth facts:

- Fyers tokens expire daily at 6:30 AM. Every run re-authenticates;
  a token cached from yesterday is never reused.
- **Never send your PIN or TOTP in chat.** Secrets live only in
  GitHub repo Secrets. If a secret is wrong, fix it there.
- The alert never contains your PIN, TOTP, or token. If you ever see
  a credential in a Telegram message, rotate it immediately.

## When the job hangs

Run #46 (16 Nov 2026): the engine printed "lifecycle finished" at
11:12 UTC and Telegram sent Session Closed, but the Actions job still
showed as running ~25 minutes later, with the post-steps pending.

- **What it means:** the engine itself finished cleanly — no trades
  are at risk. Something is keeping the runner alive after the
  script ends (suspect: a non-daemon thread, e.g. the WebSocket).
- **What to do:** open the run, check the log tail. If it ends with
  "Jev Engine lifecycle finished" and nothing more, press **Cancel
  workflow**. If it happens again on the next run, it needs a code
  fix (make the socket thread a daemon / close it explicitly) — do
  not just leave jobs hanging, they burn Actions minutes.

## Going live — checklist (do not skip)

Paper mode is the default for a reason: the strategy has never been
backtested and has never traded live. Before ever setting
`PAPER_MODE=False`:

1. Watch at least one full week of paper runs: auth succeeds every
   day, signals fire on real breakouts, exits trigger as designed.
2. Confirm the paper ledger reconciles (trades vs balance).
3. Set real-money risk limits deliberately — the ₹10,000 / 3 trades /
   1 position defaults are paper-sized.
4. Know the IP question: the login APIs worked from a GitHub Actions
   runner on 16 Nov 2026, but live order placement from a runner has
   not been proven. A Trading app requires a static IP; if Fyers
   enforces it on order calls, the engine cannot trade from Actions
   even with a valid token.
5. Decide how you will monitor it live (Telegram `/status` at minimum).

## Open items (as of 16 Nov 2026)

- **Close PR #5** — superseded by PR #6; do not merge it.
- **Workflow file** (`.github/workflows/jev_engine_workflow.yml`) still
  installs Chromium (`playwright install chromium --with-deps`). It is
  unused since PR #6 removed the browser login. Apply the edit manually
  — workflow files cannot be pushed by the GitHub integration.
- **Job-hang watch item** — see above; check the next run.

## History (why things are the way they are)

- **PR #1** — fixed signals that could never fire, unreachable live
  execution, and spot-based pricing. Live config: premium-based
  orders, dealer gamma from the live chain, Thursday expiry, exit
  management, risk limits.
- **PRs #2–#5** — chased a Fyers browser-login failure ("Stalled",
  then a selector crash, then Continue staying disabled). Each run
  named the next failure precisely; that is what the step-named
  errors above are for.
- **PR #6** — replaced the browser login with API calls
  (`send_login_otp` → `verify_otp` → `verify_pin` → `/api/v3/token`
  → `validate-authcode`). Why: typing the real Client ID into the
  live login page left Continue disabled — the page shows an
  unchecked Cloudflare "Verify you are human" checkbox that headless
  automation cannot complete. Proved live in run #46: all four steps
  in ~2 seconds. Caveat: the `vagator` login endpoints are **not** in
  Fyers' official docs (community guides only) and may change
  without notice. If they do, the Auth Error will name the step.
