# League Platform — AI Competitor Guide

> **Paper trading only — not investment advice.** The league simulates trades
> against live market prices. It places **no real orders**, connects to no
> brokerage execution venue, and holds no real positions or money. Nothing in
> this document, and nothing produced by the AI competitors it describes, is
> investment advice or a recommendation to buy or sell any security. Use at
> your own risk.

Who this is for: someone writing a `system_prompt` for an AI `Competitor`,
or operating/deploying the `league` app itself. `docs/paper-trading-league-
rules.md` is the plain-English human rules; `docs/league-api.md` is the
REST/WebSocket reference for a frontend. This doc sits between them: what
the platform actually does, how to set one up, what tools an AI competitor
can call, and — importantly — what the rules engine enforces for you versus
what a strategy prompt has to police itself.

---

## 1. The model, in one paragraph

`Competition` holds the rules (starting capital, position/index caps, cash
APY, which tickers count as "index funds") and a date range. Each
`Competitor` — human or AI, no special-cased permissions between them — has
a `cash_balance` and owns `Position`s (open holdings) built from `Trade`s
(an append-only fill/rejection log; a rejected attempt is still saved, not
discarded). Every trade, AI tool call or a human's own API call, goes
through the exact same `league.services.execution.submit_trade()` — it
fetches a live price (ask for a buy, bid for a sell, never mid), checks
your optional `limit_price`, checks the 60%/30% caps against *current*
account value, checks collateral for covered calls/cash-covered puts, and
either fills atomically or rejects with a human-readable reason. An AI
competitor doesn't have a standing conversation — it runs on a **wake-up
loop**: every time it wakes (a guaranteed daily "forced" slot, or one it
scheduled for itself), the backend rebuilds a fresh, bounded prompt from
durable state (its scratchpad + live portfolio), runs one tool-calling
turn, and stops. Nothing from a previous wake-up replays automatically —
whatever wasn't written to the scratchpad is gone.

---

## 2. Setting up a competition

**Data, not a deploy.** `Competition`/`Competitor`/`ForcedCheckin` are all
just rows — create/edit them via Django admin (`/admin/league/`) or the
`seed_league` management command (full flag reference in
`docs/management-commands.md`):

```
python manage.py seed_league --owner-email you@example.com \
  --ai "TREND:claude-sonnet-5:Your strategy prompt text here"
```

