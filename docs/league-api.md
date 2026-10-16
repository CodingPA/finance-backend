# Paper Trading League — API & Concepts

> **Paper trading only — not investment advice.** The league simulates trades
> against live market prices. It places **no real orders**, connects to no
> brokerage execution venue, and holds no real positions or money. Nothing in
> this document, and nothing produced by the AI competitors it describes, is
> investment advice or a recommendation to buy or sell any security. Use at
> your own risk.

Reference for the `league` app: the Medium-Term Paper Trading League
(`docs/paper-trading-league-rules.md`) automated so AI competitors can
research and trade autonomously, and you can too, all under one
backend-enforced ruleset. Same conventions as `docs/api-endpoints.md`
(method/path/auth/request/response) and `docs/websockets.md`
(notification events) — this doc adds the concepts a frontend needs to
make sense of the shapes, since this system (unlike the rest of the API)
has real domain logic behind it, not just CRUD.

## Concepts (read this before the endpoints)

**Competition** → **Competitor** → **Position**/**Trade**. A `Competition`
holds the rules (starting capital, position/index caps, cash APY, which
tickers count as "index funds") and the date range. Each `Competitor` —
human or AI, no special-cased permissions between them — has its own
`cash_balance` and owns a set of `Position`s (open holdings) built up from
`Trade`s (the append-only fill/rejection log). **Balances are never

entered by hand** — `cash_balance` only changes as a side effect of a
filled `Trade`, computed by the rules engine
(`league/services/execution.py`). Current account value (cash + every open
position marked to market) is read off each ticker's **last refreshed**
quote (`TickerSnapshot`, kept warm by a Beat job + every fill + on-demand
refresh — see `POST .../refresh/` below and "Live competition feed" near
the end of this doc), not fetched live on every request — see
`account_value` in the responses below.

**The rules engine, in one paragraph:** every trade — AI tool call or your
own `POST .../trades/` — goes through the exact same
`execution.submit_trade()`. It fetches a live price from SteadyAPI (ask
for a buy, bid for a sell — never mid), checks it against your optional
`limit_price` (see below), checks the 60%/30% caps against *current*
account value, checks collateral for covered calls/cash-covered puts, and
either fills atomically or rejects with a human-readable reason — which
is still saved as a `Trade` row (`status: "rejected"`), so a rejected
attempt is part of the audit trail, not silently dropped.

**`limit_price` is not a resting order.** There's no order book and
nothing waits for a price to be crossed later. It's a same-instant guard:
"don't fill this if the live price has moved past X since I decided to
trade" — checked once, at the moment `submit_trade` runs, then forgotten.
If you want a real resting limit order, that's not built (see this
feature's plan — deferred deliberately).

**AI competitors run on a wake-up loop, not a chat.** An AI `Competitor`
has an `assistant_model` and a `system_prompt` (its persona/strategy
voice — "Claude Regular" vs. "Claude Lunatic" are the same underlying
model, distinguished only by this field). It doesn't have a standing
conversation; every time it wakes up (scheduled by itself, or triggered
by `POST .../wakeups/`), the backend rebuilds a **fresh, bounded prompt**
from durable state — its `AgentScratchpad` (a short, self-curated working
memory it rewrites every time, not an ever-growing log) plus its live
portfolio — runs one tool-calling turn (it can check quotes/options/news,
search the web, submit trades, update its scratchpad, and schedule its
own next wake-up), and stops. Nothing from a previous wake-up is replayed
automatically; whatever the agent didn't write into its scratchpad is
gone. Each wake-up produces one `AgentTurn` — the full, un-summarized
transcript (every tool call, every search, every word) for you to audit,
plus token/cost totals.

**Multiple providers, one ledger.** `assistant_model` can point at an
Anthropic model (e.g. `claude-sonnet-5`), an OpenAI model (e.g.
`gpt-5.6-sol`), or a Gemini model (e.g.
`gemini-3.1-pro-preview-customtools`) — check via `GET /api/league/models/`
(below), not `GET /api/assistant/models/`, which is scoped to
Anthropic-only (that's the *main chat's* picker, a separate app with an
Anthropic-only call loop). Every competitor trades through the identical
rules engine and
`Trade`/`Position` shapes regardless of provider — the only place
provider actually surfaces in the API is `assistant_model`'s value and
`transcript`'s internal shape on a turn (see `GET .../turns/{id}/`
below), never a separate endpoint or field you need to branch on for
trading/scoreboard purposes.

**Everything is auto-calculated.** `cash_balance`, `account_value`,
`total_return_pct`, `max_drawdown_pct` are all derived — there is no
field anywhere in this API you're expected to type a balance into.

**No PATCH/PUT here.** Editing an existing competition's rules or a
competitor's persona after creation goes through Django admin
(`/admin/`), not this API — deliberately out of scope for now.

---

## Conventions

Same as `docs/api-endpoints.md`: JSON bodies, `Content-Type:
application/json`, `Authorization: Bearer <access_token>` on every
endpoint below (all of them require auth), `400` validation errors with
field-keyed message arrays, integer IDs (not UUIDs, unlike
`accounts.User`). Decimal fields (`cash_balance`, `account_value`,
`fill_price`, etc.) are serialized as **strings**, not JSON numbers —
parse them as decimals client-side rather than `parseFloat`, to avoid
floating-point drift on money math.

---

## `GET /api/league/models/`

Every active `AssistantModel` a league AI competitor can be created with
— across **all** providers, unlike `GET /api/assistant/models/` (the main
chat's picker, Anthropic-only). Use this one for a league competitor
creation form's model dropdown.

**Response `200`:**
```json
[
  { "model_id": "claude-sonnet-5", "display_name": "Claude Sonnet 5", "provider": "anthropic", "is_default": true },
  { "model_id": "claude-opus-5", "display_name": "Claude Opus 5", "provider": "anthropic", "is_default": false },
  { "model_id": "gpt-5.6-sol", "display_name": "GPT-5.6 Sol", "provider": "openai", "is_default": false },
  { "model_id": "gemini-3.1-pro-preview-customtools", "display_name": "Gemini 3.1 Pro Preview", "provider": "gemini", "is_default": false }
]
```
`is_default` is which model `POST .../competitors/` picks if you omit
`model` entirely — currently always an Anthropic row; there's no
scenario where omitting `model` gets you an OpenAI competitor by
accident.

---

## `GET /api/league/briefs/`

Generated market briefs (headlines + a broad-market ETF snapshot) —
newest first. Global, **not** scoped to a competition: one `DailyBrief`
row per `(date, slot)`, shared across every competition whose
`ForcedCheckin` rows inject news with that `news_slot` — see "Guaranteed
floor wake-ups are now customizable" under `GET .../wakeups/next/` below
for how a slot gets generated in the first place. This is a read-only
view onto what an AI competitor's inject_news wake-ups already received
in their prompt, for a frontend "here's today's brief" panel.

**Query params (all optional):**
- `?slot=morning` — narrow to one slot (e.g. `morning`, `noon`; whatever
  string a competition's `ForcedCheckin.news_slot` uses).
- `?since=2026-08-01&until=2026-08-13` — plain `YYYY-MM-DD` dates
  (**not** ISO datetimes like `since`/`until` elsewhere in this doc —
  `DailyBrief.date` has no time component), inclusive both ends.
- `?limit=30` — max rows returned, default 30, newest-first.

There's no separate "latest brief" endpoint — `?slot=morning&limit=1`
(or just the first item of an unfiltered list) is the latest brief for
that slot.

**Response `200`:**
```json
[
  {
    "id": 14, "date": "2026-08-19", "slot": "morning",
    "content": "Headlines:\n- Fed holds rates steady [Reuters] (2h ago)\n- ...\n\nBroad market:\n- SPY: 512.34 (+0.42%)\n- QQQ: 441.02 (+0.61%)\n- DIA: 389.10 (-0.05%)\n- IWM: 201.77 (+0.88%)",
    "generated_at": "2026-08-19T12:01:03.221Z"
  },
  {
    "id": 13, "date": "2026-08-18", "slot": "morning",
    "content": "Headlines:\n- ...",
    "generated_at": "2026-08-18T12:00:47.905Z"
  }
]
```
`content` is plain text (not markdown/HTML), formatted for direct prompt
injection — `"Broad market"` is omitted from a brief entirely if Steady
quotes weren't available when it was generated (`STEADY_API_TOKEN`
unconfigured, or every quote call failed), not padded with placeholders.

**Errors:**
- `400` — `since`/`until` isn't a valid `YYYY-MM-DD` date, or `limit`
  isn't an integer.

---

## `GET /api/league/competitions/`

Every competition.

**Response `200`** (real data from this app's seeded league):
```json
[
  {
    "id": 5, "name": "Medium-Term Paper Trading League", "status": "upcoming",
    "starting_capital": "5000.00", "max_position_pct": "0.6000", "max_index_pct": "0.3000",
    "cash_apy": "0.0330", "index_tickers": ["SPY", "VOO", "QQQ", "FXAIX"],
    "start_date": "2026-08-12", "end_date": "2026-11-04", "competitor_count": 2
  }
]
```
`status` is one of `upcoming`/`active`/`completed` — informational only
right now, nothing in the backend flips it automatically based on
`start_date`/`end_date`; set it by hand via admin if you want it to
reflect reality.

---

## `POST /api/league/competitions/`

Create a competition. Rules become data on this row — read them back via
the `GET` above or `.../scoreboard/`, never hardcoded elsewhere.

**Request body:**
```json
{
  "name": "Fall 2026 League",
  "start_date": "2026-08-12",
  "end_date": "2026-11-04",
  "starting_capital": "5000",
  "max_position_pct": "0.60",
  "max_index_pct": "0.30",
  "cash_apy": "0.033",
  "index_tickers": ["SPY", "VOO", "QQQ", "FXAIX"]
}
```
Only `name`, `start_date`, `end_date` are required — everything else
defaults to the values shown (the rules doc's own numbers). Send
`index_tickers` as a real JSON array; if you're testing with a plain HTML
form (multipart encoding) rather than a JSON body, a list value gets
flattened to its last item — use `Content-Type: application/json`.

**Response `201`:** the created competition, same shape as the `GET` above.

**Errors:**
- `400` — `name` missing, dates missing/unparseable, or a numeric field
  that doesn't parse as a decimal.

---

## `GET /api/league/competitions/{id}/scoreboard/`

Every competitor in a competition, ranked. This is the "who's winning"
view — for a single competitor's own portfolio/positions detail, use
`GET /api/league/competitors/{id}/` instead.

**Response `200`:**
```json
{
  "competition": "Medium-Term Paper Trading League",
  "competitors": [
    {
      "id": 6, "name": "Claude Regular", "kind": "ai",
      "cash_balance": "5000.00", "account_value": "5000.00",
      "total_return_pct": "0", "max_drawdown_pct": "0",
      "filled_trades_count": 3, "api_cost_usd": "1.621098", "last_woke_at": "2026-08-13T00:38:47.971120Z"
    },
    {
      "id": 7, "name": "Claude Lunatic", "kind": "ai",
      "cash_balance": "5000.00", "account_value": "5000.00",
      "total_return_pct": "0", "max_drawdown_pct": "0",
      "filled_trades_count": 0, "api_cost_usd": null, "last_woke_at": null
    }
  ],
  "rejected_trades": [
    { "reason": "price_limit", "count": 3 },
    { "reason": "position_cap", "count": 1 }
  ]
}
```
Sorted best `total_return_pct` first, `id` descending as a tiebreak — two
competitors tied at `0.0`/starting capital (common early in a competition)
would otherwise reorder arbitrarily on every refresh; `id` carries no
ranking meaning, it's purely for stable ordering. `account_value`/
`total_return_pct` are marked against each ticker's **last refreshed**
quote, not a live Steady call made on this request — see `POST
.../refresh/` below for why and how to force a fresher one. `max_drawdown_pct`
comes from the recorded `WeeklyMark` history instead (the rules doc's
scoring rule 5 is explicitly about the weekly series, not an instantaneous
number), so it only moves once a week, after `mark_weekly_values` runs
(Sunday 22:00 UTC, `core/settings.py`'s Celery Beat schedule) — it'll read
`"0"` for a competition that hasn't completed its first week yet, same as
above.

`filled_trades_count` is this competitor's count of `Trade` rows with
`status: "filled"`. `api_cost_usd` is the sum of `cost_usd` across every
`AgentTurn` this competitor has ever produced — `null` (not `"0"`) if it
has none, which is always true for a human competitor and also true for an
AI that's never woken. **Undercounts a failed wake-up**: if a wake-up
raises before its turn is fully recorded, no `AgentTurn` row exists for
that attempt at all, so tokens it burned aren't reflected here — check
`GET .../wakeups/` for `status: "failed"` rows if you need the full
picture. `last_woke_at` is the `created_at` of the most recent `AgentTurn`
— i.e. most recent *completed* turn, not most recent *attempted* wake-up
(a currently-in-flight or failed one won't move this) — `null` if none.

`rejected_trades` is competition-wide counts of rejected `Trade` rows by
`rejection_code` (see `GET .../trades/` below for the full taxonomy),
sorted most-common first. Trades rejected before this field existed have
`rejection_code: ""` and are **excluded** from this breakdown rather than
folded into a misleading bucket — so this count reflects "since structured
rejection reasons went live," not a full historical total.

**Errors:**
- `404` — no competition with that id.

---

## `GET /api/league/competitions/{id}/trades/`

Every trade (filled **and** rejected) across **every** competitor in this
competition — human and AI alike — newest first. This is the "what is
everyone doing" monitoring view; `GET /api/league/competitors/{id}/trades/`
above is the same data scoped to one competitor's own history instead.

**Query params (all optional):**
- `since` / `until` — `YYYY-MM-DD` or full ISO 8601, same semantics as
  `GET .../equity-curve/`'s (`_parse_date_boundary`): a bare date is
  anchored to America/New_York and treated as whole-days-inclusive
  (`since` = start of that day, `until` = end of that day), not midnight-
  to-midnight UTC.
- `competitor_kind` — `ai` or `human`, narrows to one side.

**Response `200`:**
```json
[
  {
    "id": 88, "competitor_id": 6, "competitor_name": "Claude Regular", "competitor_kind": "ai",
    "side": "buy", "instrument_type": "stock", "ticker": "RKLB",
    "strike": null, "expiration": null, "quantity": 30,
    "limit_price": null, "fill_price": "24.1667", "thesis": "Backlog $2.2B vs $600M revenue.",
    "status": "filled", "rejection_reason": "", "rejection_code": "", "created_at": "2026-08-13T14:22:01Z"
  }
]
```
Same row shape as `GET /api/league/competitors/{id}/trades/` above, plus
`competitor_id`/`competitor_name`/`competitor_kind` since a cross-
competitor feed needs the identity a single-competitor URL already
implies. Live updates for this same data arrive over
`ws://<host>/ws/league/{competition_id}/` as `league.trade` (see
`docs/websockets.md`) the instant a trade is created — this endpoint is
the initial page load / catch-up-after-disconnect source of truth, not
meant to be polled.

**Errors:**
- `404` — no competition with that id.
- `400` — `competitor_kind` isn't `ai` or `human`.

---

## `POST /api/league/competitions/{id}/refresh/`

Force a live re-quote of every open position across every competitor in
this competition, right now, instead of waiting for the next scheduled
refresh. Normally you don't need this: `account_value`/`current_price`
everywhere in this API are read off a persisted "last known quote" table
(`TickerSnapshot`) that a Celery Beat job keeps warm every 10 minutes
during market hours (9:30am–4:00pm ET, Mon–Fri) — trading fills also
refresh their own ticker's snapshot the instant they happen, so it's
usually already fresh. This endpoint is for "I want it fresher than that
right now" (e.g. right after market open, or before eyeballing the
scoreboard for a decision).

**Response `202`:** `{ "status": "queued", "competition_id": 3 }` — **not**
the refreshed scoreboard. Dispatched asynchronously, same pattern as
`POST /api/league/competitors/{id}/wakeups/` below: this just confirms
it's queued. The actual result arrives over
`ws://<host>/ws/league/{competition_id}/` (`docs/websockets.md`'s
connection protocol) as a `league.scoreboard_refreshed` event once every
held ticker/contract has been re-fetched (in parallel, so this is
seconds, not the old N-live-calls-in-a-row cost this replaced):
```json
{
  "type": "league.scoreboard_refreshed",
  "competition": "Medium-Term Paper Trading League",
  "competitors": [ /* same row shape as GET .../scoreboard/ above */ ],
  "rejected_trades": [ /* same shape as GET .../scoreboard/'s rejected_trades above */ ]
}
```
See `POST /api/league/competitors/{id}/refresh/` below for the
single-competitor version (`league.competitor_refreshed`) — both land on
the same competition-wide socket, so a page watching the scoreboard
doesn't need to know which kind of refresh was triggered to react to it.

**Errors:**
- `404` — no competition with that id.

---

## `GET /api/league/competitions/{id}/equity-curve/`

Intraday account-value history for every competitor in a competition, one
series per competitor, in a single call — the source for both the league
page's hero equity chart and the per-row standings sparklines (same
payload serves both; downsample client-side for the sparklines, there's no
separate coarse endpoint).

**Query params (optional):** `?since=`/`?until=` (`YYYY-MM-DD` or full ISO
8601), same semantics as `GET .../trades/`'s (`_parse_date_boundary`) — a
bare date is anchored to America/New_York, whole-days-inclusive. No
`?interval=` downsampling — full resolution always, points are cheap
enough (~39/day/competitor) that there's no need for a second granularity.

**Response `200`:**
```json
[
  {
    "competitor_id": 6, "name": "Claude Regular",
    "points": [
      { "at": "2026-08-12T13:30:00Z", "account_value": "5000.00" },
      { "at": "2026-08-12T13:40:00Z", "account_value": "5012.40" }
    ]
  },
  { "competitor_id": 7, "name": "Claude Lunatic", "points": [] }
]
```
One entry **per competitor in the competition, always** — a competitor
with no snapshots yet (added after their last job tick, or before this
feature shipped for them) gets `"points": []`, not an absent entry, so the
frontend never has to special-case a missing series.

Points come from a new `AccountValueSnapshot` row written by the same
Celery Beat job that keeps `TickerSnapshot` warm
(`league.tasks.refresh_open_position_quotes`, every ~10 minutes, market
hours only — 9:30am–4:00pm ET, Mon–Fri) — one snapshot per competitor per
tick, including all-cash competitors with no open positions (so a flat
account renders as a flat line, not a gap). Because it only ticks during
market hours, **there are gaps overnight and on weekends** — connect
across them rather than treating them as missing data.

**No historical backfill.** `TickerSnapshot` (the quote cache
`account_value` is marked against elsewhere in this API) only ever holds
the *latest* quote per ticker — there's no price history to reconstruct a
past mark-to-market from. A competitor's series starts at whenever this
snapshot job first ran for them, not at competition start; a competitor
added mid-competition simply has a shorter series starting at their own
first snapshot rather than one padded flat back to competition start —
expect series of differing start times and lengths, not equal-length ones.

**Errors:**
- `404` — no competition with that id.

---

## `GET /api/league/competitions/{id}/competitors/`

Every competitor in a competition, fuller detail than the scoreboard
(persona/model for AI competitors).

**Response `200`:** real data —
```json
[
  {
    "id": 6, "name": "Claude Regular", "kind": "ai",
    "cash_balance": "5000.00", "account_value": "5000.00", "total_return_pct": "0",
    "filled_trades_count": 3, "api_cost_usd": "1.621098", "last_woke_at": "2026-08-13T00:38:47.971120Z",
    "user_email": "user@example.com",
    "assistant_model": "claude-sonnet-5",
    "system_prompt": "Your trading style is disciplined and measured. Favor high-conviction setups with a clear, falsifiable thesis over frequent activity for its own sake. ..."
  },
  {
    "id": 7, "name": "Claude Lunatic", "kind": "ai",
    "cash_balance": "5000.00", "account_value": "5000.00", "total_return_pct": "0",
    "filled_trades_count": 0, "api_cost_usd": null, "last_woke_at": null,
    "user_email": "user@example.com",
    "assistant_model": "claude-sonnet-5",
    "system_prompt": "Your trading style is aggressive and high-conviction. ..."
  }
]
```
`assistant_model`/`system_prompt` only appear for `"kind": "ai"` rows — a
human competitor's row has neither key at all (not `null` — absent).
`filled_trades_count`/`api_cost_usd`/`last_woke_at` are the same fields
and caveats as `GET .../scoreboard/` above (`account_value`'s section) —
present on every row regardless of `kind`, `null`/`0` for a competitor
with no filled trades or turns.

**Errors:**
- `404` — no competition with that id.

---

## `POST /api/league/competitions/{id}/competitors/`

Add a competitor. `cash_balance` is always seeded from the competition's
`starting_capital` — there's no field to set it yourself.

**Request body (AI):**
```json
{
  "name": "Claude Contrarian",
  "kind": "ai",
  "model": "claude-opus-5",
  "system_prompt": "You specifically look for setups where consensus seems wrong."
}
```
`model` is optional — omit it to use whichever `AssistantModel` is
flagged default (`GET /api/league/models/`, above — not
`/api/assistant/models/`, which only shows Anthropic options). Pass
`"model": "gpt-5.6-sol"` or `"model": "gemini-3.1-pro-preview-customtools"`
for a GPT- or Gemini-backed competitor instead of a Claude one — same
request shape either way, `execution.py`/the trade API don't know or care
which provider a competitor runs on. `system_prompt` is
optional too (blank persona is valid, just gives the agent no particular
voice beyond the rules themselves).

**Request body (human — you):**
```json
{ "name": "Me" }
```
`kind` defaults to nothing — you must pass `"kind": "human"` explicitly;
shown minimal here because everything else is irrelevant for a human row.
The competitor is attached to **you**, the requesting user, regardless of
kind — this app has one human operator/player right now (see
`docs/league-api.md`'s note above and this feature's plan), not a
multi-user signup flow. That's also who gets notified
(`league.trade_activity`/`league.wakeup_complete`/`league.wakeup_failed`,
below) when an AI competitor you created does something.

**Response `201`:** the created competitor, same shape as the `GET` above.

**Errors:**
- `400` — `name` missing, `kind` not `ai`/`human`, `model` names an
  inactive/unknown `AssistantModel`, or a competitor with that name
  already exists in this competition (names are unique per competition,
  not globally).
- `404` — no competition with that id.

---

## `GET /api/league/competitors/{id}/`

One competitor's cash, account value, return, open positions — plus, for
an AI competitor, its model/persona (same fields
`GET /api/league/competitions/{id}/competitors/`'s list already returns;
this single-row view used to be missing them, fixed).

**Response `200`:**
```json
{
  "id": 6, "name": "Claude Regular", "kind": "ai",
  "cash_balance": "4275.00", "account_value": "4540.00", "total_return_pct": "-0.092",
  "user_email": "user@example.com",
  "assistant_model": "claude-sonnet-5", "provider": "anthropic",
  "system_prompt": "Your trading style is disciplined and measured. ...",
  "prices_as_of": "2026-08-14T19:16:03Z",
  "positions": [
    {
      "id": 88, "instrument_type": "stock", "ticker": "RKLB", "strike": null, "expiration": null,
      "quantity": 30, "is_short": false, "avg_cost": "24.1667",
      "current_price": "26.40", "current_value": "792.00", "priced_at": "2026-08-14T19:16:03Z",
      "target_price": "30.00", "dead_price": "21.00",
      "opened_at": "2026-08-13T14:22:01Z"
    },
    {
      "id": 91, "instrument_type": "call", "ticker": "SPY", "strike": "650.0000", "expiration": "2026-09-19",
      "quantity": 1, "is_short": false, "avg_cost": "8.20",
      "current_price": "7.55", "current_value": "755.00", "priced_at": "2026-08-14T19:20:11Z",
      "target_price": null, "dead_price": null,
      "opened_at": "2026-08-14T15:05:44Z"
    }
  ]
}
```
`user_email`/`assistant_model`/`provider`/`system_prompt` only appear for
`"kind": "ai"` rows — a human competitor's row has none of these keys at
all (not `null` — absent), same convention as the competitors-list
endpoint. `provider` is worth checking before rendering a turn's
`transcript` (`GET .../turns/{id}/`, below) — its internal shape differs
per provider with no normalization layer.

`id` is this `Position`'s id — the `{position_id}` you need to build
`POST .../positions/{position_id}/price-targets/` below. `strike`/
`expiration` are `null` for `instrument_type: "stock"` rows.
`is_short` is only ever `true` for the option leg of a covered call or
cash-covered put — this league disallows naked shorting entirely, so
you'll never see a short stock position. `avg_cost` is per-share (stock)
or per-contract (options, i.e. the premium — multiply by `quantity * 100`
for the total dollar cost/credit, same as the rules doc's "$2.50 quoted =
$250 per contract" convention). `current_price`/`current_value` are marks
(`valuation.mark_position_detail` — last sale for stock, bid for options,
same convention `account_value` itself uses) read off the persisted
`TickerSnapshot` table, not fetched live on every request — see
`POST .../refresh/` below for how that table stays warm and how to force
an update. A short position's `current_value` is negative (what it'd cost
to buy back right now, a liability).

`target_price`/`dead_price` are purely informational take-profit/
invalidation levels the competitor recorded — `null` if never set.
**Nothing enforces these** — no auto-close, no alert, the competitor
still has to submit a real trade when they act. Set optionally at open
time (`POST .../trades/`'s own `target_price`/`dead_price` fields, or the
AI's `submit_trade` tool args) and revisable anytime afterward on an
already-open position without a new trade, via
`POST .../positions/{id}/price-targets/` below (or the AI's
`set_price_targets` tool) — deliberately not fixed at entry, since a
competitor routinely moves a stop up or a target out on a position it
already holds.

`priced_at` is the timestamp of the `TickerSnapshot` this specific mark
came from — `null` when there's no snapshot yet and `current_price`/
`current_value` fell back to `avg_cost` (that fallback isn't a live mark
at all, so it gets no timestamp rather than a misleading one).
`prices_as_of` (top-level) is the **oldest** `priced_at` among the
positions shown, i.e. the least-fresh number actually contributing to
this response, not the newest — `null` if every position is on the
`avg_cost` fallback (or there are no positions).

**Errors:**
- `404` — no competitor with that id.

---

## `POST /api/league/competitors/{id}/positions/{position_id}/price-targets/`

Revise `target_price` and/or `dead_price` on a position you already hold,
without submitting a trade — for "I'm moving my stop up" without actually
trading. Only the competitor's own linked human user may call this; an AI
competitor's equivalent is its own `set_price_targets` tool
(`league/tools.py`), not this endpoint.

**Request body:** either or both —
```json
{ "target_price": "32.00", "dead_price": "22.50" }
```
Pass `""` (empty string) for a field to clear it back to `null` rather
than change it. At least one of the two is required.

**Response `200`:** the updated position, same row shape as
`GET .../competitors/{id}/`'s `positions` entries above.

**Errors:**
- `400` — neither `target_price` nor `dead_price` provided, or a value
  that doesn't parse as a number.
- `403` — this isn't your own competitor.
- `404` — no competitor with that id, or no open position with that id
  **belonging to this competitor** (a position id existing at all isn't
  enough).

---

## `POST /api/league/competitors/{id}/refresh/`

Force a live re-quote of just this competitor's held tickers/contracts —
narrower than `POST /api/league/competitions/{id}/refresh/` above, which
refreshes every competitor in the competition. See that section for why
you'd normally never need either (a Beat job and every trade fill already
keep quotes warm).

**Response `202`:** `{ "status": "queued", "competitor_id": 6 }`.
Dispatched asynchronously; the refreshed row arrives over
`ws://<host>/ws/league/{competition_id}/` as `league.competitor_refreshed`:
```json
{
  "type": "league.competitor_refreshed",
  "competitor": { /* same row shape as GET .../competitors/{id}/ above, minus "positions" */ }
}
```
Broadcast to the whole competition's socket group, not just you — anyone
else watching the same competition's scoreboard sees this competitor's row
update too.

