# API Endpoints

Reference for every HTTP endpoint this backend exposes. `docs/authentication.md`
covers *how* to use the auth endpoints (token lifecycle, Google sign-in flow,
storage, refresh strategy); this doc is the flat reference — method, path,
auth requirement, request/response shapes. Between the two, this is
everything the frontend needs.

`/api/auth/`, `/api/companies/` (including the async screen + price
history endpoints, and clips/journal), all exist today.

`/api/league/` (the paper trading league) is documented separately in
`docs/league-api.md` instead of inline here — it has real domain concepts
(the rules engine, the AI wake-up loop, scratchpad vs. turn history) a
frontend needs explained alongside the endpoint shapes, not just a flat
method/path/body reference.

## Conventions

- Base URL: wherever Django is running, e.g. `http://localhost:8000` in dev.
- All request and response bodies are JSON. Send `Content-Type: application/json`.
- Authenticated endpoints require `Authorization: Bearer <access_token>`.
- Validation errors are `400` with field-name keys mapping to arrays of
  message strings, e.g. `{"email": ["user with this email already exists."]}`.
- Auth failures (missing/invalid/expired token, bad credentials) are `401`
  with a `detail` string, sometimes a `code`.
- User IDs are UUIDs (strings), not integers.

---

## `POST /api/auth/register/`

Email + password sign-up. No email verification required — the account is
usable immediately. **Invite-only**: the email must have a matching
`AllowedEmail` row (`accounts.models.AllowedEmail`) or this is rejected —
see "Sign-up allowlist" below.

**Auth:** none

**Request body:**
```json
{ "email": "alex@example.com", "name": "Alex Chen", "password": "a-strong-password" }
```
`name` is optional. `password` is validated by Django's standard password
validators (minimum length, not too common, not all-numeric, not too similar
to the email/name) — a weak password returns `400` with a `password` field
listing the specific reasons.

**Response `201`:**
```json
{
  "access": "<jwt>",
  "refresh": "<jwt>",
  "user": {
    "id": "e6764d05-0178-4d5f-8942-6194cf4fc358",
    "email": "alex@example.com",
    "name": "Alex Chen",
    "initials": "AC",
    "date_joined": "2026-07-20T00:16:27.247994Z"
  }
}
```

**Errors:** `400` — email already registered, email not on the sign-up
allowlist (`{"email": ["This email isn't on the sign-up allowlist."]}`), or
password fails validation.

---

## `POST /api/auth/login/`

Email + password login.

**Auth:** none

**Request body:**
```json
{ "email": "alex@example.com", "password": "a-strong-password" }
```

**Response `200`:** same shape as register — `{access, refresh, user}`.

**Errors:** `401` — `{"detail": "No active account found with the given credentials"}`.
Same message for "no such user" and "wrong password" — don't use this to
distinguish the two in the UI.

---

## `POST /api/auth/google/`

Google sign-in. The frontend obtains an ID token from Google (see
`authentication.md`) and hands it to this endpoint, which verifies it with
Google and finds-or-creates the matching user.

**Auth:** none

**Request body:**
```json
{ "id_token": "<google-issued id token>" }
```

**Response `200`:** same shape as register/login — `{access, refresh, user}`.
Note this is always `200`, even the first time a given Google account signs
in (account creation is silent — there's no separate "register via Google"
step).

**Errors:**
- `400` — `id_token` missing from the body.
- `401` — `{"detail": "Invalid Google token: ..."}` if Google rejects it (expired, wrong audience, tampered).
- `401` — `{"detail": "Google sign-in is not configured on this server."}` if the backend has no `GOOGLE_OAUTH_CLIENT_ID` set. Only happens in a misconfigured deployment.
- `401` — `{"detail": "This email isn't authorized to sign up."}` — same
  sign-up allowlist as `register/` above, but **only checked for a
  brand-new email** with no existing account. Logging in again, or linking
  Google to an already-existing email/password account, always succeeds
  regardless of the allowlist — removing an `AllowedEmail` row never locks
  out someone who already has an account.

A Google login with an email that already has an email/password account
links to that same account (matched by email) rather than creating a
duplicate — the user ends up with both email/password login still working as
before, plus Google sign-in now linked for that account too. See
`authentication.md` for details.

---

## `POST /api/auth/token/refresh/`

Exchange a refresh token for a new access token. **Refresh tokens rotate**:
this call blacklists the refresh token you sent and issues a new one — you
must store the new `refresh` from the response, not reuse the old one.

**Auth:** none (the refresh token itself is the credential)

**Request body:**
```json
{ "refresh": "<jwt>" }
```

**Response `200`:**
```json
{ "access": "<new jwt>", "refresh": "<new jwt>" }
```

**Errors:** `401` — expired, malformed, or already-used (blacklisted) refresh
token, e.g. `{"detail": "Token is blacklisted", "code": "token_not_valid"}`.
Treat any `401` here as "session over" — send the user to login.

---

## `POST /api/auth/logout/`

Blacklists a refresh token so it can never be used again (including for
refresh, even if not yet expired).

**Auth:** required (`Authorization: Bearer <access_token>`)

**Request body:**
```json
{ "refresh": "<jwt>" }
```

**Response:** `205`, empty body.

**Errors:**
- `401` — no/invalid access token in the `Authorization` header.
- `401` — `{"detail": "Invalid refresh token: ..."}` if the refresh token in the body is already invalid/blacklisted/expired.

This only invalidates the *refresh* token. The access token you already have
keeps working until it naturally expires (30 min) — there's no server-side
access-token revocation. See `authentication.md` for what this means in
practice.

---

## `GET /api/auth/me/`

Current user's profile.

**Auth:** required

**Response `200`:**
```json
{
  "id": "e6764d05-0178-4d5f-8942-6194cf4fc358",
  "email": "alex@example.com",
  "name": "Alex Chen",
  "initials": "AC",
  "date_joined": "2026-07-20T00:16:27.247994Z"
}
```

**Errors:** `401` — no/invalid/expired access token.

---

## `GET /api/companies/`

The dashboard: every company the logged-in user tracks. On first call ever
for a user, auto-provisions a row for each system-default company (see
below) — after that it's just a read.

**Auth:** required

