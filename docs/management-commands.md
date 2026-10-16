# Management commands

All commands are run as `python manage.py <command>` from the project
root (activate `.venv` first). `--help` on any of them lists the full
argument set with defaults; this doc is a map of what exists and when
you'd reach for it, not a full flag reference.

---

## companies app

### `screen TICKER [--max-age-days N]`

Runs Mode 1 (Screen) for a ticker: fetches/caches SEC EDGAR + FMP data,
prints the company, latest filing, what was fetched vs. not reported by
this filer, and the tripwire check (price × shares outstanding vs. FMP's
`marketCap`, flags drift beyond tolerance -- usually a units mismatch or
a schema break). `--max-age-days` controls cache reuse; `0` forces a
refresh. Needs `FMP_API_KEY`/`SEC_CONTACT_EMAIL` for the FMP side and the
tripwire; runs (skipping the tripwire) without them.

```
python manage.py screen AAPL
python manage.py screen AAPL --max-age-days 0
```

### `init_weaviate`

Creates the Weaviate collection for `ResearchClip` embeddings if it
doesn't exist yet. Idempotent -- safe to rerun. Run once before anything
tries to index a clip.

### `seed_filing_clips TICKER --user EMAIL [--forms 10-K 10-Q] [--max-tokens N] [--keep-tables] [--force]`

Seeds real `ResearchClip`s from a ticker's latest 10-K/10-Q through the
actual Docling + HybridChunker ingestion pipeline (`companies/services/
filing_ingestion.py`) -- the same pipeline the assistant app's
filing-ingestion tool uses for one filing at a time. `--user` must be an
existing user's email (creates a `TrackedCompany` for them if they
weren't already tracking the ticker). Skips a filing already seeded
(matched by accession number) unless `--force`. Needs `OPENAI_API_KEY`
(embeddings).

```
python manage.py seed_filing_clips RKLB --user you@example.com
python manage.py seed_filing_clips RKLB --user you@example.com --forms 10-K --force
```

### `spike_filing_chunks [TICKER] [--form 10-K] [--query Q ...] [--max-tokens N] [--top N] [--keep-tables]`

Ad-hoc research tool, not a production path: fetches one real filing,
parses it with Docling, chunks it (HybridChunker), embeds every chunk,
and runs an in-memory similarity search against `--query` (repeatable;
defaults to three fixed re-test queries) to eyeball chunk/retrieval
quality by hand. Writes nothing to Postgres or Weaviate. `TICKER`
defaults to `RKLB`. Needs `OPENAI_API_KEY`.

```
python manage.py spike_filing_chunks
python manage.py spike_filing_chunks NVDA --form 10-Q --query "gross margin trend" --top 3
```

---

## media app

### `init_media_weaviate`

Same idea as `init_weaviate`, for `MediaSegment` embeddings instead of
`ResearchClip`s. Idempotent, run once before media transcript search can
index anything.

---

## league app

### `seed_league --owner-email EMAIL [--name ...] [--start-date ...] [--end-date ...] [--human NAME:EMAIL ...] [--ai NAME:MODEL_ID:PERSONA ...] [--six-way] [other Competition fields]`