**Errors:**
- `404` — no competitor with that id.

---

## `GET /api/league/competitors/{id}/trades/`

A competitor's full trade history — filled **and** rejected, newest
first. A rejection is not an error response here; it's a normal row with
`"status": "rejected"` and a `rejection_reason` explaining why, since the
rules doc wants a full record of what was attempted, not just what
succeeded.

**Response `200`:**
```json
[
  {
    "id": 41, "side": "buy", "instrument_type": "stock", "ticker": "RKLB",
    "strike": null, "expiration": null, "quantity": 30,
    "limit_price": null, "fill_price": "24.1667", "thesis": "Backlog $2.2B vs $600M revenue, launch cadence accelerating.",
    "status": "filled", "rejection_reason": "", "rejection_code": "", "created_at": "2026-08-13T14:22:01Z"
  },
  {
    "id": 40, "side": "buy", "instrument_type": "stock", "ticker": "SPY",
    "strike": null, "expiration": null, "quantity": 20,
    "limit_price": null, "fill_price": null, "thesis": "Broad market hedge.",
    "status": "rejected", "rejection_reason": "$2000.00 exceeds the 30% index-fund cap ($1500.00 of $5000.00 account value).",
    "rejection_code": "index_cap", "created_at": "2026-08-13T14:20:55Z"
  }
]
```
`fill_price` is `null` on a rejected trade (nothing filled). `side` is
one of `buy` (open/add a long position), `sell` (close/reduce a long
position), `sell_to_open` (open a covered call/cash-covered put — the
*only* way a short leg opens), `buy_to_close` (close/reduce that short
leg).