**Response `200`** (real example, a fresh user's first load):
```json
[
  { "ticker": "ASTS", "name": "AST SpaceMobile, Inc.", "screened": true, "price": 57.8, "market_cap": 23518360606,
    "revenue_growth_pct": null, "fcf": null, "runway_years": null, "runway_status": "unknown", "fcf_approximated": false, "tripwire_triggered": true,
    "updated_at": "2026-07-20T02:55:10Z", "status": "pending", "origin": "system", "added_at": "2026-07-20T03:20:00Z" },
  { "ticker": "RDW", "name": "Redwire Corp", "screened": true, "price": 8.45, "market_cap": 2018071250,
    "revenue_growth_pct": 0.1029, "fcf": -12345000, "runway_years": 1.8, "runway_status": "computed", "fcf_approximated": false, "tripwire_triggered": false,
    "updated_at": "2026-07-20T02:58:41Z", "status": "pending", "origin": "system", "added_at": "2026-07-20T03:20:00Z" },
  { "ticker": "SLS", "name": "SELLAS Life Sciences Group, Inc.", "screened": true, "price": 3.1, "market_cap": 412000000,
    "revenue_growth_pct": null, "fcf": null, "runway_years": 2.53, "runway_status": "computed", "fcf_approximated": true, "tripwire_triggered": false,
    "updated_at": "2026-07-21T02:08:57Z", "status": "pending", "origin": "user", "added_at": "2026-07-21T02:00:00Z" },
  { "ticker": "MSFT", "name": "MICROSOFT CORP", "screened": true, "price": 381.7, "market_cap": 2835430000000,
    "revenue_growth_pct": 0.149, "fcf": 71611000000, "runway_years": null, "runway_status": "profitable", "fcf_approximated": false, "tripwire_triggered": false,
    "updated_at": "2026-07-24T21:58:52Z", "status": "pending", "origin": "user", "added_at": "2026-07-24T21:50:00Z" },
  { "ticker": "NVDA", "name": "NVIDIA CORP", "screened": false, "price": null, "market_cap": null,
    "revenue_growth_pct": null, "fcf": null, "runway_years": null, "runway_status": "unknown", "fcf_approximated": false, "tripwire_triggered": false,
    "updated_at": null, "status": "pending", "origin": "user", "added_at": "2026-07-20T04:02:00Z" }
]
```

Notes:
- **Check `screened` before rendering financial fields, not their
  emptiness.** `false` means this company's been added/resolved (the ticker
  is valid, the row exists) but never actually run through
  `POST .../screen/` — every financial field is `null`/empty because there's
  nothing to show yet, not because of an error. Prompt "Run screen" for
  these rather than rendering a blank-looking row. This was a real gap:
  before this field existed, an unscreened company was indistinguishable
  from one with a data problem — both just showed nulls.
- `runway_status` — one of `"computed"` (`runway_years` is a real number),
  `"profitable"` (latest-year FCF is `>= 0` — runway isn't a meaningful
  question for a company not burning cash, see MSFT above), or `"unknown"`
  (burning cash but missing the FCF or cash-on-hand data needed to compute
  it, see ASTS above). Render all three differently — don't collapse
  `"profitable"` and `"unknown"` into the same blank dash; they mean
  opposite things. `runway_years` is only non-null when `runway_status`
  is `"computed"`.
- `fcf_approximated` — `true` when `runway_years` was computed from OCF
  alone because the filer hasn't tagged capex for the latest fiscal year
  (common for pre-revenue names once capex drops below tagging
  materiality — SLS/SELLAS above is a real example, capex untagged since
  FY2016). `fcf` itself is `null` in that case (a truer statement than
  pretending capex is zero) — only the runway calc falls back, and only
  for that one number. Surface this in the UI as a caveat on the runway
  figure, not as a reason to hide it. Always `false` when `runway_status`
  isn't `"computed"`.
- `origin` is `"system"` (suggested default, matches the mockup's "SYS" tag)
  or `"user"` (`"MINE"`) — display-only. `status` is independently owned by
  this user regardless of origin; changing it never affects other users'
  copies of the same company.
- Fields here are the *latest fiscal year's* values only (a summary row, not
  the full history).
- `updated_at` is when this company's data was last fetched from EDGAR/FMP
  (across any user) — not per-user, not per-tracking-row. `null` when
  `screened` is `false`.

---

## `POST /api/companies/`

Add a ticker to the user's dashboard. Does **not** trigger a screen —
that's `POST /api/companies/{ticker}/screen/`, a separate action (matches
"Add" and "Run screen" being distinct buttons). A freshly-added company
comes back with `"screened": false` and every financial field `null` —
that's the expected state until `.../screen/` runs, not an error.

**Auth:** required

**Request body:**
```json
{ "ticker": "RKLB" }
```

**Response `201`:** one dashboard row (same shape as the `GET` list above),
with `"origin": "user"`. Idempotent — POSTing a ticker already tracked
returns its existing row (still `201`) rather than erroring or duplicating.

**Errors:**
- `400` — `ticker` missing, or not found in SEC's company directory.

---

## `PATCH /api/companies/{ticker}/status/`

Change the logged-in user's status for a ticker they're already tracking.

**Auth:** required

**Request body:**
```json
{ "status": "shortlisted" }
```
One of `pending`, `shortlisted`, `killed`.

**Response `200`:** the updated dashboard row.

**Errors:**
- `400` — `status` isn't one of the three valid values.
- `404` — this user isn't tracking that ticker yet (add it first via `POST /api/companies/`).

---

## `POST /api/companies/{ticker}/screen/`

Runs Mode 1 (Screen) for a ticker: fetches fresh SEC EDGAR + FMP data
(reusing anything already cached under a day old), computes the derived
report. Creates the `Company` on first sight — no separate "add a company"
step. The ticker is validated synchronously (so a bad one `400`s
immediately on this response), but the actual fetching happens in a Celery
task — the result arrives over the user's WebSocket group instead of in
this response (`docs/websockets.md`), so the frontend isn't blocked on
SEC's rate-limit throttling for however many seconds a screen takes.

**Auth:** required

**Request body (optional):**
```json
{ "max_age_days": 1.0 }
```
Omit for the default (reuse cached data younger than 1 day). `0` forces a
full refresh.

**Response `202`:**
```json
{ "status": "queued", "ticker": "RKLB" }
```
Not the report — that comes over the WebSocket as `screen.complete`
(`docs/websockets.md`), or via a follow-up `GET /api/companies/{ticker}/`
once that message arrives.