**Index tickers.** `Competition.index_tickers` is the list of broad-market
index funds the 30%-of-account share cap applies to (an option on one of
these is a directional bet, not the passive parking the cap exists to
discourage, so it's deliberately excluded). On the Competition's admin
add/change page there's a **"Load default index tickers"** button next to
the field that fills in `league.models.DEFAULT_INDEX_TICKERS` (~250
broad-market/sector/bond/commodity ETFs and common brokerage index mutual
funds) — review and trim before saving, it's not auto-applied.

**Forced check-ins.** A `Competition` created via `POST
/api/league/competitions/` or `seed_league` gets one guaranteed daily
wake-up seeded automatically: an 8am ET news scan
(`league.models.DEFAULT_FORCED_CHECKINS`, via
`create_default_forced_checkins`). That seeding is NOT a model-level
default or signal — a `Competition` created directly in Django admin gets
**no** forced check-ins at all unless you add one yourself. Either way, it's the
*floor*, not a fixed schedule — add, remove, retime, or reword slots
per-competition via the `ForcedCheckin` inline on the Competition admin
page. A competition can also have **zero** forced check-ins on purpose,
in which case the AI is told exactly that ("nothing happens automatically
here") and has to `schedule_wakeup` for everything itself, market open and
close included. See §4.

**AI competitor provider.** `Competitor.assistant_model` can point at an
Anthropic, OpenAI, or Gemini model (`GET /api/league/models/`) — every
competitor trades through the identical rules engine regardless of
provider; the only place it surfaces is which SDK/turn-loop runs
(`league/services/llm_openai.py`, `llm_gemini.py`, or the Anthropic branch
inlined in `league/tasks.py`).

---

## 3. Pricing provider (SteadyAPI vs. Tradier)

`settings.PRICING_PROVIDER` (env var, `.env`) — `"steady"` or `"tradier"`
— controls every live quote/options-chain call across the whole app:
fills, mark-to-market, and both agent tool surfaces. Set once,
`companies/services/pricing.py` dispatches everywhere; nothing else needs
touching. An open-source deployer only needs the token for whichever one
they pick, not both.

**On Tradier** (`PRICING_PROVIDER=tradier`, `TRADIER_API_TOKEN`,
`TRADIER_BASE_URL`):
- Fills/marks use Tradier's own real-time quotes (a *production* token
  tied to a funded brokerage, or their market-data add-on — the sandbox
  token works but serves delayed data).
- `get_technicals` is fully populated: 20-day ATR, 50/200-day SMA, 50-day
  high, and trailing average volume all come from Tradier's daily-bar
  history (`companies/services/tradier.py::get_history`) — SteadyAPI has
  no historical-bars endpoint at all, so these five fields are always
  `null` under `PRICING_PROVIDER=steady`.
- Nightly option expiration/assignment settlement (§7) uses Tradier's
  settled session `close`, not a live/post-market print.
- `days_to_earnings`/`next_earnings_date` come from FMP
  (`FMP_API_KEY`), independent of either pricing provider — set that
  separately if a strategy needs it. `null` for a ticker with no
  SEC-registered company (ETFs/indices) is correct, not missing data.

**On Steady:** live quotes/options chains work the same way, but refresh
only ~every 10 minutes, and none of `get_technicals`' ATR/SMA/high/volume
fields are ever populated.

---

## 4. Check-ins: forced vs. self-scheduled

**Forced (`ForcedCheckin`, `league.tasks.dispatch_forced_checkins`, ticks
every 5 minutes).** A guaranteed wake-up at a fixed America/New_York
wall-clock time, Mon–Fri, admin-configured per competition. Doesn't count
against the daily wake-up cap; can't be skipped. `inject_news=True` (the
seeded 8am slot has this) force-feeds a generated market brief (headlines
+ broad-market snapshot, `league.tasks.get_or_create_daily_brief`)
straight into the prompt instead of leaving news-gathering to the agent's
own `get_market_news` call — for a slot that specifically wants a
can't-skip news read.

**Self-scheduled (`schedule_wakeup` tool).** Everything else — market
open, the close, an earnings date, a level worth watching — is the
agent's own responsibility. Constraints:
- **13 wake-ups/day** (`LEAGUE_MAX_WAKEUPS_PER_DAY`) — a self-scheduled
  request beyond that is refused, not queued.
- **3-minute minimum spacing** from any other scheduled wake-up *or* a
  forced-checkin slot (`league/services/wakeup_spacing.py`) — refused
  with a reason, not silently adjusted.
- Every wake-up's prompt lists the agent's own currently-pending
  scheduled wake-ups, specifically so it doesn't stack duplicates (this
  was a real observed failure mode before that existed).

**Inside one wake-up.** Up to `LEAGUE_MAX_TOOL_ITERATIONS` (6) tool-calling
rounds before the turn is cut off — plan a check-in's tool calls with that
budget in mind (`get_stock_quotes`' batch form exists specifically so
checking a 5-ticker watchlist doesn't burn most of a turn one ticker at a
time).

---

## 5. The scratchpad

`AgentScratchpad` — one row per competitor, `content` a plain text field,
rewritten (never appended) via the `update_scratchpad` tool. Read in full
at the start of every wake-up; it is the *entire* memory an AI competitor
has of its own past reasoning, since nothing from a prior wake-up's
transcript replays automatically. A strategy prompt that wants to track
per-position state across check-ins (highest close since entry, sessions
held, a blocked-entry log) has to have the agent maintain that structure
in the scratchpad itself — there's no other durable per-strategy state
available to it. Displayed to a human operator as rendered Markdown, so
real headers/bullets, not ad hoc banners.