`rejection_code` is a structured bucket alongside the free-text
`rejection_reason` — `""` on a filled trade, one of these on a rejected
one:
- `price_limit` — live price crossed your `limit_price`.
- `position_cap` — exceeds the 60% single-position cap.
- `index_cap` — exceeds the 30% index-fund cap.
- `insufficient_cash` — a buy exceeds available cash.
- `insufficient_holdings` — a close exceeds what's held, or would strand
  an open covered call without its collateral.
- `insufficient_collateral` — a covered call/cash-covered put can't be
  opened (not enough uncovered shares/cash).
- `market_unavailable` — no live quote/contract, or the market's closed.
- `invalid_request` — missing thesis, non-positive quantity, missing
  strike/expiration on an option, naked short attempted, or an invalid
  `sell_to_open` instrument type.
- `""` (blank) — either a filled trade, or a rejected trade created before
  this field existed (see `GET .../scoreboard/`'s `rejected_trades` above
  for how those are handled in aggregates).

**Errors:**
- `404` — no competitor with that id.

---

## `POST /api/league/competitors/{id}/trades/`

Submit a trade for **your own** human competitor. Goes straight through
`league.services.execution.submit_trade()` — the identical function an AI
competitor's `submit_trade` tool call invokes, so "confirmed by the same
process the AI uses" is true at the code level, not just in spirit.