**Errors:**
- `400` — ticker not found in SEC's company directory, e.g.
  `["Ticker 'ZZZZ' not found in SEC's company_tickers.json"]`. Raised before
  the task is even queued, so this is still a synchronous response.

---

## `GET /api/companies/{ticker}/`

The latest cached report for a ticker, without triggering a fetch. Fast —
just reads what's already in the database and computes the derived fields
from it.

**Auth:** required

**Response `200`** (real example, RKLB — trimmed to one field per section
for brevity; every EDGAR-sourced field in `financials_by_fy` is keyed the
same way, one entry per fiscal year it's reported for):
```json
{
  "ticker": "RKLB",
  "cik": 1819994,
  "name": "Rocket Lab Corp",
  "screened": true,
  "fiscal_years": [2020, 2021, 2022, 2023, 2024, 2025],
  "financials_by_fy": {
    "revenue": { "2023": 244592000, "2024": 436214000, "2025": 601799000 },
    "gross_profit": { "2023": 51409000, "2024": 116149000, "2025": 207181000 },
    "net_loss": { "2023": -182571000, "2024": -190175000, "2025": -198209000 },
    "operating_cash_flow": { "2023": -98867000, "2024": -48890000, "2025": -165521000 },
    "capex": { "2023": 54707000, "2024": 67093000, "2025": 156285000 },
    "stock_based_comp": { "2023": 53461000, "2024": 56816000, "2025": 71099000 },
    "cash": { "2023": 162518000, "2024": 271042000, "2025": 828660000 },
    "total_debt": { "2023": 105351000, "2024": 56094000, "2025": 1716000 }
  },
  "ai_mapped_concepts": [],
  "derived": {
    "gross_margin_pct_by_fy": { "2025": 0.3442694321525958 },
    "fcf_by_fy": { "2025": -321806000 },
    "revenue_growth_pct_by_fy": { "2025": 0.3795957947246076 },
    "runway_years": 2.575029676264582,
    "runway_status": "computed",
    "fcf_approximated": false,
    "ps_trailing": 65.04334281047326
  },
  "shares": {
    "cover_page": { "value": 578750990, "as_of": "2026-04-30", "form": "10-Q" },
    "balance_sheet_by_fy": { "2025": 543574552 }
  },
  "price": 67.62,
  "market_cap": 39143018660,
  "price_queried_at": "2026-07-20T02:42:08.995487+00:00",
  "shares_float_fmp": {
    "shares_outstanding": 578867475,
    "fmp_date": "2026-07-18 13:07:11",
    "source": "https://www.sec.gov/Archives/edgar/data/1819994/000181999426000028/rklb-20260331.htm"
  },
  "filings": [
    { "form": "10-Q", "filing_date": "2026-05-07", "report_date": "2026-03-31",
      "accession_number": "0001819994-26-000028",
      "url": "https://www.sec.gov/Archives/edgar/data/1819994/000181999426000028/rklb-20260331.htm",
      "description": "10-Q" },
    { "form": "10-K", "filing_date": "2026-02-26", "report_date": "2025-12-31",
      "accession_number": "0001819994-26-000013",
      "url": "https://www.sec.gov/Archives/edgar/data/1819994/000181999426000013/rklb-20251231.htm",
      "description": "10-K" }
  ],
  "tripwire": { "drift": 1.2773669919099694e-11, "triggered": false }
}
```

Notes on reading this:
- **Check `screened` first.** `false` means this `Company` row exists (the
  ticker's been resolved — added by a user, or looked up before) but never
  actually run through a screen. Every other field is then an empty shell:
  `fiscal_years: []`, every entry in `financials_by_fy` `{}`, `price` and
  `market_cap` `null`. That's the expected unscreened state, not an error or
  a data problem — prompt `POST .../screen/` rather than rendering it as if
  something's wrong.
- `financials_by_fy` fields can have gaps or different year ranges per field
  — not every tag is reported for every year (e.g. a company might have 6
  years of revenue but only 3 of gross profit). Don't assume every field
  covers all of `fiscal_years`.
- `ai_mapped_concepts` — which `financials_by_fy`/`shares.balance_sheet_by_fy`
  field names (if any) came from an LLM-resolved XBRL tag rather than this
  app's own fixed list, for filers this app's standard `us-gaap` tags find
  nothing for at all (foreign private issuers reporting under a different
  taxonomy, e.g. IFRS — confirmed live for NU, TSM, RIO). Almost always `[]`
  — surfaced explicitly rather than left indistinguishable from a directly
  tagged figure, since these values were never vetted by a human the way the
  fixed tag list's names were. A filer whose reporting currency isn't USD at
  all (e.g. Unilever, EUR) gets no fallback data rather than a
  foreign-currency figure silently shown as if it were dollars — worth
  knowing if a European filer's report looks unexpectedly empty.
- `shares.cover_page` is the single most-recent value across *all* filing
  types (10-K or 10-Q, whichever is newer) — not fiscal-year-keyed like
  everything else, since it's a point-in-time figure, not an annual one.
- `tripwire.drift` near zero (as above) means FMP's own numbers are
  internally consistent — it does **not** mean FMP agrees with EDGAR's share
  count. See the comment on `check_sanity()` in
  `companies/services/fmp.py` for a real case (RDW) where they disagreed by
  ~24% while this still read as a clean pass.
- Missing `price`/`market_cap`/`shares_float_fmp` (all `null`) means
  `FMP_API_KEY` isn't set on the backend — everything else still works.
- `filings` — every cached 10-K/10-Q/20-F, newest first, each with a direct
  link (`url`) to the primary document on EDGAR. Meant for the frontend to
  let a user pull up the actual filing (e.g. to prefill a
  `ResearchClip`'s `source` field), not for identifying which fiscal years
  are covered above — use `fiscal_years` for that. Empty when `screened`
  is `false`. Sourced from EDGAR's "recent" filings window (up to ~1,000
  filings) — older filings for long-tenured filers wouldn't appear here,
  though that hasn't come up yet for this app's small/young-cap universe.

**Errors:**
- `404` — this ticker has never been resolved by *anyone* (never added via
  `POST /api/companies/`, never screened), so no `Company` row exists at
  all, e.g. `{"detail": "ZZZZ hasn't been screened yet -- POST to /api/companies/ZZZZ/screen/ first."}`.
  This is different from `"screened": false` in a `200` response — that's a
  `Company` that exists (added, but not yet screened); this `404` is a
  ticker nobody's touched yet.

---

## `GET /api/companies/{ticker}/price-history/`

Full daily EOD price series for a ticker, for charting — not part of the
screen's report (that only carries today's price, for the tripwire).
Fetch-or-cache-through FMP like every other data source in this app: the
first request in a day hits FMP and caches the whole series; every repeat
request that same day is served from the cache, no extra FMP calls. No
separate "run" step like `/screen/` — just ask for it.

**Auth:** required

**Response `200`** (trimmed to 2 of ~1,000+ entries — real shape, newest
first):
```json
{
  "ticker": "RKLB",
  "history": [
    { "date": "2026-07-20", "open": 68.10, "high": 68.95, "low": 66.80, "close": 67.62, "volume": 8123400 },
    { "date": "2026-07-17", "open": 66.90, "high": 69.20, "low": 66.50, "close": 68.30, "volume": 9541200 }
  ]
}
```

**Errors:**
- `404` — this ticker has never been resolved (never added or screened),
  same condition as `GET /api/companies/{ticker}/`.

**`200` with `"history": []`** — not an error, but worth knowing why it
happens (same convention as `filings: []` on the report endpoint):
`FMP_API_KEY` isn't set on the backend, FMP genuinely has no data for the
symbol, **or** — the one actually likely to bite in practice — FMP's
free/starter plans gate `historical-price-eod/full` to a handful of demo
symbols (`AAPL` works; confirmed live). Every ticker this app's own
universe tracks (RKLB, ASTS, etc.) currently 402s under a free key. That
402 is caught and cached as an empty result rather than raised as a `500`
or retried on every request — but the underlying data genuinely isn't
available without upgrading the FMP plan.

---

## `GET /api/companies/{ticker}/quote/`

Live quote for **any** ticker -- unlike `GET /api/companies/{ticker}/`, no
`Company` row is required (nothing needs to have been added or screened
first). Meant for a trade ticket's "here's the current price" display: the
frontend polls this every few seconds while the ticket is open and stops
once it's closed/submitted -- no websocket for this, since a pre-trade price
glance doesn't need tick-by-tick streaming (see `docs/websockets.md` for
what actually does warrant a socket). Backed by
`companies/services/pricing.py::get_quote()`, the same provider-agnostic
(SteadyAPI/Tradier) call the league's own trade execution and
mark-to-market use internally.

