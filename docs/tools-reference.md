# AI Tools Reference

Every tool either AI surface can call, side by side: the human-facing
**Assistant** chat (`assistant/tools.py`, `build_tools()`) and the paper
trading league's **AI agents** (`league/tools.py`, `build_league_tools()`).
Both are closure factories called fresh per turn/wake-up, not static lists --
`✅` below means "registered for that surface when its provider requirement
is met," not "always present regardless of configuration." For the prose
walkthrough of how/when each app's agent actually uses these, see
`docs/api-endpoints.md`'s `POST .../messages/` tools list (Assistant) and
`docs/league-agent-guide.md` §"tools an AI competitor can call" (League) --
this page exists to compare the two side by side and name every provider in
one place, not to replace either.

## How to read "Provider"

The service each tool actually calls, and the env var(s) that must be set
for it to return real data. Two different failure shapes show up in the
tables below:

- **Gated tools** (marked with a dagger `†` on the tool name) are filtered
  out of the tool list entirely when unconfigured -- the model never sees
  them, so it can't call something guaranteed to fail. This is
  `assistant/tools.py`'s and `league/tools.py`'s own `_capability_gate`
  dict, re-evaluated fresh on every `build_tools()`/`build_league_tools()`
  call (so a live env change or `@override_settings` in a test takes effect
  immediately, no restart-shaped caching).
- **Ungated tools** stay registered even with nothing configured -- calling
  one with its provider missing raises a `ToolError` with a clear message
  instead of silently failing. These are the "core, cross-provider" tools
  (`get_stock_quote`, `get_options_chain`, `get_technicals`, `list_filings`,
  ...) where being unconfigured is a total misconfiguration of the
  deployment, not a narrow capability gap worth hiding the tool over.

`PRICING_PROVIDER` (`steady` or `tradier`, `core/settings.py`) picks which
provider every quote/options-chain call in the whole app goes through
(`companies/services/pricing.py`) -- it's not a per-tool choice.

---

## Market data & pricing

| Tool | Description | Provider | Assistant | AI agents |
|---|---|---|---|---|
| `get_stock_quote` | Live last/bid/ask/volume/52-week & day range for one symbol. | Steady or Tradier (`PRICING_PROVIDER`) | ✅ | ✅ |
| `get_stock_quotes` | Batch form of the above -- N tickers in one round trip (parallel fetch), not N tool-call rounds. | Steady or Tradier | — | ✅ |
| `get_options_chain` | ~10 calls/puts nearest the money for one expiration, with bid/ask/greeks/IV. | Steady or Tradier | ✅ | ✅ |
| `get_technicals` | Last price, trailing avg volume, 20d ATR, 50/200d SMA, 50d high, days-to-earnings, one call. | Steady or Tradier (last price) + **Tradier only** (ATR/SMA/volume/high) + **FMP** (earnings date) | ✅ | ✅ |
| `get_price_history` | Last N daily OHLCV bars, capped at 90 per call. | **Tradier only** | ✅ | ✅ |
| `scan_down_streaks` | Full-market scan for tickers on a 3-5(default)-session down-streak while above their own N-day SMA -- capped at 50 candidates, longest streak first. | **Polygon** (`companies.models.DailyBar`, independent of `PRICING_PROVIDER`) | ✅ | ✅ |
| `get_intraday_candles`† | Today's 1/5/15-min OHLCV bars, regular session only, capped to the most recent 100. | **Tradier only** (production token for real-time; sandbox is delayed) | ✅ | ✅ |

## News & Reddit

