# Jev Engine — Market Playbook

How to read the market the engine trades, and how to play it. Companion
to `PLAYBOOK.md` (operations: alerts, commands, failures). This file is
about the market itself — the levels, the walls, the Greeks, and the
gamma blast. Every number that comes from the code is labelled **code**;
worked premium/delta/theta numbers are Black–Scholes at spot 25,144,
IV 13%, rate 7% (the rate the engine itself uses) — they are
illustrations, not quotes. Nothing here is a promise: the strategy has
never been backtested and has never traded live.

## 1. Why the signals only say "PDH / PDL"

The Telegram alert is short on purpose — it shows the two levels a
human checks first. But the engine decides on much more than that.
One signal is the end of this chain (**code**):

1. **Bias** — price above/below previous day's close (PDC).
2. **Trend** — 12-bar (1-hour) slope **and** dealer gamma in the same
   direction. If the option chain cannot be fetched, dealer gamma is
   unknown and the engine **never signals**.
3. **Breakout** — current 5-minute close above the highest high (or
   below the lowest low) of the last 6 bars.
4. **Order flow** — CVD (cumulative volume delta) in the same
   direction, plus footprint checks (no absorption, no single
   time-framing against the trade).
5. **Context** — weekly/monthly profile position (see §2) and the POC
   chop filter: **no breakout trade within 0.1% of the weekly POC**.
6. **Timing** — the bar must be at least 260 seconds into its 300
   seconds (the "early trigger" — it does not wait for the close).

All six must hold. That is why a day can pass with no signal at all.

## 2. The map: PDH, PDL, PDC, POC, VAH, VAL

| Level | What it is | How the engine uses it |
| --- | --- | --- |
| **PDH** | Previous day's high | The top of yesterday's range. Above it, buyers are in control. |
| **PDL** | Previous day's low | The bottom of yesterday's range. Below it, sellers are in control. |
| **PDC** | Previous day's close | The bias line. Price ≥ PDC = bullish bias, < PDC = bearish bias. |
| **POC** (Point of Control) | The price where the most volume traded — the market's agreed "fair value". Price is magnetised to it. | **Chop filter:** no trade within 0.1% of the *weekly* POC (≈ ±25 points at 25,144). Inside value, the destination *is* the POC — price rotates back to it instead of breaking out. |
| **VAH** | Value Area High — top of the range where ~70% of volume traded. | Above VAH = imbalance bullish, destination is new value discovery higher. |
| **VAL** | Value Area Low — bottom of the value area. | Below VAL = imbalance bearish, destination is new value discovery lower. |

The engine's three context questions (**code**):

1. *Where is price coming from?* — above/below PDC, above VAH / below VAL.
2. *Where is price right now?* — inside value (rotational, expect a
   return to POC) or outside balance (trending, expect continuation).
3. *Where is price trying to go?* — toward the POC (if inside value)
   or toward new value (if outside).

**How to play it:** buy breakouts only *outside* value, with the POC
behind you, not in front of you. A breakout that starts right at the
POC is usually a fake — the magnet pulls price back. That is exactly
what the chop filter blocks. On Mon/Tue the engine uses the *prior*
week's VAH/VAL/POC; Wed–Fri it uses the *current* developing week;
for the month, the prior month applies for the first 7 days.

## 3. Call wall and put wall

*(You wrote "football" — that is the put wall, the pair to the call wall.)*

A **wall** is the strike with the highest open interest on one side
of the option chain. The engine computes it exactly this way
(**code**): call wall = strike with the highest **call** OI; put wall
= strike with the highest **put** OI.

Why a wall matters: it is where the dealers are most exposed, and
dealer hedging moves the market.

- **Call wall:** heavy call OI. Retail bought calls → dealers sold
  them → dealers are **short gamma** on the call side. If spot rises
  toward the wall, dealers must *buy* more underlying to stay hedged
  — that buying pushes spot up further. So a call wall acts as a
  **magnet below, resistance at the strike, then fuel above it**.
- **Put wall:** heavy put OI. Retail bought puts → dealers sold
  them → dealers are **long gamma** on the put side. If spot falls
  toward the wall, dealers must *sell* less / buy back — that
  cushions the fall. So a put wall acts as **support**.

Net dealer gamma (GEX) is the sum across the chain (**code**):

- **Net GEX > 0 — LONG_GAMMA_SUPPRESSION.** Dealers are net long
  gamma. They buy dips and sell rallies to hedge, which *pins* the
  market in a range. Play: mean reversion, fades at the edges, small
  targets. Breakouts usually fail.
- **Net GEX < 0 — SHORT_GAMMA_EXPANSION.** Dealers are net short
  gamma. They must buy rallies and sell dips, which *amplifies*
  moves. Play: momentum, breakouts, trend-following. This is the
  regime where fast moves — including gamma blasts — happen.