**Auth:** required

**Query params:** `asset_class` (optional, default `STOCKS`) -- one of
`STOCKS`, `ETF`, `MUTUALFUNDS`, `FUTURES` when SteadyAPI is the active
provider (`settings.PRICING_PROVIDER`); ignored by Tradier, which
auto-detects the asset class.

**Response `200`:**
```json
{
  "symbol": "RKLB", "company_name": "Rocket Lab USA, Inc.", "exchange": "NASDAQ",
  "market_status": "Open", "last_price": 67.62, "net_change": 1.14,
  "percent_change_pct": 1.71, "is_real_time": true,
  "last_trade_timestamp": "2026-07-20T19:42:08Z",
  "bid": 67.60, "ask": 67.64, "volume": 8123400, "average_volume": 9541200,
  "close": 66.48, "after_hours": null,
  "fifty_two_week_range": "12.40 - 71.20", "day_range": "66.80 - 68.95"
}
```
Exact field set matches whichever provider is active -- see
`companies/services/steady.py::_parse_quote` (SteadyAPI) or
`companies/services/pricing.py::_tradier_quote_to_steady_shape` (Tradier,
normalized into the same shape). `last_price` is `null` if the ticker isn't
recognized by the provider -- that's a normal response, not an error.

**Errors:**
- `404` -- neither `STEADY_API_TOKEN` nor Tradier is configured on the
  backend, e.g. `{"detail": "No live pricing provider is configured on the
  backend."}`.
- `400` -- invalid `asset_class` (SteadyAPI path only).

---

## `GET /api/companies/{ticker}/options-expirations/`

Just the list of expiration dates for a ticker's options -- meant to
populate a date picker before the frontend commits to fetching a full
chain via `options-chain/` below. Cheap on Tradier (a dedicated endpoint);
on SteadyAPI it costs the same as a full chain fetch, since Steady has no
lighter endpoint than that -- see `pricing.get_expirations`'s docstring.

**Auth:** required

**Response `200`:**
```json
{ "expirations": ["2026-09-08", "2026-09-19", "2026-10-16", "2026-12-18"] }
```
Always one flat, sorted list of `YYYY-MM-DD` strings regardless of which
provider is active -- Steady's own `{"weekly": [...], "monthly": [...]}`
shape is flattened here so the frontend never has to branch on provider.

**Errors:**
- `404` -- no live pricing provider is configured on the backend.

---

## `GET /api/companies/{ticker}/options-chain/`

Live options chain for a ticker -- same "no `Company` row required"
contract as the quote endpoint above, for pricing an option before a trade.

**Auth:** required

**Query params:**
- `expiration` (optional, `YYYY-MM-DD`) -- one specific expiration. Omitted,
  the provider picks its own nearest/next expiration.
- `strikes` (optional, positive integer) -- trims `calls` and `puts` down to
  the `N` contracts on each side closest to the money (ranked by
  `|moneyness_pct|`, same ranking the AI agent tools use internally, see
  `pricing.nearest_the_money`). **Strongly recommended** for any UI table --
  a real SPY chain at one expiration is 100+ strikes per side, and Steady/
  Tradier both send it uncompressed. Omit it only if you genuinely want the
  whole chain (e.g. building your own strike-range picker client-side).

**Response `200`:**
```json
{
  "expirations": ["2026-08-15", "2026-08-22", "2026-09-19"],
  "calls": [
    { "symbol": "RKLB260815C00070000", "base_symbol": "RKLB", "strike_price": 70.0,
      "expiration_date": "2026-08-15", "moneyness_pct": 3.52,
      "bid": 2.10, "mid": 2.15, "ask": 2.20, "last": 2.16,
      "volume": 412, "open_interest": 1890,
      "implied_volatility_pct": 61.2, "delta": 0.47, "option_type": "call" }
  ],
  "puts": []
}
```
Trimmed to one contract for brevity -- a real chain has one entry per strike.
Exact field set matches whichever provider is active, normalized to one
shape either way (see `companies/services/pricing.py::get_options_chain`).