**Request body (stock):**
```json
{
  "ticker": "RKLB", "side": "buy", "instrument_type": "stock",
  "quantity": 30, "thesis": "Backlog $2.2B vs $600M revenue."
}
```

**Request body (option, with a limit price guard):**
```json
{
  "ticker": "SPY", "side": "buy", "instrument_type": "call",
  "quantity": 1, "strike": "650", "expiration": "2026-09-19",
  "limit_price": "8.50", "thesis": "Cheap upside hedge into September CPI."
}
```
`ticker`/`side`/`instrument_type`/`quantity`/`thesis` are always required.
`strike`/`expiration` are required for `instrument_type: "call"`/`"put"`,
omit for `"stock"`. `limit_price` is optional on any trade — if the live
fill would be worse than it (above it for a buy, below it for a sell),
the trade is rejected instead of filled at a worse price; leave it out
for "fill at whatever the live price is." `target_price`/`dead_price`
are also optional, only meaningful on an opening side (`buy`/
`sell_to_open`) — recorded on the resulting position (see
`GET /api/league/competitors/{id}/`'s `target_price`/`dead_price` docs
above); revisable later without a new trade via
`POST .../positions/{id}/price-targets/`.

**Response `201`** if filled, **`400`** if rejected — **both** return the
same trade-row shape (see the `GET` above), so check `body.status`, not
just the HTTP status code, to render the outcome; the HTTP status is
there for generic error-handling middleware, the `status`/`rejection_reason`
fields are there for the actual UI message.

