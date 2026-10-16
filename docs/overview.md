# The Lab — Repeatable Stock Research Process

> **Not investment advice.** This describes a personal research process and
> the tooling built around it. It is for research and education only. Nothing
> here is investment advice or a recommendation to buy or sell any security,
> and no output of this software should be relied on for a financial decision.
> Use at your own risk.

**The system in one line:** the script screens everything; you only read filings for the few that survive.

Three modes. Never mix them — mixing them is what today's goose chase was.

| Mode | Who does it | Companies | Time each |
|---|---|---|---|
| 1. Screen | Script | All (20–30) | ~1 min |
| 2. Rank | You, Summary tab only | All | ~30 sec |
| 3. Deep dive | You, filings open | 2–3 survivors | Hours (fine) |

---

## Mode 1 — Screen (automated)

Run: `python manage.py screen TICKER`, or `POST /api/companies/TICKER/screen/`
from the frontend.

Everything the screen needs, and where it comes from:

| Field | Source | Notes |
|---|---|---|
| Revenue (3 yr) | EDGAR `RevenueFromContractWithCustomerExcludingAssessedTax` | Annual filter can leak quarterly dates — eyeball the period dates |
| Gross profit | EDGAR `GrossProfit` | Some companies don't tag it → compute Rev − CoR |
| Net loss | EDGAR `NetIncomeLoss` | Enter as negative |
| Operating cash flow | EDGAR `NetCashProvidedByUsedInOperatingActivities` | Usually negative for these companies |
| Capex | EDGAR `PaymentsToAcquirePropertyPlantAndEquipment` | Enter positive (sheet convention) |
| Stock-based comp | EDGAR `ShareBasedCompensation` | Dilution engine — watch as % of revenue |
| Cash (+ ST investments) | EDGAR `CashAndCashEquivalentsAtCarryingValue` + current marketable securities | |
| Total debt | EDGAR debt tags, current + noncurrent summed | |
| Shares — cover page | EDGAR `dei:EntityCommonStockSharesOutstanding` | Filing-date count → use for market cap |
| Shares — balance sheet | EDGAR `us-gaap:CommonStockSharesOutstanding` | Year-end count → use for the trend row |
| Price + market cap | FMP `/stable/profile` | Price's date = as-of date, not today |
| Shares tracker | FMP `/stable/shares-float` | SEC-derived; staleness check only, not independent truth |
| Tripwire | price × shares vs FMP's own marketCap | Warn if drift > 1% |

Known edge cases (already tested): multi-class share structures (ASTS) can have **no** EDGAR share tags — that missing tag is itself a complexity flag, not a bug. Some small caps (LUNR) have stale/spotty dei entries. Delisted tickers fail clean.

---

## Mode 2 — Rank (30 seconds per company, Summary tab only)

Six questions, all answered by fields the script fetched. **No filings open in this mode.**

1. **Growing?** — Revenue YoY, both years. Direction matters more than level.
2. **Margin trajectory?** — Gross margin % across 3 years. Improving = scaling works.
3. **Burning how much?** — FCF (OCF − capex). Size the hole.
4. **Can it survive?** — Runway = (cash + ST inv) ÷ annual FCF burn. Under ~1.5 yr means a raise is coming (dilution or debt).
5. **Diluting how fast?** — Share count growth over 3 yrs + SBC as % of revenue.
6. **What am I paying?** — P/S trailing, judged against the growth rate.

Kill 80–90% here. "Not interesting enough to read about" is the default verdict. Shortlist survivors for Mode 3.

---

## Mode 3 — Deep dive (survivors only)

This is where the manual work lives — and only here.

### The six questions (what actually changes the verdict)

The screen already answered "is this worth reading about." These six are
what the screen *can't* fetch, roughly in the order that changes your mind
fastest. Everything else in a 10-K is context.

1. **Revenue quality/durability** — backlog and book-to-bill. Is the growth
   already contracted, or does it depend on winning deals that don't exist
   yet? RKLB's $2.2B backlog vs. $600M revenue is the bull case in one
   ratio.
2. **Path to profitability** — not *whether* they're losing money (the
   screen already shows that), but *why*, and whether it's narrowing
   structurally. Is gross margin trending toward a level where scale
   eventually covers opex? Or is opex growing as fast as gross profit
   (RKLB's problem — margins climbing but net loss flat because R&D eats it
   all)?
3. **The one falsifiable catalyst** — the dated event that proves or breaks
   the thesis. Neutron's Q4 launch, for RKLB. A binary you can put on the
   calendar and check.
4. **Customer/revenue concentration** — how much rides on one customer or
   one contract. Buried in risk factors as a % figure. Determines how
   fragile the whole story is.
5. **Dilution trajectory** — the screen has share count; this is *why* it's
   growing and whether it's about to accelerate (ATM offering capacity,
   convertibles that haven't converted yet).
6. **Insider behavior** — Form 4s. Are insiders buying, or is the sponsor
   dumping?

Five of these six are things the screen structurally can't fetch — that's
exactly why they're the deep-dive payload, not screen fields.

**In the app:** each survivor's deep dive is a set of `ResearchClip` rows
(`POST /api/companies/{ticker}/clips/`, see `docs/api-endpoints.md`) — one
paste per finding, tagged with which of the six it answers (or `context`
for background material that doesn't map to one of them: wiki articles,
launch cadence, general operational KPIs). Paste-in only, deliberately —
the whole point of these six is that nothing fetches them automatically.
`GET /api/companies/{ticker}/` returns `filings` — direct EDGAR links to
every cached 10-K/10-Q/20-F, newest first — so the frontend can put "open
the actual filing" one click away instead of the user hunting for it on EDGAR.