**Errors:**
- `404` -- no live pricing provider is configured on the backend.
- `400` -- `strikes` isn't a positive integer.
- `200` with `"calls": [], "puts": []` -- not an error: the ticker has no
  listed options, or (Tradier only) no expiration is available on or after
  today.

---

## `GET /api/companies/{ticker}/clips/`

Every `ResearchClip` the logged-in user has saved for a ticker they track —
Mode 3 deep-dive material (docs/overview.md): filing excerpts, wiki
articles, news, operational KPIs, whatever. Paste-in only — nothing here is
auto-fetched.

**Auth:** required

**Response `200`:**
```json
[
  { "id": 12, "category": "revenue_quality", "source_type": "filing",
    "label": "Backlog $2.2B vs $600M revenue",
    "source": "10-Q Item 7 (MD&A), filed 2026-05-01",
    "text": "Backlog of $2.2 billion as of...",
    "created_at": "2026-07-20T05:10:00Z" },
  { "id": 13, "category": "context", "source_type": "wiki",
    "label": "Electron & Neutron overview",
    "source": "https://en.wikipedia.org/wiki/Rocket_Lab",
    "text": "Electron is a two-stage...",
    "created_at": "2026-07-20T05:11:00Z" }
]
```
Newest first.

**Errors:**
- `404` — this user isn't tracking that ticker yet (add it first via `POST /api/companies/`).

---

## `POST /api/companies/{ticker}/clips/`

Save a new clip against a ticker the user tracks.

**Auth:** required

**Request body:**
```json
{
  "label": "Backlog $2.2B vs $600M revenue",
  "text": "Backlog of $2.2 billion as of...",
  "category": "revenue_quality",
  "source_type": "filing",
  "source": "10-Q Item 7 (MD&A), filed 2026-05-01"
}
```
`label` and `text` are required. `category` is one of
`revenue_quality`, `profitability_path`, `catalyst`, `concentration`,
`dilution`, `insider_activity`, `context` — defaults to `context` if
omitted (background material that doesn't map to one of the six deep-dive
questions). `source_type` is one of `filing`, `news`, `wiki`,
`press_release`, `other` — defaults to `other`. `source` is a free-text
citation or URL, optional.

**Response `201`:** the created clip, same shape as the `GET` list above.

**Errors:**
- `400` — `label` or `text` missing, or `category`/`source_type` not one of
  the valid values.
- `404` — this user isn't tracking that ticker yet.

---

## `DELETE /api/companies/{ticker}/clips/{id}/`

Remove a clip. No edit endpoint — delete and re-`POST` for corrections.

**Auth:** required

**Response:** `204`, empty body.