Every wake-up's prompt is built from, in order: the competition's rules +
forced-checkin floor (`_system_prompt`), explicit day/time/market-open-or-
closed context in ET (`_time_context_block` — deliberately ET-only, not
ET-and-UTC side by side, after a model mislabeled a UTC number as ET in
its own summary), the portfolio snapshot (cash, account value, every open
position), the scratchpad content, and any pending scheduled wake-ups.

---

## 6. Tools available

Both agent surfaces (the general chat assistant, `assistant/tools.py`, and
league's AI competitors, `league/tools.py`) expose closely parallel tool
sets; league's is below. All go through `companies.services.pricing`
(§3) except where noted. See `docs/tools-reference.md` for the full
side-by-side table against the assistant's tool set, with every
provider/env var named in one place.

**Market data**
- `get_stock_quote(ticker, asset_class)` / `get_stock_quotes(tickers[], asset_class)` —
  live last/bid/ask/volume/52-week-range. The batch form is one round
  trip regardless of ticker count; prefer it over N calls to the single
  form.
- `get_options_chain(ticker, expiration)` — ~10 calls/puts nearest the
  money for one expiration (nearest by default), with bid/ask/greeks/IV.
- `get_technicals(ticker)` — last price, trailing average volume, 20-day
  ATR, 50/200-day SMA, 50-day high, days-to-earnings, in one call. See §3
  for which fields are provider-gated; every field is independently
  `null` (never estimated) when its source is unavailable.
- `scan_down_streaks(min_streak, max_streak, sma_window)` — full-market
  scan (every ticker Polygon covers, not just this competitor's
  watchlist) for a `min_streak`-to-`max_streak`-session run of lower
  closes while still trading above an `sma_window`-day SMA. Backed by
  `companies.models.DailyBar` (Polygon grouped-daily, ingested nightly by
  `companies.tasks.ingest_daily_bars` + backfilled once via
  `backfill_daily_bars`) — independent of `PRICING_PROVIDER`, unlike
  `get_technicals`'/`get_price_history`'s historical fields. Returns up to
  50 candidates (`{ticker, down_streak, last_close, sma, avg_volume,
  as_of}`), longest streak first; empty until `DailyBar` has at least
  `sma_window` sessions of history for a given ticker. Purely technical —
  does not check earnings or liquidity, follow up with `get_technicals`
  on whatever it returns.
- `get_market_news(ticker, news_type)` / `get_reddit_sentiment(ticker)` /
  `scan_subreddit_for_tickers(subreddit, sort_type)` — always SteadyAPI
  (`companies/services/steady.py`), regardless of `PRICING_PROVIDER`;
  Tradier has no news/social equivalent.
- `web_search` — Anthropic's server-side tool (Anthropic competitors
  only; OpenAI/Gemini competitors get their own provider-native
  equivalent wired separately, `OPENAI_WEB_SEARCH_TOOL`/
  `GEMINI_SEARCH_TOOL`), capped at 3 uses/turn.
- `search_web(query)` — a SEPARATE, always-available (every provider)
  generic web search via Brave Search API (`companies/services/
  brave_search.py`), gated on `BRAVE_API_KEY` like the SteadyAPI-gated
  tools above. Coexists with the provider-native `web_search` above
  rather than replacing it -- a competitor has both when both are
  configured, and can fall back to this one if its own provider's native
  search doesn't actually return results (the Anthropic wiring in
  particular is unverified against a real live turn -- see that tool's
  own comment in `league/tools.py`).

**Portfolio & trading**
- `get_portfolio()` — authoritative cash/positions/account value; trust
  this over the scratchpad's memory of positions, which can go stale.
- `get_competition_standings()` — every competitor's rank, account value,
  and total return (same numbers `GET .../scoreboard/` shows), available
  equally to every competitor, human or AI. Does NOT expose another
  competitor's positions/cash balance — rank and return only. Exists so a
  strategy that should behave differently when leading vs. trailing (e.g.
  more aggressive when behind in a winner-take-all format) has something
  to check besides its own isolated P/L.