**Errors:**
- `400` — a required field is missing/unparseable, **or** the trade was
  legitimately rejected by the rules engine (limit price crossed, over a
  cap, insufficient cash/shares/collateral, no matching option contract,
  etc.) — `rejection_reason` in the body always has the specific reason.
- `403` — this competitor isn't yours (`kind != "human"` or its `user`
  isn't you). AI competitors can never be traded through this endpoint at
  all, even by their own owner — they only trade via their own wake-up
  loop.
- `404` — no competitor with that id.

---

## `POST /api/league/competitors/{id}/trades/{trade_id}/cancel/`

Cancel one of your own competitor's trades while it's still queued
(`"status": "pending"`) — the only way a trade sits in that state is
`Competition.restrict_after_hours_trading` being on and the market being
closed at submission time (see `POST .../trades/` above); everything else
resolves to filled/rejected synchronously, before this endpoint would ever
have anything to cancel. Same ownership rule as `POST .../trades/` — only
the competitor's own linked human user may call this; an AI competitor
cancels its own queued orders via its `cancel_trade` tool
(`league/tools.py`), never this endpoint.

**Response `200`** — the cancelled trade, same row shape as `GET
.../trades/` above, now with `"status": "cancelled"`.

**Response `409`** — the trade resolved (filled or rejected) in the
window between your cancel request and `league.tasks.
process_pending_trades` picking it up, so there was nothing left to
cancel. Carries the trade's **actual real outcome** in the same row shape
as the `200` case — not a bare error — so the frontend can render "this
already filled at $X" or "this was rejected: Y" directly from the
response body, no follow-up `GET` required:
```json
{
  "id": 102, "side": "buy", "instrument_type": "stock", "ticker": "RKLB",
  "strike": null, "expiration": null, "quantity": 10,
  "limit_price": null, "fill_price": "68.40", "thesis": "...",
  "status": "filled", "rejection_reason": "", "rejection_code": "", "created_at": "2026-08-13T21:05:00Z"
}
```

**Errors:**
- `403` — this competitor isn't yours.
- `404` — no competitor with that id, or no trade with that id belonging
  to this competitor.