Bootstraps one `Competition` and its `Competitor`s -- data, not schema.
`--owner-email` must be an existing user (AI competitors' `user` FK, so
that account gets trade-activity notifications). Human competitors are
opt-in via repeated `--human "Name:email"` (the email must already be a
real user -- this doesn't create accounts). AI competitors are opt-in via
repeated `--ai "Name:model_id:persona"`, where `persona` matches a preset
keyword (`regular` / `lunatic` / `daytrader`) or, if it doesn't, is used
verbatim as custom persona text -- so one league can mix providers and
mix presets with fully custom prompts. `--six-way` overrides `--ai`/
`--model` and seeds the six real dev-DB competitors (Claude Opus 5,
Gemini 3.1 Pro, GPT-5.6 Sol, each with a Regular + Gambling Daytrader
persona) verbatim, so they don't need retyping on a fresh environment.
Omitting both `--ai` and `--six-way` falls back to the original default:
two Claude competitors (Regular/Lunatic) on `--model` (or the catalog
default). Safe to rerun -- competition and competitors are
`get_or_create`d by name.

```
python manage.py seed_league --owner-email you@example.com
python manage.py seed_league --owner-email you@example.com --six-way
python manage.py seed_league --owner-email you@example.com \
  --human "George:george@example.com" \
  --ai "Gemini Regular:gemini-3.1-pro-preview-customtools:regular"
```

### `refresh_ticker_snapshots [--competition ID]`

Live-refetches and persists a `TickerSnapshot` for every open `Position`
(or one competition's, with `--competition`). No market-hours gate --
unlike the normal Beat tick (`league.tasks.refresh_open_position_quotes`,
9:30am-4:00pm ET Mon-Fri), so it's the way to get real marks right after
a fresh deploy (the snapshot table starts empty and everyone would
otherwise show flat 0% return until the next market-hours tick) or any
time you want a refresh outside the normal cadence.

```
python manage.py refresh_ticker_snapshots
python manage.py refresh_ticker_snapshots --competition 1
```

### `sell_all_positions (--competitor ID | --competition ID | --all) [--thesis TEXT] [--dry-run]`

Closes every open position for one competitor, one competition, or
everything, by submitting real closing trades through
`execution.submit_trade()` -- never edits `Position` rows directly, so
`cash_balance` and the `Trade` audit trail stay consistent (a "sold"
position is a real filled `Trade` you can see in history, not a row that
vanished). Short legs (the leg of a covered call/cash-covered put) close
before the long positions they might be covering, matching
`execution.py`'s own covered-call check. Built for "I'm just testing,
flatten everything" -- doesn't touch `Competitor.is_paused` (that's what
actually stops an AI's wake-up loop). One of `--competitor` /
`--competition` / `--all` is required. Use `--dry-run` first.

```
python manage.py sell_all_positions --competitor 3 --dry-run
python manage.py sell_all_positions --competitor 3
python manage.py sell_all_positions --all --thesis "End of season, flattening everyone."
```

### `enforce_wakeup_spacing [--dry-run]`

Cancels already-scheduled `AgentWakeup`s that violate the 3-minute
minimum spacing `league.tools.schedule_wakeup` enforces going forward
(`league/services/wakeup_spacing.py`) -- a safety net for wakeups created
*before* that check existed (e.g. a competitor triple-booked at the same
minute). Per AI competitor, walks future scheduled wakeups in order,
keeps the earliest of any cluster within 3 minutes and cancels the rest,
then separately cancels anything within 3 minutes of that competitor's
own forced daily check-in slots (8:00am / 9:35am / 3:15pm ET) regardless
of what else survived. "Cancel" sets `status=STATUS_CANCELLED` rather
than deleting -- an already-dispatched Celery `apply_async(eta=...)` task
can't be pulled back, so `run_agent_wakeup` checks this status itself and
no-ops if a cancelled wakeup still fires.

```
python manage.py enforce_wakeup_spacing --dry-run
python manage.py enforce_wakeup_spacing
```

### `wakeup_diagnostics [--days N] [--json PATH]`

Read-only health report on the AI competitor wake-up loop -- answers "why
did an agent go quiet?" with evidence instead of a guess. Safe on
production (no writes, no task dispatch, no LLM calls). Six sections,
each isolating one candidate cause: session coverage (blackout days /
wide gaps between turns), orphaned wake-ups (scheduled but never fired),
forced check-in coverage (did the guaranteed floor actually fire every
weekday), daily wake-up cap pressure, failed wake-ups (with how long the
resulting gap lasted), and past-dated schedules (a wake-up whose `run_at`
was already behind `created_at`, which fires immediately rather than
waiting). `--days` sets the lookback window (default 14); `--json` also
writes the full findings, including every row id, for deeper digging
beyond the stdout summary.

```
python manage.py wakeup_diagnostics --days 14
python manage.py wakeup_diagnostics --days 30 --json wakeups.json
```

### `export_league_data [--days N] [-o PATH] [--full-transcripts] [--include-snapshots]`

Read-only, safe-on-production dump of the whole `league` app to one JSON
file for offline analysis -- not `manage.py dumpdata league`, because
that can't trim `AgentTurn.transcript` (a full LLM transcript per turn,
including tool results, that would dominate the file) or
`AccountValueSnapshot` (written every ~10 minutes per competitor, the
app's highest-volume table and irrelevant to anything but the equity
curve) down to just what's diagnostically useful. Both are summarized by
default; `--full-transcripts` / `--include-snapshots` bring the full
rows back. Also bundles context dumpdata wouldn't: `TIME_ZONE`, the
league's Beat schedule entries, and the wake-up-loop constants
(`LEAGUE_MAX_WAKEUPS_PER_DAY`, `MIN_WAKEUP_SPACING`,
`SAFETY_NET_WAKEUP_DELAY`) that shape its behavior, alongside the data
itself. Not a fixture -- flattened for reading, not `loaddata`. `--days`
windows the high-volume tables (trades, wake-ups, turns, snapshots);
config/competition/competitor/position rows are always dumped in full.

```
python manage.py export_league_data --days 30 -o league.json
python manage.py export_league_data --days 7 --full-transcripts -o debug.json
```

### `watch_quote TICKER [--interval SECS] [--count N] [--strike PRICE --expiration YYYY-MM-DD [--type call|put]]`

Polls SteadyAPI's live quote (bare ticker) or one option contract's chain
entry (`--strike` + `--expiration`, both required together) every
`--interval` seconds and prints it -- a manual eyeball check against a
real brokerage tab ("does this actually track what Fidelity shows me
right now"), not an automated test. Always calls SteadyAPI live, never
the cached `TickerSnapshot` the scoreboard/competitor-detail display path
uses (`league/services/valuation.py`) -- so a mismatch against a real
brokerage here is a real SteadyAPI data issue, not this app's caching
layer. Ctrl+C to stop, or `--count N` to stop automatically. Needs
`STEADY_API_TOKEN`.

```
python manage.py watch_quote AAPL
python manage.py watch_quote AAPL --interval 30 --count 10
python manage.py watch_quote AAPL --strike 230 --expiration 2026-09-19 --type call
```

### `tradier_itm_quote TICKER [--interval SECS] [--count N]`

Polls Tradier every `--interval` seconds (default 5) and prints one line:
time, ticker, spot, expiration, strike, call price, put price -- for the
closest in-the-money call and the put at that same strike, nearest
Friday expiration (today counts as "the following Friday" if run on a
Friday itself). Built to eyeball whether Tradier's options pricing
actually moves in real time, as a fresher alternative to SteadyAPI, whose
options chain only updates ~every 10 minutes. Call/put price is mid
(`(bid+ask)/2`), falling back to `last` if no bid/ask. The expiration is picked once at
startup; spot and the chain are re-fetched every poll, and the ITM strike
is recomputed each time, so the printed strike can change mid-session if
the underlying trades across a strike boundary -- that's correct, not a
bug. Ctrl+C to stop, or `--count N` to stop automatically. Needs a
**production** `TRADIER_API_TOKEN` -- Tradier's sandbox exercises the
same API shape but serves delayed data, so it can't show real movement
(the command warns loudly if `TRADIER_BASE_URL` points at sandbox).

```
python manage.py tradier_itm_quote AAPL
python manage.py tradier_itm_quote AAPL --interval 10
python manage.py tradier_itm_quote AAPL --count 20
```