**Errors:**
- `404` — this user isn't tracking that ticker, or no clip with that `id`
  exists under it (including someone else's clip — clips are only visible
  through the tracking user's own `TrackedCompany` row).

---

## `GET /api/companies/clips/search/`

Semantic search over `ResearchClip` text (embeddings + Weaviate, Phase 6),
across every company the user tracks. Always scoped to the requesting
user -- another user's clips never surface here, regardless of `ticker`.

**Auth:** required

**Query params:**
- `q` (required) -- the search text. Embedded with the same
  `text-embedding-3-large` model used at index time, then matched by
  nearest-neighbor distance.
- `ticker` (optional) -- narrow to one company.
- `limit` (optional, default `10`).

**Response `200`:** nearest matches first, each the same shape as a clip
row plus `ticker` and `distance` (cosine distance -- lower is closer):
```json
[
  { "id": 12, "category": "concentration", "source_type": "filing",
    "label": "Top customer 41% of revenue",
    "source": "10-Q Item 7 (MD&A), filed 2026-05-01",
    "text": "Our top customer accounts for 41% of total revenue...",
    "created_at": "2026-07-20T05:10:00Z",
    "ticker": "RKLB", "distance": 0.28 }
]
```

**Errors:**
- `400` -- `q` missing.
- `503` -- `OPENAI_API_KEY` isn't configured, so the query can't be
  embedded.

---

## `GET /api/companies/{ticker}/clips/search/`

Same search as above, pinned to one company via the URL instead of
`?ticker=` -- matches the nested `clips`/`journal` convention. 404s the
same way as those if the user isn't tracking the ticker.

**Auth:** required

**Query params:** `q` (required), `limit` (optional, default `10`).

**Response `200`:** same shape as the global search above.

**Errors:**
- `400` -- `q` missing.
- `404` -- this user isn't tracking that ticker yet.
- `503` -- `OPENAI_API_KEY` isn't configured.

---

## `GET /api/companies/{ticker}/journal/`

The Mode 3 deliverable (docs/overview.md): what they sell / why they win /
what kills them, plus one falsifiable checkpoint with a date. Auto-creates
a blank row the first time it's fetched for a given tracked company — not
an error, just nothing written yet.

**Auth:** required

**Response `200`:**
```json
{
  "sells": "",
  "wins": "",
  "kills": "",
  "checkpoint": "",
  "checkpoint_date": null,
  "updated_at": "2026-07-20T05:12:00Z"
}
```

**Errors:**
- `404` — this user isn't tracking that ticker yet.

---

## `PATCH /api/companies/{ticker}/journal/`

Update whichever journal fields are present in the body — omitted fields
are left as-is (partial update, same as the dashboard status endpoint's
pattern but multi-field).

**Auth:** required

**Request body (any subset of):**
```json
{
  "sells": "Launch services + space systems.",
  "wins": "Vertical integration, Neutron optionality.",
  "kills": "Neutron delay past 2027, margin stall.",
  "checkpoint": "Neutron Q4 launch",
  "checkpoint_date": "2026-12-15"
}
```
`checkpoint_date` is `YYYY-MM-DD` or `null` to clear it.

**Response `200`:** the full updated journal row, same shape as the `GET`
above.

**Errors:**
- `400` — `checkpoint_date` isn't a valid date.
- `404` — this user isn't tracking that ticker yet.

---

## `GET /api/companies/{ticker}/wiki/`

Every Wikipedia page linked to a tracked company for ongoing monitoring
(`WikiSource`) -- a daily job rechecks each one and keeps its
`ResearchClip`s (`source_type: "wiki"`) in sync with the current content.

**Auth:** required

**Response `200`:**
```json
[
  { "id": 3, "page_title": "Rocket Lab", "label": "",
    "last_checked_at": "2026-07-28T22:00:00Z", "last_changed_at": "2026-07-20T10:00:00Z",
    "created_at": "2026-07-18T09:00:00Z" }
]
```
`last_changed_at` is only updated when a poll actually detects different
content, not on every check -- compare it against `last_checked_at` to see
whether the page has been static.

**Errors:**
- `404` -- this user isn't tracking that ticker yet.

---

## `POST /api/companies/{ticker}/wiki/`

Link a Wikipedia page to a tracked company. Resolves redirects and
validates the title synchronously against Wikipedia (so a bad title or a
disambiguation page `400`s immediately in this response), then queues the
actual fetch + chunk + embed as a Celery task -- the request doesn't block
on it.

**Auth:** required

**Request body:**
```json
{ "page_title": "https://en.wikipedia.org/wiki/Rocket_Lab", "label": "Parent company" }
```
`page_title` is required and accepts **either** a plain title (exact or
approximate -- redirects resolve automatically) **or** a full
`en.wikipedia.org` URL, `#section` anchors and the `/w/index.php?title=...`
form included -- this is the expected frontend input: a user pastes a
Wikipedia link, the backend extracts and normalizes the title itself
(`companies/services/wikipedia_client.py::resolve_title_input`), no
client-side parsing needed. Only English Wikipedia is supported; a
non-`en.wikipedia.org` URL `400`s rather than silently fetching the wrong
article. `label` is optional, free text.

**Response:** `201` with the created row (same shape as the `GET` above,
`last_checked_at`/`last_changed_at` both `null` until the queued task
completes), or `200` with the existing row if this page (by its resolved
canonical title) was already linked to this company -- idempotent, not an
error.

**Errors:**
- `400` -- `page_title` missing, not a recognizable title/URL, a
  non-English Wikipedia URL, no such page, or it resolved to a
  disambiguation page (link the specific article instead).
- `404` -- this user isn't tracking that ticker yet.

---

## `DELETE /api/companies/{ticker}/wiki/{id}/`

Unlink a page: stops future daily checks and removes its `ResearchClip`s
(Postgres + the matching Weaviate vectors) from semantic search.

**Auth:** required

**Response:** `204`, empty body.

**Errors:**
- `404` -- this user isn't tracking that ticker, or no linked page with
  that `id` exists under it.

---

## `GET /api/companies/subreddits/`

Every subreddit this user has asked to have scanned for ticker mentions --
the "Social" discovery watch list, independent of `TrackedCompany`. A
subreddit watch isn't about any one ticker; it's "keep an eye on r/X and
tell me what comes up."

**Auth:** required

**Response `200`:**
```json
[{ "id": 4, "name": "wallstreetbets", "added_at": "2026-08-29T12:00:00Z", "last_scanned_at": "2026-09-19T12:00:03Z" }]
```
`last_scanned_at` is `null` until the scan job has attempted this
subreddit at least once, then advances on every attempt (Beat sweep or
`.../refresh/` alike) -- **including a failed one** (a Steady/OpenAI
outage still counts as "reached," just not "succeeded"). A value that
never advances despite the scan job supposedly running is the fastest
"is this actually working" check -- same field, sortable, in the admin
panel (`TrackedSubreddit`).

---

## `POST /api/companies/subreddits/`

Add a subreddit to the watch list.

**Auth:** required

**Request body:**
```json
{ "name": "wallstreetbets" }
```
A leading `r/` is stripped server-side (`"r/wallstreetbets"` and
`"wallstreetbets"` are equivalent) -- but only that literal prefix, so a
subreddit that genuinely starts with `r` (e.g. `"robinhood"`) is left
alone. Adding a name already being watched by this user is idempotent
(`get_or_create`), not a `400`.

**Response `201`:** the created row, same shape as the `GET` above.

**Errors:**
- `400` -- `name` missing/blank.

---

## `DELETE /api/companies/subreddits/{id}/`

Stop watching a subreddit. Genuinely removable (unlike `TrackedCompany`,
which is only ever status-flipped) -- nothing else references a row here
by foreign key.

**Auth:** required

**Response:** `204`, empty body.

**Errors:**
- `404` -- no watch with that `id` belonging to this user.

---

## `POST /api/companies/subreddits/{id}/refresh/`

Scan this one subreddit **right now** instead of waiting for the next
twice-daily scheduled scan (`companies.tasks.scan_tracked_subreddits`,
~12:00 and ~00:00 UTC). Same dispatched-asynchronously shape as
`POST .../screen/` above.

**Auth:** required

**Response `202`:**
```json
{ "status": "queued", "id": 4, "name": "wallstreetbets" }
```
Not the result -- that arrives as `subreddit.progress` / `subreddit.complete`
/ `subreddit.error` over the user's WebSocket group (`docs/websockets.md`),
and is visible afterward via `GET .../subreddit-mentions/` regardless.

**Errors:**
- `404` -- no watch with that `id` belonging to this user.

---

## `POST /api/companies/subreddits/refresh/`

Scan **every** subreddit this user watches, right now -- the "refresh
now" button for the whole list. Queues one independent
`refresh_tracked_subreddit` task per watch (the same task the
single-subreddit endpoint above uses), so each subreddit reports back its
own `subreddit.progress`/`complete`/`error` triple rather than one bulk
event -- a slow or failing subreddit doesn't hold up the others.

**Auth:** required

**Response `202`:**
```json
{ "status": "queued", "count": 3 }
```
`count` is how many subreddits were queued (this user's watch-list size at
request time).

---

## `GET /api/companies/subreddit-mentions/`

Ticker mentions found by the scan job, scoped to this user's watch list.
Default: aggregated `{subreddit, ticker, count}` rows, a `Count` query
over the raw mention rows computed at read time (not a stored aggregate),
sorted highest-first.

**Auth:** required

**Query params (optional, combinable):**
- `?ticker=SLS` -- drill-down mode instead: every individual mention (post
  title/excerpt/permalink/score) for that one ticker, for a "click a
  ticker to see context" UI.
- `?subreddit=wallstreetbets` -- narrows either mode to one subreddit.
  Combined with `?ticker=`, narrows the drill-down to posts mentioning
  that ticker in just that one subreddit (useful once a ticker shows up
  across more than one watched subreddit).
- `?days=7` -- narrows either mode to mentions first scanned in the last
  N days (a positive integer; `0`, negative, or non-numeric `400`s).
  Without it, counts are all-time -- worth adding once this has run long
  enough that old/dead threads start padding the aggregated counts.
  Combinable with both params above.

**Response `200` (aggregated, default):**
```json
[{ "subreddit": "wallstreetbets", "ticker": "SLS", "count": 2 }]
```

**Response `200` (`?ticker=` drill-down):**
```json
[{
  "id": 1, "subreddit": "wallstreetbets", "ticker": "SLS",
  "title": "$SLS mooning today", "permalink": "/r/wallstreetbets/comments/p1/",
  "excerpt": "...", "score": 100, "num_comments": 10,
  "scanned_at": "2026-09-19T12:00:03Z"
}]
```
`scanned_at` is when this row was first written, not the post's own
creation time -- the fastest way to check "is the scan job actually
running and finding anything recent" (also visible directly in the admin
panel, `SubredditMention`, sorted by this column).