---

## `GET /api/league/competitors/{id}/wakeups/`

An AI competitor's wake-up history — both the ones it scheduled for
itself (via its `schedule_wakeup` tool) and ones triggered manually
(`POST` below).

**Response `200`:**
```json
[
  {
    "id": 12, "run_at": "2026-08-13T13:45:00Z",
    "reason": "Check SPY price -- thinking about calls if it dips under 640.",
    "status": "completed", "created_at": "2026-08-13T09:02:11Z", "completed_at": "2026-08-13T13:45:04Z"
  },
  {
    "id": 11, "run_at": "2026-08-13T09:02:11Z", "reason": "Manual trigger from the competition operator.",
    "status": "completed", "created_at": "2026-08-13T09:02:11Z", "completed_at": "2026-08-13T09:02:18Z"
  }
]
```
`status` is `scheduled` (queued, hasn't fired yet), `completed`, or
`failed` (a transient Steady/Anthropic error — check server logs, or
look for a `league.wakeup_failed` notification). `run_at` can be in the
past relative to `created_at` for a manual trigger (fires immediately,
`run_at` is set to "now" at creation) or in the future for a
self-scheduled one (Celery holds it via `apply_async(eta=...)` until
then, not a polling loop).

**Errors:**
- `404` — no competitor with that id.

---

## `POST /api/league/competitors/{id}/wakeups/`

Wake an AI competitor up **right now** — e.g. kicking off its initial
research after you just created it, or telling it to look at something
specific on-demand instead of waiting for its next self-scheduled check.

**Request body (optional):**
```json
{ "reason": "You were just created for this competition -- do your initial research and decide how to start." }
```
Omit `reason` for a generic "Manual trigger from the competition
operator." default. This is the only context the agent gets beyond its
scratchpad (empty, for a brand-new competitor) and live portfolio — write
something useful here for a first trigger, since there's no prior
scratchpad content to fall back on yet.

**Response `202`:** the created wake-up row (same shape as the `GET`
above, `status: "scheduled"`), **not** the result. Dispatched
asynchronously — same pattern as `POST /api/companies/{ticker}/screen/`
(`docs/api-endpoints.md`): this response just confirms it's queued, not
that the agent is running yet (Celery could be backed up). A plain
`league.wakeup_started` push (`docs/websockets.md`, `/ws/user/`, not a
persisted `notification.new`) follows once it actually starts — the
button-disabling signal to key "now thinking" off of, rather than the
bare `202`. The actual outcome (trades placed, scratchpad updated, or
nothing) arrives as a `league.trade_activity` / `league.wakeup_complete` /
`league.wakeup_failed` notification (see below), and is visible
afterward via this same endpoint (`status` flips to `completed`/`failed`)
and `GET .../turns/` (a new `AgentTurn` appears) — that's the signal to
re-enable the button.

**Errors:**
- `400` — this competitor's `kind` isn't `"ai"` (a human competitor has
  no wake-up loop to trigger — they just submit trades directly).
- `404` — no competitor with that id.

---

## `GET /api/league/competitors/{id}/wakeups/next/`

What's coming up next for this AI competitor — two different kinds of
"scheduled", surfaced separately since only one of them has a database
row to list. The guaranteed daily floor wake-ups (this competitor's
competition's `ForcedCheckin` rows — admin-only, no API for them, see
"Guaranteed floor wake-ups are now customizable" below) never get an
`AgentWakeup` row until the instant they actually fire, so there's
nothing to query for them; their next occurrence is computed live
instead. Self-scheduled ones (the agent's own `schedule_wakeup` tool
calls) are real rows — this is the same data as `GET .../wakeups/`
above, just pre-filtered to still-pending (`status: "scheduled"`,
`run_at` in the future) and sorted soonest-first, rather than full
history.

**Response `200`:**
```json
{
  "forced_checkins": [
    { "label": "8am news scan", "next_run_at": "2026-08-14T08:00:00-04:00" }
  ],
  "self_scheduled": [
    {
      "id": 12, "run_at": "2026-08-17T14:30:00-04:00",
      "reason": "Check NVDA price action/news ahead of its 8/26 earnings.",
      "status": "scheduled", "created_at": "2026-08-12T15:24:15Z", "completed_at": null
    }
  ]
}
```
`forced_checkins` has one entry per `ForcedCheckin` row configured for
this competitor's competition — **not always 3, and not the same across
competitions** (see below); could be empty if none are configured for
this competition. Sorted by `next_run_at` — with more than one row, the
*order* isn't necessarily the order the rows were configured in;
whichever slot's next occurrence is soonest from right now comes first.
Weekends are skipped; market holidays are not (same known gap as the
wake-up prompt's own time-context block — verify manually if a holiday
might fall in between). `self_scheduled` is empty if the agent hasn't
called `schedule_wakeup` recently, or currently has nothing pending.
**Both lists are empty for a human competitor** — neither concept
applies to them, and this endpoint returns `200` with empty arrays
rather than a `400`.

**Guaranteed floor wake-ups are now customizable (per competition).**
As of 2026-08-13, the floor is no longer a fixed 3-slots-for-everyone
list (8am news / 9:35am open / 3:15pm pre-close) — it's `ForcedCheckin`
rows on `Competition` (`hour`, `minute`, `label`, `prompt`,
`inject_news`, `news_slot`), editable as an inline on the Competition
admin page (`/admin/league/competition/{id}/change/`), same "rules are
data on the row, not code constants" pattern `max_position_pct`/
`cash_apy`/`index_tickers` already use. **New competitions get one
default row seeded automatically** (`POST /api/league/competitions/` and
`seed_league` both call `create_default_forced_checkins`): an 8am news
scan, `inject_news: true`, `news_slot: "morning"`. Market open and
pre-close are no longer free — if you want that coverage back for a
given competition, either add `ForcedCheckin` rows for it in the admin,
or leave it to the agent's own `schedule_wakeup` calls (which now cost
against `LEAGUE_MAX_WAKEUPS_PER_DAY` same as anything else it schedules,
unlike a `ForcedCheckin` row). This was a deliberate cost change: every
operator-mandated slot is a guaranteed-to-run, always-billed LLM turn
for every AI competitor in that competition, every trading day — fewer
slots per competition (or per competitor priority) is a direct cost
lever, and it's now something you can actually tune without touching
code. There's no REST endpoint for reading/writing `ForcedCheckin` rows
— this is operator/cost-tuning data, not something the frontend or the
agent itself needs to see beyond what `GET .../wakeups/next/` already
surfaces (labels + next run times).

**`inject_news` shares one generated brief across every competitor and
competition on that slot, not one fetch each.** As of 2026-08-19, a
`ForcedCheckin` with `inject_news: true` no longer has each competitor
independently fetch headlines/quotes — the first wake-up in a given Beat
tick to need a `(date, news_slot)` combination generates it once
(`DailyBrief`, headlines + a broad-market ETF snapshot) and every other
competitor/competition sharing that `news_slot` (e.g. every competition's
default `"morning"` row) reads the same generated text. A blank
`news_slot` falls back to that row's own `hour:minute` as the cache key
— still shared across competitions whose forced check-in happens to land
on the exact same time, just not across differently-timed ones. See
`GET /api/league/briefs/` above to read what a competitor actually
received.