- **Gamma flip level:** the spot price where net GEX crosses zero.
  Above it the market is pinned; below it the market is explosive
  (or the reverse, depending on the chain). Treat it as the line
  between the two regimes.

**How to play through a wall:**

- **Do not** buy a call *into* a call wall expecting it to break on
  the first touch. The wall is where the most sellers sit; the first
  touch usually rejects.
- **Do** watch what happens *at* the wall: if spot stalls there with
  falling OI (sellers unwinding) and CVD still positive, the wall is
  weakening — the break, when it comes, is fast, because dealer
  hedging flips from selling to buying.
- **Do** treat the put wall the same way from below: a breakdown
  that stalls at the put wall with put OI unwinding is a wall
  breaking, not a bounce.
- The engine only trades a breakout when dealer gamma agrees with
  the direction (Layer 2). If the chain says dealers are long gamma
  (pinning regime), it will not chase a breakout at all.

## 4. Point of Control — in detail

The POC is not just "a level". It is where the most business was
done, so it is the market's fair price. Consequences:

- **Inside value, price returns to POC.** If the market opens above
  the POC and cannot hold above VAH, expect a rotation back down to
  the POC — not a breakout. Do not buy the open in that spot.
- **POC is the invalidation line.** In the engine's structural
  module, a short trade below the opening range is invalidated at the
  opening high; a breakout setup is invalidated at the bar low.
  Your stop should sit beyond the level that proves you wrong, not
  at a round number.
- **Weekly POC is the one that matters for the chop filter**
  (0.1% ≈ 25 points). Daily POC matters for intraday rotation;
  the engine's Telegram levels show the *week* POC.

## 5. Delta and theta — how they actually move

Delta and theta are not fixed properties of an option. They move
with spot, and they move *fast* near expiry. Worked numbers below
(spot 25,144, IV 13%).

### Delta — your speedometer and your probability

- Delta is how much the premium moves for a 1-point move in NIFTY.
  ATM delta ≈ **0.50** — a ₹100 premium gains ~₹50 on a 100-point
  NIFTY move (before gamma).
- Delta is also, roughly, the market's implied probability the
  option finishes in the money. **A call with delta 0.30 has about
  a 30% chance of finishing in the money.** That is where the "30%"
  in §7 comes from — it is not a guess, it is the delta.
- Delta rises as spot rises (for a call) and falls as spot falls.
  The rate of that change is **gamma**.

| Strike (CE) | 3 days left: premium / delta | 1 day left: premium / delta | 6 hrs left: premium / delta | 2 hrs left: premium / delta |
| --- | --- | --- | --- | --- |
| 25000 (ITM) | ₹214 / 0.71 | ₹167 / 0.81 | ₹147 / 0.96 | ₹144 / 1.00 |
| 25150 (ATM) | ₹122 / 0.51 | ₹68 / 0.50 | ₹32 / 0.48 | ₹17 / 0.46 |
| 25200 | ₹99 / 0.45 | ₹46 / 0.38 | ₹14 / 0.26 | ₹3 / 0.13 |
| 25300 | ₹61 / 0.32 | ₹18 / 0.19 | ₹1 / 0.04 | ₹0 / 0.00 |
| 25400 | ₹35 / 0.21 | ₹6 / 0.07 | ₹0 / 0.00 | ₹0 / 0.00 |

Read the table top to bottom: the same strike's delta *collapses*
as expiry approaches unless spot moves toward it. A 25300 CE bought
on Monday at delta 0.32 is a delta-0.04 lottery ticket by Tuesday
morning if NIFTY has not moved. **Time kills delta on OTM options
first.**

### Gamma — why ATM options spike

Gamma is highest **at the money and near expiry**. ATM, 1 day left:
gamma 0.0023 — a +100-point NIFTY move lifts delta from 0.50 to
≈0.73. That is why the premium jumps more than delta alone predicts:

- ATM 25150 CE, 1 day left: ₹68. Spot +100 → **₹129 (+90%)**.
  Spot +200 → **₹209 (+209%)**.
- The same option, 2 hours left: gamma 0.0080 — four times higher.
  A 100-point move then lifts delta by ~0.80, but delta cannot
  exceed 1.00, so the move saturates: the option behaves like the
  underlying itself.

**Rule:** gamma is your friend when you are right and the move is
fast; it is what makes a ₹68 option a ₹209 option. It is also why
an ATM option can lose half its value on a 50-point move against
you in the last hour.

### Theta — the rent you pay every hour

Theta is the premium lost per day if nothing moves. It is highest
ATM and it **explodes on expiry day**:

| ATM 25150 CE | Theta per day | Theta per hour* |
| --- | --- | --- |
| 3 days left | −₹22 | −₹3.5 |
| 1 day left | −₹37 | −₹5.8 |
| 6 hours left | −₹70 | −₹11.3 |
| 2 hours left | −₹120 | −₹19.2 |

\* Per trading hour (6.25-hour day). Theta is an *instantaneous*
rate — it falls as the premium falls, so a ₹17 option does not
literally lose ₹19 in the next hour; it loses value fast and then
there is little left to lose.

**Rules that follow:**

- **Never hold an ATM option sideways into expiry day.** At 2 hours
  left you are paying ~₹19/hour on a ₹17 option. You need a move
  *now*, not later.
- **OTM options decay slower in rupees but faster in percent.**
  A ₹6 option losing ₹2/hour is a 33%/hour bleed.
- **Theta is why the engine's targets are premium-based, not
  spot-based.** A 100-point NIFTY move is worth +90% on the premium
  with a day left, but the same move with 2 hours left is worth far
  less in percent — the clock eats the move. The engine's SL/T1/T2
  (50% / 150% / 200% of entry premium) are the same fractions
  regardless, which is a weakness on expiry day — see §8.

## 6. Gamma blast — the exact scenario and time

**First, the honest part: the engine does NOT trade gamma blasts
today.** `gamma_blast_detector.py` exists as a standalone module
with exact conditions, but the orchestrator never calls it. If you
want it live, that is a build job — say so and it can be wired in
with the same paper-mode safety. What follows is what the module
itself requires (**code**).

**Time window: 2:00 PM – 3:08 PM IST, expiry day (Tuesday, 0DTE).**
Why then: it is the last window before RMS square-off. Dealers
holding short gamma into the close must hedge *now*; there is no
tomorrow to spread the hedging over. After 3:08 PM there is not
enough time left for the cascade to pay.

**All six conditions must hold at the same time (code):**

1. **Time** — between 14:00 and 15:08 IST.
2. **Cheap premium** — the option costs **₹2 to ₹9**. (Cheap because
   it is far OTM and nearly dead — that is the whole point: if it
   works, it multiplies; if not, you lose ₹2–9.)
3. **Close to the strike** — spot within **0.35%** of the strike
   (≈ 88 points at 25,144). Not 500 points away — nearly touching.
4. **It is a wall strike** — the strike must be the major call wall
   or put wall (highest OI). Random strikes do not blast; walls do,
   because that is where dealer exposure concentrates.
5. **Dealers short gamma** — net dealer GEX **< 0** at that strike.
   Dealers sold the options and must hedge by buying the underlying
   as it rises — their hedging *is* the fuel.
6. **Unwinding has started** — the strike's OI fell over the last
   **15 minutes** (delta OI < 0): writers are covering. **And** CVD
   slope is positive (real buying, not just a quote flicker).

**The mechanics, in order:** spot drifts toward the wall → dealers
short gamma buy underlying to hedge → their buying pushes spot
higher → more hedging → option writers panic-cover (OI falls) →
premium reprices violently because gamma is at its maximum near
expiry. That is the cascade. The module labels it a "20x–50x gamma
cascade" — **that is the module's label, not a measured result.**
Nothing in this repo has ever observed one live.

**Stop-loss (code):** max(₹0.50, 50% of entry premium). On a ₹5
entry, the stop is ₹2.50 — you lose half, not everything, *if* you
can exit. In a real blast unwind, liquidity at ₹2–9 can vanish;
a stop is not a guarantee.

**What the engine's other module adds:** the structural engine
calls the same regime SHORT_GAMMA_EXPANSION and fires a momentum
short when price breaks the opening low *with* dealers short gamma
*and* IV skew widening. Same idea, opposite direction.

## 7. The 30% problem — how to play options when the spike usually does not come

You said it plainly: a cheap option has only about a 30% chance of
a fast premium spike. That number is consistent with the delta
table — a ₹5–9 OTM option typically carries delta 0.10–0.30. So
assume 30% is, if anything, generous for a *blast* (a 5x+ move
needs more than just finishing in the money; its true probability
is lower than the delta).

The maths that decides whether a 30% strategy makes money:

- **Expected value = (chance of winning × average win) −
  (chance of losing × average loss).**
- Buy at ₹5, sell at 3x (₹15) when right, lose everything when
  wrong: EV = 0.30 × ₹15 − 0.70 × ₹5 = ₹4.50 − ₹3.50 = **+₹1.00**.
  Barely positive — and only if you really do get 3x on every win.
- Buy at ₹5, sell at 2x (₹10) when right: EV = 0.30 × ₹10 − 0.70 ×
  ₹5 = ₹3.00 − ₹3.50 = **−₹0.50**. You lose money on average.