**Errors:**
- `400` -- `days` present but not a positive integer, e.g.
  `{"days": ["Must be a positive integer."]}`.

---

## `GET /api/notifications/`

The logged-in user's durable notifications (`notifications` app), newest
first -- the REST-side view of what also arrives live over the WebSocket
as `notification.new` (`docs/websockets.md`). Wiki's daily change-detection
job is the first producer (`wiki.page_changed`).

**Auth:** required

**Query params:**
- `unread` (optional) -- `1` to return only unacknowledged notifications.

**Response `200`:**
```json
[
  { "id": 7, "notification_type": "wiki.page_changed",
    "title": "RKLB: \"Rocket Lab\" changed on Wikipedia",
    "body": "Content changed -- use get_wiki_page_changes to see what.",
    "payload": { "ticker": "RKLB", "wiki_source_id": 3, "page_title": "Rocket Lab" },
    "created_at": "2026-07-28T22:00:03Z", "acknowledged_at": null }
]
```

---

## `POST /api/notifications/{id}/ack/`

Mark one notification acknowledged (seen/handled). Idempotent -- acking an
already-acked notification `200`s with the same `acknowledged_at` rather
than erroring. An unacknowledged notification keeps being redelivered over
the WebSocket on every reconnect until this is called.

**Auth:** required

**Response `200`:** the notification row, same shape as the `GET` above,
with `acknowledged_at` now set.

**Errors:**
- `404` -- no notification with that id belonging to this user.

---

## `GET /api/assistant/models/`

Active Claude models the chat assistant can be started with (`assistant`
app) — the same catalog `POST .../messages/`'s `model_id` validates
against. Admin-managed (`/admin/`), not a setting — add a model there when
it's worth offering, deactivate one without deleting it (past messages
still resolve to the same row).

**Auth:** required

**Response `200`:**
```json
[
  { "model_id": "claude-opus-5", "display_name": "Claude Opus 5", "is_default": false },
  { "model_id": "claude-sonnet-5", "display_name": "Claude Sonnet 5", "is_default": true }
]
```

---

## `GET /api/assistant/conversations/`

Every conversation the user has, newest-active first. No tools yet, no
per-ticker scoping — a conversation is just a back-and-forth thread with
the assistant.

**Auth:** required

**Response `200`:**
```json
[
  { "id": 4, "title": "", "created_at": "2026-07-26T18:00:00Z", "updated_at": "2026-07-26T18:05:00Z" }
]
```
`title` is user-settable (see `POST` below); blank by default — nothing
auto-generates one from the first message yet.

---

## `POST /api/assistant/conversations/`

Start a new, empty conversation. Sending the first message is a separate
call (`POST .../messages/` below) — matches "Add" vs. "Run screen" being
distinct actions elsewhere in this API.

**Auth:** required

**Request body (optional):** `{"title": "Space stocks screen"}`

**Response `201`:** the created conversation, same shape as the `GET` above.

---

## `DELETE /api/assistant/conversations/{id}/`

Delete a conversation and every message in it.

**Auth:** required

**Response:** `204`, empty body.

**Errors:**
- `404` — no conversation with that id belonging to this user.

---

## `GET /api/assistant/conversations/{id}/messages/`

Every message in a conversation, oldest first — what a frontend loads when
the user switches to it.

**Auth:** required

**Response `200`:**
```json
[
  { "id": 12, "role": "user", "text": "What does this company do?", "ticker": "RKLB",
    "model_id": null, "input_tokens": null, "output_tokens": null,
    "tool_calls": [], "tool_use_output_tokens": null, "cost_usd": null,
    "created_at": "2026-07-26T18:00:00Z" },
  { "id": 13, "role": "assistant", "text": "Rocket Lab Corp (RKLB) is a space company that...",
    "ticker": null, "model_id": "claude-sonnet-5", "input_tokens": 18019, "output_tokens": 777,
    "tool_calls": ["list_filings", "get_company_report"], "tool_use_output_tokens": 126,
    "cost_usd": "0.043808", "created_at": "2026-07-26T18:00:03Z" }
]
```
`ticker` reflects this specific turn's scoping (`null` if it wasn't
ticker-scoped) — assistant rows never carry it themselves, only whichever
user turn they're answering. `model_id`/`input_tokens`/`output_tokens`/
`tool_calls`/`tool_use_output_tokens`/`cost_usd` are only ever populated on
`assistant` rows — a `user` row didn't call the API on its own.

`input_tokens`/`output_tokens`/`cost_usd` are **totals across every API
round-trip** this turn took, not just the final one — if the assistant
called tools before answering, each tool call is a separate call to
Anthropic with its own usage, all summed here. `tool_calls` lists which
tools were actually invoked, in order (`[]` if none were); `tool_use_output_tokens`
is the slice of `output_tokens` spent on those tool-calling round-trips
specifically (deciding to call a tool, plus any preamble text) — subtract
it from `output_tokens` to get what the final answer itself cost. There's
no equivalent split for `input_tokens`: every round resends the full prior
conversation, so a round's input total reflects "everything said so far,"
not that round's tool call specifically.

A failed or refused turn still appears here as an `assistant` row (its
`text` explains the failure) — every turn
gets a row, including failed ones, so
role alternation stays intact for the next call to Anthropic.

**Errors:**
- `404` — no conversation with that id belonging to this user.

---

## `POST /api/assistant/conversations/{id}/messages/`