| Tool | Description | Provider | Assistant | AI agents |
|---|---|---|---|---|
| `get_market_news` | Recent market news/press releases, optionally scoped to one ticker. `news_type` filter confirmed unreliable server-side. | Steady | — | ✅ |
| `get_reddit_sentiment`† | Raw Reddit posts mentioning a ticker (title/score/excerpt/permalink) -- NOT subreddit-scoped (Steady's own filter param is confirmed broken), NOT a computed score. | Steady | ✅ | ✅ |
| `scan_subreddit_for_tickers`† | Live one-off scan of one subreddit's current posts -- which tickers are genuinely being discussed, via LLM extraction (not keyword/cashtag matching). | Steady (fetch) + OpenAI (extraction) | ✅ | ✅ |
| `track_subreddit` | Add a subreddit to the ongoing twice-daily watch-list scan (`companies.tasks.scan_tracked_subreddits`). | None (local DB write) | ✅ | — |
| `list_tracked_subreddits` | List the user's watched subreddits. | None | ✅ | — |
| `untrack_subreddit` | Remove a subreddit from the watch list. | None | ✅ | — |
| `get_subreddit_mentions` | Read results already collected by the twice-daily scan -- does NOT trigger a new scan. Aggregated counts, or drill down to one ticker's posts. | None (reads DB populated by the Steady+OpenAI Beat scan) | ✅ | — |

## Web search

| Tool | Description | Provider | Assistant | AI agents |
|---|---|---|---|---|
| `search_web`† | Generic open-web search -- title/url/description per result. The only GENERIC web search either surface has of its own. | Brave Search API | ✅ | ✅ |
| `web_search` (native, not a `@beta_tool`) | Each model provider's own server-side search tool, attached alongside everything above, not filtered by any gate here. **Anthropic's wiring is unverified against a real live turn** (see `league/tools.py`'s own module docstring). | Anthropic (`web_search_20250305`) / OpenAI (`{"type": "web_search"}`) / Gemini (`GoogleSearch()`) -- whichever the competitor's `assistant_model` is | — | ✅ |

## Filings & company data

| Tool | Description | Provider | Assistant | AI agents |
|---|---|---|---|---|
| `list_filings` | A company's 10-K/10-Q/20-F filings from EDGAR (form, date, accession number, URL), flagged with ingestion status. | SEC EDGAR (`SEC_CONTACT_EMAIL` required in the User-Agent) | ✅ | ✅ |
| `get_company_report` | Latest CACHED report: price, market cap, runway, tripwire, multi-year metrics. Reads only what a prior screen already saved -- no live fetch. | None at call time (populated earlier by FMP + SEC EDGAR via a screen) | ✅ | ✅ |
| `propose_filing_ingestion` | Proposes parsing + embedding one filing into semantic search -- does NOT ingest immediately, shows a confirmation prompt instead. | SEC EDGAR (fetch) + Docling (parse, `filing_ingestion` service) + OpenAI (embeddings) + Weaviate (index) | ✅ | — |

## Wikipedia

| Tool | Description | Provider | Assistant | AI agents |
|---|---|---|---|---|
| `search_wikipedia` | Live Wikipedia search, nothing persisted. | Wikipedia (no key required) | ✅ | ✅ |
| `link_wiki_page` | Link a page to a tracked company: fetches it now, saves as searchable clips, schedules daily change-checks. | Wikipedia (fetch) + OpenAI (embeddings) + Weaviate (index) | ✅ | — |
| `list_linked_wiki_pages` | List a company's linked pages and when each was last checked. | None | ✅ | — |
| `unlink_wiki_page` | Unlink a page -- stops daily checks, deletes its clips + vectors. | None | ✅ | — |
| `get_wiki_page_changes` | Paragraph-level diff between a linked page's two most recent snapshots. | Wikipedia (the snapshots being diffed were fetched from it earlier) | ✅ | — |

## Semantic search (Assistant only)

| Tool | Description | Provider | Assistant | AI agents |
|---|---|---|---|---|
| `search_research_clips`† | Semantic search over the user's own saved ResearchClips (pasted excerpts, ingested filings, linked wiki pages), optionally scoped to one ticker. | OpenAI (query embedding) + Weaviate (vector search) | ✅ | — |
| `search_media_transcripts`† | Semantic search over the user's uploaded audio/video transcripts, with speaker + timestamp per hit. Requires the optional `media` app to be enabled at all. | `ENABLE_MEDIA` + OpenAI + Weaviate | ✅ | — |

## Portfolio & trading (League only)

| Tool | Description | Provider | Assistant | AI agents |
|---|---|---|---|---|
| `get_portfolio` | Authoritative cash balance, open positions, account value. | None (internal ledger) | — | ✅ |
| `get_competition_standings` | Every competitor's rank, account value, and return -- same as the public scoreboard. No other competitor's holdings exposed. | None | — | ✅ |
| `submit_trade` | Submit a trade -- fills at the live ask/bid immediately, or queues if after-hours trading is restricted and the market's closed. | Steady or Tradier (the fill price) | — | ✅ |
| `set_price_targets` | Update take-profit/stop-loss levels on an already-open position, without a new trade. Purely informational -- nothing auto-closes. | None | — | ✅ |
| `cancel_trade` | Cancel one of the competitor's own still-QUEUED (after-hours) orders. | None | — | ✅ |

## Memory & scheduling (League only)

| Tool | Description | Provider | Assistant | AI agents |
|---|---|---|---|---|
| `list_ticker_memories` | List every ticker with a saved research note, with last-updated + length (not the text itself). | None | — | ✅ |
| `get_ticker_memories` | Pull saved research notes for specific tickers. | None | — | ✅ |
| `set_ticker_memories` | Save/overwrite research notes for one or more tickers (full replace, not append). | None | — | ✅ |
| `update_scratchpad` | Overwrite the competitor's persistent cross-ticker working notes (read in full at the start of every wake-up). | None | — | ✅ |
| `schedule_wakeup` | Schedule a future self-triggered wake-up (capped per day, min-spaced from other wake-ups/forced check-ins). | None | — | ✅ |

---

## Provider summary

Every external provider referenced above, and which tools depend on it:

| Provider | Env var(s) | Used by |
|---|---|---|
| **Steady** | `STEADY_API_TOKEN` | `get_stock_quote(s)`, `get_options_chain`, `get_technicals` (base fields), `get_market_news`, `get_reddit_sentiment`, `scan_subreddit_for_tickers`, `submit_trade` (fill price) -- whenever `PRICING_PROVIDER=steady` for the pricing-related ones |
| **Tradier** | `TRADIER_API_TOKEN`, `TRADIER_BASE_URL` | Same pricing tools as Steady when `PRICING_PROVIDER=tradier`, plus the Tradier-exclusive `get_price_history`, `get_intraday_candles`, and `get_technicals`' ATR/SMA/volume/high fields (no Steady equivalent exists) |
| **FMP** | `FMP_API_KEY` | `get_technicals`' `next_earnings_date`/`days_to_earnings` only -- independent of `PRICING_PROVIDER` |
| **Polygon** | `POLYGON_API_KEY` | `scan_down_streaks` only, via the nightly `ingest_daily_bars` Beat task + one-time `backfill_daily_bars` command populating `DailyBar` -- independent of `PRICING_PROVIDER` and every other provider above |
| **Brave Search** | `BRAVE_API_KEY` | `search_web` (both surfaces) |
| **OpenAI** | `OPENAI_API_KEY` | `scan_subreddit_for_tickers` (ticker extraction), `search_research_clips`/`search_media_transcripts`/`link_wiki_page`/`propose_filing_ingestion` (embeddings) |
| **Weaviate** | `WEAVIATE_URL` | `search_research_clips`, `search_media_transcripts`, and indexing for `link_wiki_page`/`propose_filing_ingestion` |
| **SEC EDGAR** | `SEC_CONTACT_EMAIL` | `list_filings`, `propose_filing_ingestion` |
| **Wikipedia** | none | `search_wikipedia`, `link_wiki_page`, `get_wiki_page_changes` |
| **Anthropic / OpenAI / Gemini** (native search) | `ANTHROPIC_API_KEY` / `OPENAI_API_KEY` / `GEMINI_API_KEY` (whichever the competitor's model needs) | League's native `web_search` only |

## Why the two lists differ

- **League-only**: everything trading/portfolio/memory-related
  (`get_portfolio` through `schedule_wakeup`), `get_stock_quotes` (batch --
  Assistant has no equivalent multi-ticker call), `get_market_news`, and
  the native provider `web_search`. None of these make sense for a human
  chat session.
- **Assistant-only**: subreddit watch-list management (`track_subreddit`
  and friends -- League has no ongoing per-user watch list), semantic
  search over the user's own saved material (`search_research_clips`,
  `search_media_transcripts`), filing ingestion (`propose_filing_ingestion`
  -- a confirmation-gated action that doesn't fit an unattended wake-up
  loop), and wiki page management (`link_wiki_page` and friends -- League's
  `search_wikipedia` is read-only, no linking).
- **Both**: core market data, filings/company reports, Reddit discovery,
  and (as of the `search_web` addition) generic web search.