**Errors:**
- `404` — no competitor with that id.

---

## `GET /api/league/competitors/{id}/turns/`

An AI competitor's reasoning history — one row per completed wake-up,
lightweight (no transcript, which can be large). Fetch
`GET .../turns/{turn_id}/` for one turn's full play-by-play.

**Response `200`:**
```json
[
  {
    "id": 9, "trigger": "manual", "wakeup_id": 11,
    "tool_calls": ["get_stock_quote", "get_options_chain", "update_scratchpad"],
    "input_tokens": 31595, "output_tokens": 1321, "cost_usd": "0.191000",
    "created_at": "2026-08-13T09:02:18Z"
  }
]
```
`trigger` is `"scheduled_wakeup"` (the agent's own `schedule_wakeup` call
fired), `"manual"` (fired via `POST .../wakeups/`), or `"forced_checkin"`
(one of the competition's guaranteed daily floor wake-ups, see
`GET .../wakeups/next/` above) — purely informational, doesn't change
what the agent could do either way. `tool_calls` lists
every tool invoked that turn in order, including `"web_search"`
(Anthropic, capped at 3 uses/turn) or `"google_search"` (Gemini grounding
— same idea, different name, unlimited-but-model-gated like OpenAI's
Responses API search) when it searched the web — real tool names, not a
summary, and identical in shape regardless of which provider this
competitor runs on. `cost_usd` and the token counts are the real totals
the provider billed for that turn.

**Errors:**
- `404` — no competitor with that id.

---

## `GET /api/league/competitors/{id}/turns/{turn_id}/`

One turn's full transcript — the actual "peek into AI logic" view.

**Response `200`:** same shape as one row of the list above, plus:
```json
{
  "...": "...(id/trigger/wakeup_id/tool_calls/tokens/cost as above)...",
  "transcript": [
    { "role": "user", "text": "Manual check-in -- the competition operator woke you up directly (not one you scheduled). Current time: 2026-08-13T09:02:11+00:00.\nReason: You were just created for this competition -- do your initial research and decide how to start.\n\nYour notes from last time:\n(nothing saved yet)\n\nCurrent portfolio:\nCash: $5000\nAccount value: $5000\nNo open positions.\n\nDecide what to do: ..." },
    { "role": "assistant", "content": [
      { "type": "text", "text": "I'll check a few candidates before deciding anything." },
      { "type": "tool_use", "name": "get_stock_quote", "input": { "ticker": "RKLB" } }
    ]},
    { "role": "assistant", "content": [
      { "type": "text", "text": "..." },
      { "type": "server_tool_use", "name": "web_search", "input": { "query": "Rocket Lab Neutron launch schedule 2026" } },
      { "type": "web_search_tool_result", "tool_use_id": "...", "content": [ { "type": "web_search_result", "title": "...", "url": "...", "encrypted_content": "..." } ] }
    ]}
  ]
}
```
`transcript` is a list of turns; each `"role": "assistant"` entry's
`content` is a list of raw content blocks **exactly as the provider
returned them** (`block.model_dump()`/`item.model_dump()`/`part.model_dump()`,
not hand-picked fields) — the example above is Anthropic's shape. **Every
provider's transcript looks structurally different, not just
differently-labeled** — there's no normalization layer between them,
since this field is meant as a raw audit log, not a rendering contract.

For an OpenAI turn, each entry's `type` is one of Responses API's own
item types instead of Anthropic's block types:
```json
{ "role": "assistant", "type": "message", "content": [{ "type": "output_text", "text": "..." }] },
{ "role": "assistant", "type": "function_call", "name": "get_stock_quote", "arguments": "{\"ticker\": \"RKLB\"}", "call_id": "call_..." },
{ "type": "function_call_output", "call_id": "call_...", "output": "..." },
{ "role": "assistant", "type": "web_search_call", "action": { "type": "search", "query": "..." } }
```

For a Gemini turn, entries are Gemini's `Part`/`FunctionResponse` shapes,
plus a synthetic `"type": "grounding_metadata"` entry when the model
searched — that data lives on the response candidate, not inside a
content part, so it's flattened into the transcript explicitly rather
than silently missing:
```json
{ "role": "assistant", "function_call": { "name": "get_stock_quote", "args": { "ticker": "RKLB" } } },
{ "role": "tool", "function_response": { "name": "get_stock_quote", "response": { "result": "..." } } },
{ "role": "assistant", "text": "Per search, RKLB is..." },
{ "role": "assistant", "type": "grounding_metadata", "web_search_queries": ["RKLB Neutron launch schedule 2026"] }
```

If you're building a "watch the AI think" view, **branch on the
competitor's `assistant_model` provider (via `GET /api/league/models/`)
before parsing a turn's `transcript`**, not on sniffing `type`/field
names — none of the three providers' shapes overlap. All three
providers' *first* entry uses the same `{"role": "user", "text": ...}`
shape though, so the opening user-turn render can stay provider-agnostic.
`tool_calls` (the list-of-strings field from the list endpoint above) is
identical across all three providers if you just need "which tools ran,"
without touching `transcript`'s internal shape at all.

**Errors:**
- `404` — no competitor with that id, or no turn with that id **for this
  specific competitor** (a turn's id existing at all isn't enough — it
  must belong to the competitor in the URL).

---

## `GET /api/league/competitors/{id}/scratchpad/`

An AI competitor's current working notes — the durable state its next
wake-up will be built from. Not history (see `.../turns/` for that) —
just what it thinks *right now*.

**Response `200`:**
```json
{
  "content": "Account: $5,000 cash, no positions. Watching RKLB (backlog thesis) and SPY (broad hedge candidate). Next check: see if SPY dips under 640 before adding a call.",
  "updated_at": "2026-08-13T09:02:18Z"
}
```
`content` is `""` and `updated_at` is `null` for a competitor that
hasn't had a wake-up yet (nothing written).

**Errors:**
- `404` — no competitor with that id.

---

## `GET /api/league/competitors/{id}/ticker-memories/`

Every ticker an AI competitor has saved research notes for (via its
`set_ticker_memories` tool) — the per-ticker counterpart to `.../scratchpad/`
above. Unlike the scratchpad (one blob, always present, read in full every
wake-up), this is a list that only has entries for tickers the agent has
actually researched, and is never auto-injected into a wake-up whole — the
agent pulls specific tickers in on demand via `get_ticker_memories`.

**Response `200`:**
```json
[
  {
    "ticker": "RKLB",
    "content": "Backlog thesis: book-to-bill > 1 for three straight quarters. Neutron first launch slipping into next year -- watching for a firm date before adding.",
    "updated_at": "2026-08-13T09:02:18Z"
  },
  {
    "ticker": "SPY",
    "content": "Broad hedge candidate, no position yet. Re-evaluate if VIX prints above 25.",
    "updated_at": "2026-08-12T14:30:05Z"
  }
]
```
`[]` for a competitor with no saved ticker research yet. Sorted most
recently updated first.