Send a new user turn into a conversation. Persists the user's message
synchronously (so a follow-up `GET` shows it immediately, before the
assistant has replied) and queues the actual model call — the reply
streams over the user's WebSocket group as `assistant.token` /
`assistant.complete` / `assistant.error` (`docs/websockets.md`), not over
this HTTP response.

Context sent to the model is the conversation's last
`ASSISTANT_HISTORY_LENGTH` messages (default 10, both roles), trimmed
further if that plus the new message would exceed the chosen model's
`max_input_tokens` — checked with Anthropic's real `count_tokens` endpoint,
not an estimate.

**Auth:** required

**Request body:**
```json
{ "message": "What does this company do?", "ticker": "RKLB", "model_id": "claude-opus-5" }
```
`message` is required. `model_id` is optional — omit it to use whichever
model is flagged default in `AssistantModel` (see `GET
/api/assistant/models/`). `ticker` is also optional and scopes this
specific turn to one company — resolved the same way `POST
/api/companies/` is (creates the `Company` on first sight if it's a real
SEC ticker). It's per-message, not per-conversation: one conversation can
mix scoped and unscoped turns freely (only single-ticker scoping exists so
far — comparing multiple tickers or an explicit "all tickers" mode isn't
built yet).

**Response `202`:** the created user message row, same shape as the `GET`
above (`model_id`/tokens/cost all `null` for a user row).

**Errors:**
- `400` — `message` missing, `model_id` isn't an active model, or `ticker`
  isn't a real SEC ticker.

**Tools available to the assistant** (`assistant/tools.py`), which it calls
on its own when relevant — nothing the frontend has to trigger directly.
See `docs/tools-reference.md` for the full side-by-side comparison against
the paper trading league's AI agents, including which provider/env var
each one needs:
- `list_filings` — a company's 10-K/10-Q/20-F filings from EDGAR, each flagged
  with whether it's already been ingested into semantic search for this user.
- `get_company_report` — the same cached data as `GET /api/companies/{ticker}/`
  (price, market cap, runway, tripwire, multi-year metrics).
- `search_research_clips` — semantic search over the user's own saved
  ResearchClips, optionally narrowed to one ticker.
- `search_media_transcripts` — semantic search over the user's uploaded
  audio/video transcripts (see `POST /api/media/` below), returning
  speaker + timestamp alongside each hit so the assistant can cite exactly
  who said what and when.
- `propose_filing_ingestion` — proposes parsing + embedding one filing into
  semantic search. Doesn't ingest anything itself; see the confirmation
  endpoint below.
- `search_wikipedia` — live Wikipedia search, nothing persisted. For
  finding/confirming a page before linking it, or answering a background
  question directly.
- `search_web` — generic open-web search (Brave Search API), for anything
  the other tools can't answer: current events, a company's own site,
  news outside Reddit. `BRAVE_API_KEY` must be configured or this tool
  isn't offered at all. The same tool (same Brave-backed implementation)
  is also available to League AI competitors (`league/tools.py`),
  alongside whichever provider's own native `web_search` capability that
  competitor's model has.
- `link_wiki_page` — links a Wikipedia page to a tracked company (same
  effect as `POST .../wiki/` above), runs immediately, no confirmation
  needed.
- `list_linked_wiki_pages` — lists a company's linked pages and when each
  was last checked/changed.
- `unlink_wiki_page` — unlinks a page (same effect as `DELETE .../wiki/{id}/`).
- `get_wiki_page_changes` — paragraph-level diff between a linked page's
  two most recent snapshots, for "did something notable happen" questions.

---

## `POST /api/assistant/conversations/{id}/pending-actions/{pending_id}/`

Approve or deny an action the assistant proposed (currently only
`ingest_filing`, from `propose_filing_ingestion`). The proposal itself
arrives over the WebSocket as `assistant.confirmation_required`
(`docs/websockets.md`) — this endpoint is where the user actually decides.

Approving queues the real work as a plain Celery task; it does **not**
go back through Claude — the result (success, failure, or a plain
"declined" note) lands as an ordinary assistant-role message and an
`assistant.action_complete` WebSocket push, same as any other turn.

**Auth:** required

**Request body:** `{"approved": true}` (or `false`)

**Response `200`:**
```json
{ "id": 4, "action_type": "ingest_filing",
  "params": {"ticker": "SLS", "accession_number": "0001390478-26-000008"},
  "description": "Ingest 10-Q filed 2026-05-12 for SLS into semantic search",
  "status": "approved", "created_at": "2026-07-26T20:00:00Z", "resolved_at": "2026-07-26T20:00:05Z" }
```

**Errors:**
- `404` — no conversation with that id belonging to this user, or no
  *pending* action with that id on it (already resolved, or never existed).

---

## `GET /api/media/`

Every mp3/mp4 the user has uploaded (`media` app), newest first.

**Auth:** required

**Response `200`:**
```json
[
  { "id": 3, "original_filename": "q1-earnings-call.mp3", "media_type": "audio", "ticker": "SLS",
    "duration_seconds": 1842.3, "status": "ready", "error_message": "",
    "created_at": "2026-07-26T20:00:00Z", "updated_at": "2026-07-26T20:04:12Z" }
]
```

---

## `POST /api/media/`

Upload an mp3 or mp4. Processing (audio extraction for video, diarized
transcription, embedding into semantic search) **starts automatically** --
unlike the assistant's `propose_filing_ingestion` tool, there's no
confirmation step here, because uploading the file is itself the user's
explicit authorization to process it. The result streams over the user's
WebSocket group (`docs/websockets.md` `media.*`), not over this response.

**Auth:** required

**Request body:** `multipart/form-data` — `file` (required, `.mp3`/`.mp4`
only for now), `ticker` (optional, same resolution as everywhere else in
this API).

**Response `202`:** the created media file row, same shape as the `GET`
above (`status: "uploaded"`, `duration_seconds: null` until processing
determines it).

**Errors:**
- `400` — `file` missing, an unsupported extension, or `ticker` isn't a
  real SEC ticker.

---

## `GET /api/media/{id}/`

**Auth:** required

**Response `200`:** same shape as the `GET` list above.

**Errors:**
- `404` — no media file with that id belonging to this user.

---

## `DELETE /api/media/{id}/`

Removes the file, its transcript segments, and the S3 object.

**Auth:** required

**Response:** `204`, empty body.

**Errors:**
- `404` — no media file with that id belonging to this user.