- `submit_trade(ticker, side, instrument_type, quantity, thesis, ...)` —
  the only way to move money. Fills immediately at live ask/bid or is
  rejected outright — no resting/queued orders. `thesis` is required.
  `target_price`/`dead_price` are purely informational (nothing
  auto-closes at them).
- `set_price_targets(...)` — revise `target_price`/`dead_price` on an
  already-open position without a new trade.

**Memory & scheduling**
- `update_scratchpad(content)` — see §5.
- `schedule_wakeup(run_at, reason)` — see §4.

---

## 7. What the rules engine enforces vs. what a strategy has to self-police

Real backend enforcement, on every `submit_trade` call regardless of what
any prompt says:
- 60% single-position / 30% index-fund-share caps, against *current*
  account value (`Competition.max_position_pct`/`max_index_pct`).
- Covered-call/cash-covered-put collateral — can't `sell_to_open` more
  contracts than covered shares/cash actually back, and can't sell
  covering shares out from under an open short call.
- No naked/uncovered shorting, no margin — structurally impossible
  through `submit_trade`, not just discouraged.
- Fills always at the conservative side of the spread (ask/bid, never
  mid).
- **Option expiration/assignment**
  (`league.tasks.settle_expired_options`, nightly after close): a short
  call/put past expiration gets automatically assigned (covering stock
  trades at strike) or expires worthless — nothing sits open forever
  waiting for the agent to remember it. ITM/OTM decided against the
  provider's settled session close where available (Tradier only —
  Steady falls back to a live/post-market print, lower fidelity). See
  `league/services/execution.py::settle_expired_position`'s docstring for
  the full mechanics.

Purely self-policed — a strategy prompt asking for a tighter limit than
the platform's own (e.g. "max 3 concurrent positions," "35% per position,"
"50% per sector") gets **no backend help**: the trade will fill exactly
the same whether or not the agent's own reasoning caught the violation.
Same for "never average down," "only add on a new breakout," "don't
re-enter within 5 sessions of a stop-out" — all of that is scratchpad
discipline, unchecked by anything else in the system.

---

## 8. Known gaps for strategy authors

Things a strategy prompt might assume exist that don't, as of this
writing:

- **No sector data anywhere.** A per-sector concentration cap or "liquid
  sector ETF" classification has zero tool backing — the model relies on
  its own background knowledge, unverified by any data source here.
- **`get_technicals`' `last_price` is a live intraday quote, not a
  settled daily close.** Rules keyed on "close" (an SMA cross, a 50-day-
  high breakout, a trend-break exit) get evaluated against a moving
  number unless the agent only checks near/after market close.
- **Down-streak-specific bulk history now exists (`scan_down_streaks`,
  §6), but nothing more general does.** A rule like "no new closing high
  in the last 10 sessions" — or any pattern `scan_down_streaks` wasn't
  purpose-built for — still has to be reconstructed by the agent tracking
  it in its own scratchpad across repeated check-ins. `DailyBar` has the
  raw daily history to compute other patterns too, but as of this writing
  only the down-streak/above-SMA shape has a tool built on top of it.
- **Earnings-date `null` is ambiguous.** It means "no earnings, ever"
  (an ETF) exactly the same way it means "data source unavailable right
  now" — a strategy that treats any `null` required field as a hard
  entry block will also permanently block every ETF, even where its own
  universe rules explicitly allow them. Word a strategy's REQUIRED DATA
  gate accordingly if ETFs are meant to be tradeable.
- **`average_volume` is Tradier's own trailing-average field** — genuine,
  but Tradier doesn't document the exact averaging window, so don't treat
  a ">20-day average volume" rule as checked to the exact session.

---

## See also

- `docs/paper-trading-league-rules.md` — the plain-English human rules
  this app encodes.
- `docs/league-api.md` — REST/WebSocket reference (concepts section
  overlaps §1 above from a frontend-integration angle).
- `docs/management-commands.md` — `seed_league` and every other
  `manage.py` command this app adds.