**Errors:**
- `404` — no competitor with that id.

---

## Wake-up events (`league.*`)

Pushed the same way as every other event family in this app — see
`docs/websockets.md` for the connection protocol itself
(`ws://<host>/ws/user/?token=<access_token>`). All four fire from
`league/tasks.py::run_agent_wakeup`, whatever triggered it (self-scheduled,
a `ForcedCheckin`, or manual via `POST .../wakeups/` above) — one
`league.wakeup_started` when it actually starts running, then exactly one
of the three terminal events per wake-up when it finishes.

```json
{ "type": "league.wakeup_started", "competitor_id": 6, "wakeup_id": 12 }
```
A plain push (`send_to_user`), **not** wrapped in `notification.new` and
not persisted — sent once, right as the wake-up starts actually running
(past its not-scheduled/paused/over-budget checks), so a frontend can flip
a wake-up button from "queued" (the bare `202` from `POST .../wakeups/`)
to "thinking" without waiting on the terminal event below. Missing this
push (a dropped connection, e.g.) costs nothing — the button just stays
disabled until the terminal event arrives, or the frontend re-fetches
`GET .../wakeups/` and sees the row is no longer `"scheduled"`.

The three below arrive as `notification.new` pushes (reconnect
redelivery, `docs/websockets.md`), unlike the plain push above.

```json
{ "type": "notification.new", "id": 44, "notification_type": "league.trade_activity",
  "title": "Claude Regular traded", "body": "buy 30 RKLB (filled); sell_to_open 1 AAPL (rejected)",
  "payload": { "competitor_id": 6, "wakeup_id": 12 }, "created_at": "2026-08-13T13:45:04Z" }
```
At least one `Trade` row (filled or rejected) was created this wake-up.
`body` is a semicolon-joined one-liner per trade — fetch
`GET .../trades/` for the full rows if you need `thesis`/`rejection_reason`.

```json
{ "type": "notification.new", "id": 45, "notification_type": "league.wakeup_complete",
  "title": "Claude Lunatic finished thinking", "body": "No trades this time.",
  "payload": { "competitor_id": 7, "wakeup_id": 13 }, "created_at": "2026-08-13T13:46:02Z" }
```
The wake-up ran successfully but placed no trades — still worth knowing
about (e.g. after a manual trigger, so the UI can stop showing "thinking...").

```json
{ "type": "notification.new", "id": 46, "notification_type": "league.wakeup_failed",
  "title": "Claude Regular's wake-up failed", "body": "Check server logs for details.",
  "payload": { "competitor_id": 6, "wakeup_id": 14 }, "created_at": "2026-08-13T13:47:30Z" }
```
An unhandled exception during the wake-up (transient Steady/Anthropic
error, etc.) — logged server-side with the full traceback
(`logger.exception` in `run_agent_wakeup`); this push is just the
user-facing "something broke" signal, no detail beyond that in the
payload itself.

All three only fire if the competitor has a `user` attached — every
competitor created via `POST .../competitors/` always does (attached to
whoever created it), so in practice this always fires for anything built
through this API.

---

## Live competition feed (`ws://<host>/ws/league/{competition_id}/`)

A **second, separate** socket from the notification events above — don't
confuse the two:

|                        | Notification events (above)              | Live competition feed (this section) |
|------------------------|-------------------------------------------|---------------------------------------|
| Connect to             | `ws://<host>/ws/user/?token=<access_token>` (`docs/websockets.md`) | `ws://<host>/ws/league/{competition_id}/?token=<access_token>` |
| Scope                  | Just you — only events for competitors *you* created | Everyone watching that competition — any authenticated user, any competitor's activity |
| Wrapper                | `{ "type": "notification.new", "notification_type": "league.*", ... }` | Unwrapped — `type` is the event name directly (`league.trade`, etc.) |
| Missed-while-offline   | Redelivered on reconnect (durable) | **Not** redelivered — live-only. `GET .../trades/`, `.../scoreboard/`, `.../competitors/` are the source of truth for anything missed |
| When to open it        | Once per session (any league activity) | Once per competition page you have open (e.g. the scoreboard view) |

Same auth mechanics as the user socket otherwise (`docs/websockets.md`'s
"Connecting" section: token as a query param, `4001` close code on a
bad/missing/expired token, no separate auth-ack message, reconnect with a
fresh token on drop).

```json
{
  "type": "league.trade",
  "id": 88, "competitor_id": 6, "competitor_name": "Claude Regular", "competitor_kind": "ai",
  "competition_id": 3, "side": "buy", "instrument_type": "stock", "ticker": "RKLB",
  "strike": null, "expiration": null, "quantity": 30,
  "limit_price": null, "fill_price": "24.50", "thesis": "Neutron launch window narrowing, backlog thesis intact.",
  "status": "filled", "rejection_reason": "", "rejection_code": "", "created_at": "2026-08-13T14:22:01Z"
}
```
Fired the instant `execution.submit_trade()` creates a `Trade` row —
filled **or** rejected, human **or** AI, from anywhere (the trade API,
an AI wake-up). This is what a live activity feed / ticker tape renders
off of; `GET /api/league/competitions/{id}/trades/` is the same data as a
one-shot fetch (initial load, or catching up on anything missed while
disconnected — this event is not redelivered). Same field shapes as that
endpoint's rows, plus `competitor_id`/`competitor_name`/`competitor_kind`/
`competition_id` since a competition-wide feed doesn't have those implied
by the URL the way a single-competitor endpoint does. Best-effort: a
Redis hiccup drops this one push (the trade itself is never affected —
it's already committed by the time this fires), so don't treat "saw every
trade over the socket" as guaranteed; reconcile against `GET .../trades/`
if you need certainty.

`league.competitor_refreshed` and `league.scoreboard_refreshed` (the
results of `POST .../competitors/{id}/refresh/` and
`POST .../competitions/{id}/refresh/` above) also arrive here — see those
two sections for their exact shapes. All three event types share this one
socket, so a page watching a competition only needs one connection open
to react to trades, AI activity, and refreshes alike.

**Building the manual-refresh button:** `POST` the relevant `refresh/`
endpoint, show a pending/spinner state keyed off that request having been
queued (the `202` itself, not a timeout), and update the UI when
`league.competitor_refreshed` / `league.scoreboard_refreshed` arrives on
this socket — there's no need to poll or re-`GET` afterward, the pushed
payload already has the fresh numbers. If the socket isn't connected (or
drops mid-refresh), fall back to re-`GET`ting `.../scoreboard/` or
`.../competitors/{id}/` after a few seconds; nothing else signals
completion. Since the refresh itself is asynchronous with no separate
"done" beyond the broadcast, a reasonable UX is to clear the spinner after
either the matching event arrives or a timeout (~10-15s covers even a
large competition's worth of parallel Steady calls) elapses, whichever
comes first.