One deliberate exception to "paste-in only": Wikipedia background material
(`context` clips tagged `source_type: "wiki"`) can now be *linked* instead
of pasted — `POST /api/companies/{ticker}/wiki/` or the assistant's
`link_wiki_page` tool fetches the page, keeps it searchable, and rechecks
it daily for edits. This is still narrow: it's
one specific, structured source (a named Wikipedia article), not a general
auto-fetch — the six real deep-dive questions above still have nothing that
fetches them automatically, and won't, per the goose-chase rules below.

The map of where everything in a 10-K/10-Q lives, for the filing-sourced
categories above (mainly #1, #2, #4, #5):

**In the 10-K:**
- **Cover page** — shares outstanding as of filing date (market-cap input)
- **Item 1** — what they sell, segments, acquisition history, who they name as competitors
- **Item 1A** — risk factors. Don't read linearly: Ctrl+F the product names and `%`. Hunt dated, weirdly specific confessions.
- **Item 7 (MD&A)** — segment revenue (often only as YoY comparisons across two sections — back out the third year by subtraction), backlog, liquidity, cost-per-unit KPIs
- **F-5** balance sheet → cash, debt (= convertible notes + long-term borrowings + any current installments)
- **F-6** income statement → revenue, gross profit, net loss
- **F-7** equity statement → SBC, year-end share counts, what caused dilution (ATM offerings, note conversions, acquisitions)
- **F-8** cash flow → OCF, capex, SBC cross-check

**NOT in SEC filings (stop looking there):**
- **Guidance** → earnings press release / call, on the IR site. Search: `TICKER quarterly results guidance`. Many companies guide **one quarter only** — annualize the midpoint (×4) and label it "est." in the source column.
- **Operational KPIs** (launches, satellites, deliveries) → company announcements + public records. Externally verifiable = their value.
- **Insider activity** → Form 4s on EDGAR / openinsider.com. 10b5-1 plans appear in 10-Q Item 5.
- **Analyst/sentiment** → last, only to learn what's already priced in.

**Units rule:** F-pages are in **thousands**. Divide by 1,000 on entry. Sheet is in millions, always.

**Deliverable per deep dive (Journal tab):** three sentences — what they sell / why they win / what kills them — plus one **falsifiable checkpoint with a date** (next earnings, a launch, a guidance number to hit). `GET`/`PATCH /api/companies/{ticker}/journal/` — one row per tracked company, fetched-or-created blank on first `GET`.

---

## Per-ticker workflow and sheet template — pending revision

The two sections below describe the original manual/spreadsheet-copy-paste
workflow (STONKS.xlsx, since removed). The *fields* and *reasoning* still
hold (screen field set, the six ranking questions, what's excluded and
why); the *mechanics* ("new tab, copy template, blue font = hardcoded")
don't apply anymore now that Mode 1 is an API call, not a spreadsheet
fill-in. These get rewritten once the dashboard exists to describe the
real workflow instead of a mix of old and new.

## Per-ticker workflow (literal steps) — describes the old spreadsheet process

1. Run the script.
2. New company tab (copy template) → type the fetched numbers in. Blue font = hardcoded, black = formula. Source note per cell.
3. Price + as-of date from FMP output.
4. Glance the tripwire and the units (a 1,000× error looks almost plausible — check one number against the filing if anything feels off).
5. Answer the six questions. Verdict: **kill** or **shortlist**.
6. Shortlisted → Mode 3 when you have an evening. Everything else → done, next ticker.

30 companies ≈ 30 script runs + 15 minutes of ranking + 2–3 evenings of real reading.

---

## Sheet template — screen fields only — describes the old spreadsheet process

Keep: shares (cover + trend), price, as-of, market cap, revenue, gross profit, gross margin %, net loss, OCF, capex, FCF, SBC, cash + ST inv, total debt, revenue growth %, P/S trailing, runway.

**Removed from template** (deep-dive notes now, not screen rows): segment revenue split, backlog, P/S forward, company-specific KPIs. Reason: they don't exist uniformly — every company reports them differently or not at all, so they can't be screened, only researched.

This exact field list is still authoritative for what `run_screen()` fetches
(`companies/services/screening.py`) and what `build_report()` returns
(`companies/services/presentation.py`) — only the delivery mechanism (API
response, not spreadsheet cells) has changed.

---

## Goose-chase rules (learned the hard way, 7/19/26)

1. If a field isn't uniform across all companies, it does not belong in the screen.
2. Guidance is never in the 10-K/10-Q. Press releases only. Quarterly-only guidance → annualize and label.
3. Segment tables often exist only as YoY deltas — subtract or skip; don't hunt for a table that isn't there.
4. A missing EDGAR tag is information (weird capital structure), not a failure.
5. Hours on one company is correct ×3. It is insanity ×30. The mode determines the effort.

## Tool backlog (optional, later)

- Script writes directly into the workbook (openpyxl is already in requirements.txt) — kills the hand-typing step
- Tighten the annual-period filter (known quarterly-date leak)
- Auto-fetch the full tag set for any new ticker in one command