- **The engine's own numbers:** stop-loss 50% of premium, target 2
  at 200% of premium. At a 30% win rate to T2: EV per trade =
  0.30 × (+100%) + 0.70 × (−50%) = +30% − 35% = **−5% per trade**.
  At 30%, the engine's 2x target is *not enough* — it needs a
  higher win rate, or winners bigger than 2x (which is exactly
  what a gamma blast would supply), or smaller losses.

**How to play it, given that:**

1. **Size for the 70%, not the 30%.** One lot, max 3 trades a day,
   max daily loss ₹10,000 — those limits exist because most of
   these trades lose. Never double the size after a loss.
2. **Pay for the move, not for the lottery ticket.** The engine
   buys ATM (delta ~0.50) on a confirmed breakout — a ~50% trade,
   not a 30% one. The ₹2–9 blast trade is a different, smaller,
   separate bet. Do not mix them: do not spend ATM money on OTM
   hope.
3. **Take T1 and move the stop to breakeven.** The engine does this
   automatically (at 150% of premium, stop moves to entry). It is
   the single most important rule: it turns a 30% strategy from
   "lose slowly" into "sometimes free".
4. **Never average down a dying OTM option.** If a ₹5 option is ₹2
   with an hour left, the market has told you the answer. Adding
   is not "cheap", it is paying again for the same 30% — now lower.
5. **Never hold a ₹2–9 option into the last 30 minutes unless all
   six blast conditions are true.** Without them, theta owns the
   option and the premium goes to zero while you watch.
6. **Judge the strategy on 20+ paper trades, not on one day.**
   At 30%, a run of 5 losers in a row is *expected* (about 1 in 6
   chance in any 5-trade stretch). One blast does not prove it
   works; one losing week does not prove it fails.

## 8. Do / Don't / Caution — the short list

**Do**
- Trade only when all six engine conditions agree (bias, trend +
  gamma, breakout, CVD, context, timing).
- Trade outside value, with the POC behind you.
- Respect the walls: let price prove a wall is broken (OI falling,
  CVD with you) before chasing through it.
- Use the 15:20 square-off — never carry an expiring option
  overnight. It expires worthless or auto-settled.
- Check the 30-minute heartbeat: if it stops, the feed died.

**Don't**
- Don't buy a breakout right at the weekly POC (the chop filter
  blocks it for a reason).
- Don't buy into a call wall on the first touch.
- Don't buy ATM options and hold them sideways into expiry day —
  theta is ₹19/hour on a ₹17 option.
- Don't trade when the chain is unavailable (engine already refuses
  — and so should you manually).
- Don't flip `PAPER_MODE=False` on the strength of one good day.

**Caution — what can still go wrong**
- The `vagator` login endpoints are undocumented and can change.
- GEX comes from a 15-strike Fyers chain snapshot, refreshed once a
  bar. A wall can move between refreshes.
- The engine's SL/T1/T2 fractions are fixed (50/150/200%) and do
  not widen on expiry day, when premiums move fastest.
- Gamma blast detection is **not wired in**; NR4/NR7 confluence is
  **not wired in**; IV skew is **not used** by the live path.
  They exist in the code as libraries, not as live behaviour.
- No backtest exists. The 6/10 system rating stands: the plumbing
  works (run #46 proved it), the *strategy* is unproven.

## 9. Reading a signal alert, fully decoded

Example alert:

> **NIFTY BUY SIGNAL**
> Spot: 25180 | Stacked Buy: 3 lvls
> Context: IMBALANCE_BULLISH | Bar Delta: +12,400
> Strike: NIFTY 25200 CE | Premium: 68 | Expiry: 2026-11-17
> SL: 34 | T1: 102 | T2: 136 | Qty: 75

- **Spot 25180** — above PDC (bullish bias) and above the 6-bar
  ceiling (breakout), at 260+ seconds into the bar.
- **Stacked Buy: 3 lvls** — footprint showed 3:1 buying imbalance
  at three price levels; not a single-tick fluke.
- **IMBALANCE_BULLISH** — price is above weekly VAH; destination is
  new value higher, not a return to POC.
- **Bar Delta +12,400** — aggressive buyers outnumbered sellers by
  12,400 contracts in this bar.
- **Strike 25200 CE, premium ₹68** — ATM, delta ≈ 0.50. The last
  line is the risk, now shown in the message itself (PR #9):
  stop-loss ₹34 (50%), target 1 ₹102 (150%), target 2 ₹136 (200%),
  1 lot of 75.
  A 100-point NIFTY move would take the premium to ~₹129 — just
  short of target 2. A 200-point move takes it to ~₹209.
