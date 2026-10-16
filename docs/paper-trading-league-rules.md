# Medium-Term Paper Trading League — Official Rules

> **Paper trading only — not investment advice.** The league simulates trades
> against live market prices. It places **no real orders**, connects to no
> brokerage execution venue, and holds no real positions or money. Nothing in
> this document, and nothing produced by the AI competitors it describes, is
> investment advice or a recommendation to buy or sell any security. Use at
> your own risk.

Source document for the `league` app (`docs/league-api.md` for the API/data
model this maps to). Reproduced verbatim from the original rules as given —
this is the plain-English source of truth; `league/services/execution.py`
and `league/models.py::Competition` are the code's attempt to encode it
exactly, and code comments throughout that app point back to specific
numbered rules below.

**Competitors:** George vs. Claude
**Start:** Wednesday, August 12, 2026 (market open)
**End:** Week of November 4, 2026 (12 weeks)
**Starting capital:** $5,000 each
**Stakes:** Zero. This is 100% paper money. No real trades, no real dollars, ever.

---

## 1. What you can trade

Anything permitted in a Fidelity Tier 1 options account:

- Stocks and ETFs (long only)
- Long calls and long puts
- Covered calls and buy-writes (requires owning 100 shares)
- Cash-covered puts (requires full cash collateral set aside)
- Long straddles and strangles

**Not allowed:** margin, short selling, naked options, spreads, CFDs, futures, crypto — anything above Tier 1.

**Note on account size:** At $5,000, premium-selling strategies are only
available on low-priced underlyings. A covered call or cash-covered put on
SPY would require ~$77,000 of collateral. On SPY we are effectively limited
to long calls and long puts.

## 2. Position limits

| Rule | Limit |
|---|---|
| Max in any single position, at entry | 60% of account value |
| Max in broad-market index fund shares | 30% of account value |

"Account value" means current account value at the moment of entry — cash
plus the market value of all open positions — not a fixed percentage of the
original $5,000. Options are marked at the bid for this calculation, since
that's what you'd actually get exiting.

Both caps work this way. A fixed dollar cap would stop constraining exactly
when constraint matters most: if the account fell to $2,500, a frozen
$3,000 limit would permit putting the entire remaining balance into one
trade.

| Account value at entry | Max single position | Max index shares |
|---|---|---|
| $5,000 | $3,000 | $1,500 |
| $6,500 | $3,900 | $1,950 |
| $3,500 | $2,100 | $1,050 |

This means whoever is ahead can size larger in absolute dollars, and a bad
stretch mechanically shrinks your ammo. That's not a flaw — it's how a real
account behaves, and it's what makes compounding feel different from a
fixed-bet game.

Index cap applies to shares, not options. Buying SPY/VOO/QQQ/FXAIX shares is
the passive strategy this cap exists to discourage. Buying a SPY call or put
is a directional bet with an expiration date and theta working against it —
that's an active trade and is not counted against the 30%.

The 60% cap is measured at entry only. If a position runs and grows past
60% of the account, that's a win, not a violation — no forced trimming.

## 3. Fills

Buy at the ask. Sell at the bid. No mid-price fills.

Every fill must be supported by a real-time screenshot from Fidelity taken
at the time the trade is posted.

No backdating. No "I would have gotten in at the low." The price is
whatever the screenshot says when the trade is called.

Options are 100 shares per contract. A contract quoted at $2.50 costs $250.

Why bid/ask instead of mid: you can often get filled near the mid with a
patient limit order, but never reliably. Using the conservative side of the
spread means a strategy that only works at mid prices is correctly shown as
not working.

## 4. Cash

Uninvested cash sits in SPAXX (Fidelity Government Money Market Fund) and
earns 3.3% APY, accrued weekly at each check-in.

Weekly accrual = cash balance × 3.3% ÷ 52 ≈ 0.0635%

On $5,000 fully in cash, that's about $3.17/week, or roughly $38 over the
full 12 weeks.

Sitting in cash is a legitimate strategy and gets paid a little for it —
but not enough to win.

## 5. Scoring

Two numbers are tracked every week:

- **Total return** — percent change from the $5,000 starting balance.
- **Max drawdown** — the largest peak-to-trough decline in account value, measured on weekly marks.

Total return decides the winner. Max drawdown is tracked alongside it so
the scoreboard shows how much risk was taken to get there — a +14% finish
that survived a −40% hole is a very different result from a +12% that
never dipped below break-even.

Weekly W-L record is also tracked (who gained more that specific week), for
bragging rights only.

## 6. Cadence

At least one check-in per week. Trades can be posted any time between
check-ins. (This is the default -- an operator can restrict a specific
competition to market-hours-only fills, simulating a broker without
extended-hours trading; an order submitted after hours there queues
instead and fills at the next open.)

Both sides research independently. Discussion, trash talk, and arguing each
other's theses are all encouraged.

Every position needs a written thesis at entry, so it can be graded
honestly at the end.

## 7. No incentives or injections

Straight $5,000 each, no weekly cash bonuses to the leader. Injections
would compound an early lead and pressure whoever is behind into taking
increasingly reckless bets — which produces bad trades, not better ones.

## 8. The point

The format exists to encourage active, shorter-term decision-making rather
than parking the money in an index fund and waiting 12 weeks. Every trade
should be one that could plausibly be placed in a real Tier 1 Fidelity
account of this size.

---

Paper money only. Nothing in this competition is investment advice, and
nothing done here should inform decisions in a real brokerage account.

---

## What changed going from a manual competition to the `league` app

The rules above were written for a manual process (screenshot each fill,
log each trade by hand). Automating it via `league` app (see
`docs/league-api.md`) required a few concrete decisions where the rules
doc is either silent or assumes a human with a brokerage UI:

- **"Real-time screenshot from Fidelity"** → replaced by a live SteadyAPI
  quote/options-chain fetch at the moment `submit_trade()` runs
  (`companies/services/steady.py`). Same intent (a real, live, conservative
  fill price, not a stale or cherry-picked one) via an API instead of a
  screenshot.
- **`limit_price`** isn't in the original rules (no resting-order concept
  for a manual/screenshot process) — added so an AI competitor (or you) can
  guard against the price having moved between when it decided to trade and
  when the order actually executes. It's a same-instant guard, not a
  resting order — see `docs/league-api.md`'s rules-engine section.
- **Weekly W-L record** (rule 5's bragging-rights line) isn't currently
  computed by the API — `WeeklyMark` history exists to support it later,
  but the scoreboard endpoint only returns total return and max drawdown
  today.
