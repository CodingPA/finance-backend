# Market Research

Django API backend for a personal stock research app ("The Lab"). Pulls SEC
EDGAR + FMP data (the `companies` app) and screens it per the process in
`docs/overview.md`.

> **Not investment advice. Paper trading only.**
> This is a personal research tool. The paper trading league simulates trades
> against live market prices -- it places **no real orders**, connects to no
> brokerage execution venue, and holds no real positions or money. Nothing
> produced by this software or its AI agents is investment advice or a
> recommendation to buy or sell any security. It ships with no warranty; see
> [LICENSE](LICENSE). Use at your own risk.

## ⚠️ Crucial Security & ToS Compliance Notice

This platform is engineered strictly as a **Single-Tenant / Private Research Tool**. 

If you choose to self-host this application, you must adhere to the following rules to protect your API keys and remain compliant with data provider terms:

* **DO NOT Open Public Registration:** The admin panel requires manual email whitelisting for a reason. Keep registration restricted exclusively to your own account.
* **Data Redistribution Risk:** If you connect a live Tradier API token and allow unauthenticated or public users to view saved market data, charts, or AI agent price logs, **you are violating Tradier's Terms of Service regarding data redistribution.**
* **API Key Exposure:** Opening this application to the public with your personal Tradier keys active exposes your account to unauthorized use, rate-limiting blocks, or potential financial liabilities if live endpoints are targeted.

**Run this locally or on a tightly secured, private network only.**

## Setup

```
python -m venv .venv
.venv/Scripts/pip install -r requirements.txt
cp .env.example .env   # fill in real values
python manage.py migrate
.venv/Scripts/uvicorn core.asgi:application --reload --port 8001
```

`requirements.txt` is the slim base -- everything works on it except two
opt-in features, each with its own extras file installed alongside it
(`pip install -r requirements.txt -r requirements-filings.txt`, not
instead of it):

- `requirements-filings.txt` -- SEC filing ingestion. Pulls in
  `docling-slim`'s full torch stack (~1.2GB), which is why it's not in
  the base file. Skip it and everything else still works; you just can't
  ingest a filing.
- `requirements-media.txt` -- the media app (mp3/mp4 upload, diarized
  transcription). Also needs `ENABLE_MEDIA=true` (below) and the
  `ffmpeg`/`ffprobe` binaries on `PATH`.

Runs via Uvicorn (ASGI), not `manage.py runserver` -- WebSockets (see
`docs/websockets.md`) need a real ASGI server, `runserver` alone doesn't
serve them properly once Channels is in the mix. `manage.py` still works
fine for everything else (`migrate`, `shell`, `makemigrations`, etc.).

## Running locally

Three processes, all needed -- the API server, the task worker, and the
scheduler are separate. Run each in its own terminal:

```
.venv/Scripts/uvicorn core.asgi:application --reload --port 8001
.venv/Scripts/celery.exe -A core worker --pool=solo --loglevel=info
.venv/Scripts/celery.exe -A core beat --loglevel=info
```

`beat` is only needed for the daily scheduled jobs (`CELERY_BEAT_SCHEDULE`
in `core/settings.py`: price refresh and the Wikipedia change-check) --
screening and everything request-triggered works fine without it, but
nothing will run on the schedule until it's up.

## Docker quickstart

An alternative to the two sections above -- one command instead of a venv,
Postgres, and Redis set up by hand:

```
cp .env.example .env   # fill in whatever API keys you actually have
docker compose up --build
```

Starts Django (Uvicorn, `:8001`), a Celery worker, Celery beat, Postgres,
and Redis, all containerized. Migrations run automatically on startup.
This is a quickstart for evaluating or developing the project, not a
production topology -- there's no nginx or TLS here, and Weaviate is left
out entirely (it's optional infra even natively). Production deployment
is documented separately in `docs/deployment.md` and doesn't use any of
these files.

SEC filing ingestion and the media app are left out of the image by
default, same reasoning as `requirements.txt`'s own split -- filing
ingestion alone would pull in ~1.2GB of torch. Build with either (or both)
by setting the matching shell variable before `up` -- these are build
args, read at image-build time, so they go before the command, not in
`.env`:

```
INSTALL_FILINGS=true INSTALL_MEDIA=true docker compose up --build
```

(and set `ENABLE_MEDIA=true` in `.env` too, if you built with `INSTALL_MEDIA=true` -- see below.)

## First login

Both setup paths above get you a running server, but signing in needs one
more step: new accounts are gated by a sign-up allowlist
(`accounts.AllowedEmail`) that starts out **empty** -- nobody can register
or sign in with Google until their email has a row there. Bootstrap your
own access:

1. Create a superuser -- the one path that bypasses the allowlist
   entirely, since it's a management command, not the sign-up API:

   ```
   # Native
   python manage.py createsuperuser

   # Docker (run in another terminal while `docker compose up` is running)
   docker compose exec web python manage.py createsuperuser
   ```

   Prompts for email and password interactively either way -- this
   project's `User` model has no separate username field, so that's all
   it asks for.
2. Log into `/admin/` (e.g. `http://localhost:8001/admin/`) with that
   superuser account.
3. Under **Accounts -> Allowed emails**, add whichever email address you
   actually want to sign up with (the same one, or a different one).
4. Sign up normally through the frontend, or directly --
   `POST /api/auth/register/` with `{email, password, name}` (see
   `docs/api-endpoints.md`).

Until step 3 is done for a given address, registering fails with
`"This email isn't on the sign-up allowlist."` (Google sign-in:
`"This email isn't authorized to sign up."`). This only gates *creating*
a new account -- once you're signed up, removing the `AllowedEmail` row
later never locks you out (see that model's own docstring,
`accounts/models.py`).

## Environment variables

See `.env.example` for the full list. Notably:

- `FMP_API_KEY` -- free API key from [Financial Modeling Prep](https://site.financialmodelingprep.com/), used for price and shares-outstanding estimates (not available from SEC EDGAR, which only has filed facts). Without it, price/shares-estimate fetching is skipped; everything else still works.
- `SEC_CONTACT_EMAIL` -- required for any SEC EDGAR request. SEC 403s every request without a contact email in the User-Agent header.
- `DATABASE_URL` -- unset for local SQLite; set to a Postgres URL in deployment. No code change needed either way.
- `GOOGLE_OAUTH_CLIENT_ID` -- required for Google sign-in (`/api/auth/google/`) to work.
- `REDIS_HOST` / `REDIS_PORT` / `REDIS_USERNAME` / `REDIS_PASSWORD` / `REDIS_DB` / `REDIS_TIMEOUT` / `REDIS_USE_TLS` -- Celery's broker + result backend (Redis Cloud in this project). `REDIS_USE_TLS` defaults to `true`; set `false` if your Redis database doesn't have TLS enabled (symptom of a mismatch: `SSL: WRONG_VERSION_NUMBER`).
- `WEAVIATE_URL` / `WEAVIATE_GRPC_HOST` / `WEAVIATE_API_KEY` -- semantic search infra (`companies/services/vector_store.py`). Shared Weaviate server across other apps, no vectorizer module enabled -- vectors are computed here, not by Weaviate. Run `python manage.py init_weaviate` once (idempotent) to create the collection before anything can be indexed.
- `WEAVIATE_ENVIRONMENT` -- namespaces the collection name (`FinanceApp_{env}_ResearchClip`); defaults to `Dev`. This app doesn't have a prod Weaviate instance yet.
- `OPENAI_API_KEY` -- embeddings for the above (`text-embedding-3-large`). Without it, clip indexing silently no-ops -- everything else, including clip creation itself, still works.
- `ENABLE_LEAGUE` -- off by default. Turns on the paper trading league's Celery beat schedule (forced check-ins, quote refresh, weekly marks, expired-option settlement, overdue-wakeup recovery). The `league` app and its API are always available regardless of this flag -- it only controls whether Celery beat runs the league's background jobs. Leave it off until you actually want a competition running: `league-forced-checkins` alone ticks every 5 minutes and dispatches a real, billed LLM call whenever one is due.
- `ENABLE_MEDIA` -- off by default. Turns on the `media` app (mp3/mp4 upload, diarized transcription) -- without it, the app isn't installed at all: no migrations, no `/api/media/` routes, no `search_media_transcripts` tool. It's the only feature that needs a real system binary (`ffmpeg`/`ffprobe`) outside the Python dependency tree, which is the main reason it's opt-in rather than always on.

## Data sources and their terms

This project is a **client** for third-party financial data services. It ships
no market data, no filings and no credentials -- you bring your own keys, and
each request goes to the provider under your account, subject to their terms.

The Apache-2.0 licence covers this source code only. It grants no rights to
any data you retrieve through it. Several of these providers restrict
redistribution or commercial use; read the terms for the ones you enable.

| Source | Used for | What to know |
|---|---|---|
| [SEC EDGAR](https://www.sec.gov/os/accessing-edgar-data) | Filings, company facts | Filings are public domain, but access is not unconditional: SEC requires a contact email in the `User-Agent` on every request and enforces a fair-access rate limit. Set `SEC_CONTACT_EMAIL` to your real address -- the code deliberately refuses to send a placeholder. |
| [Tradier](https://tradier.com/products/market-data) | Quotes, option chains, intraday bars | Market data is licensed under Tradier's own agreement and **redistribution is restricted**. A production token needs a funded brokerage account or their market-data add-on; sandbox tokens serve delayed data. |
| SteadyAPI | Option chains, news, Reddit | Redistribution governed by your SteadyAPI plan. |
| [Financial Modeling Prep](https://site.financialmodelingprep.com/developer/docs) | Prices, shares outstanding, sector | The free tier **restricts commercial use and redistribution**. Check your plan before building anything public on top of it. |
| [Polygon](https://polygon.io/legal/terms) | Daily bar history for the down-streak screener | Market data is licensed under Polygon's own terms and plan; **redistribution is restricted**. Check what your plan permits before exposing bars or screener output publicly. |
| [Brave Search API](https://api-dashboard.search.brave.com/terms-of-service) | Open-web search tool for the assistant and league agents | Governed by Brave's API terms, which limit how results may be stored, cached and reused (including with AI models) depending on your plan. |
| [Wikipedia](https://foundation.wikimedia.org/wiki/Policy:Terms_of_Use) | Company background | Article text is CC BY-SA 4.0 -- attribution and share-alike apply to anything you republish from it. |
| Reddit (via SteadyAPI) | Ticker sentiment | User-generated content, subject to Reddit's terms. |
| Anthropic / OpenAI / Google | Assistant and league agents | Usage is billed to your account. See "Costs" below. |

### Costs

Two things bill real money as soon as they are switched on, so they are worth
naming explicitly:

- **The assistant and the league agents make paid LLM calls.** Every wake-up,
  forced check-in and daily brief is an inference request against your own API
  key.
- **Celery beat runs the league on a schedule.** With beat running, forced
  check-ins dispatch on a recurring basis whether or not anyone is watching.
  Do not start beat unless you intend to pay for that.

### Not investment advice

**This software is for research and education only. It is not investment
advice.**

The paper trading league simulates trades against live market prices, but it
places **no real orders**, connects to no brokerage execution venue, and holds
no real positions or money. No trade it records has any effect outside this
application's own database.

Nothing produced by this software -- screening output, reports, agent
reasoning, or any AI competitor's stated rationale -- is investment advice, a
recommendation to buy or sell any security, or a solicitation. The AI agents
are language models: they are wrong routinely, and their output is not
research you should act on. Market data may be delayed, incomplete, or
incorrect.

The software is provided "as is", without warranty of any kind, as set out in
[LICENSE](LICENSE). You are solely responsible for any decision you make, and
for complying with the terms of every data provider you connect it to.

## Background jobs (Celery)

```
.venv/Scripts/celery.exe -A core worker --pool=solo --loglevel=info
```

`--pool=solo` is required on Windows (the default prefork pool needs `fork()`,
which Windows doesn't have). Tasks live in each app's `tasks.py` (e.g.
`companies/tasks.py`) and are auto-discovered at worker startup. Used today
for background screening jobs and the daily price refresh; will also carry
AI processing (article ingestion, text extraction, LLM calls) as those get
built.

## Deployment

Production runs Uvicorn behind nginx (WSGI/gunicorn alone can't serve the
WebSocket endpoints), a Celery worker, and Celery beat as three separate
systemd services on an Ubuntu server that also hosts another site --
Postgres/Redis/Weaviate are shared instances, namespaced per-app (a
separate database, a separate `REDIS_DB` number, a separate Weaviate
collection prefix) rather than installed fresh. Full one-time setup
walkthrough, systemd unit files, and the nginx config live in
`docs/deployment.md` / `deploy/`; once set up, shipping new code is just
`deploy/deploy.sh` (pulls `main`, reinstalls deps, migrates, restarts
services).

## API

Auth (`/api/auth/`), all JSON:

| Endpoint | Method | Auth | Notes |
|---|---|---|---|
| `register/` | POST | none | `{email, password, name}` -> `{access, refresh, user}`. No email verification required yet. |
| `login/` | POST | none | `{email, password}` -> `{access, refresh, user}` |
| `google/` | POST | none | `{id_token}` (from Google Identity Services on the frontend) -> `{access, refresh, user}`. Finds or creates the user. |
| `token/refresh/` | POST | none | `{refresh}` -> `{access, refresh}`. Refresh tokens rotate and the old one is blacklisted. |
| `logout/` | POST | Bearer access token | `{refresh}` -> blacklists it |
| `me/` | GET | Bearer access token | current user's `{id, email, name, initials, date_joined}` |

User IDs are UUIDs, not sequential integers. Email/password and Google sign-in
both land on the same `accounts.User` model; a Google login links to an
existing email/password account automatically if the email matches.

Companies (`/api/companies/`):

| Endpoint | Method | Auth | Notes |
|---|---|---|---|
| `` | GET | Bearer access token | The user's dashboard -- every tracked company (system defaults auto-provisioned on first load + anything user-added), one summary row each. |
| `` | POST | Bearer access token | `{ticker}` -> add to the user's dashboard. Doesn't screen it -- that's a separate action. Idempotent. |
| `{ticker}/status/` | PATCH | Bearer access token | `{status}` (`pending`/`shortlisted`/`killed`) -- this user's own verdict on a tracked ticker. |
| `{ticker}/screen/` | POST | Bearer access token | Queues a Celery task to fetch fresh EDGAR/FMP data (creates the `Company` on first sight); returns `202` immediately, result arrives over WebSocket (`screen.progress`/`screen.complete`/`screen.error`). |
| `{ticker}/` | GET | Bearer access token | Latest cached full report, no fetch. `404` if never screened. |
| `{ticker}/price-history/` | GET | Bearer access token | Full daily EOD price series, fetch-or-cache-through FMP (not part of the report). |

See `docs/api-endpoints.md` for the full report/summary shapes
(per-fiscal-year financials, derived margins/FCF/growth/P-S/runway, shares,
price, the tripwire).

## Real-time (WebSockets)

`ws://<host>/ws/user/?token=<access_token>` -- one connection per tab, joined
to a group keyed on the user's ID, used for Celery tasks (screening
progress, eventually AI processing) to push updates without the frontend
polling. See `docs/websockets.md` for the full protocol and message shapes.

## Monitoring on a server

With the systemd units from `deploy/` (see `docs/deployment.md`):

```
# Last 25 lines from the Celery worker
journalctl -u finance-celery-worker -n 25

# Follow it live, like tail -f
journalctl -u finance-celery-worker -n 25 -f

# Worker and beat together, interleaved by timestamp
journalctl -u finance-celery-worker -u finance-celery-beat -n 25 -f

# Add the API too, e.g. to line a wake-up up with an incoming request
journalctl -u finance-api -u finance-celery-worker -u finance-celery-beat -n 25 -f
```

Ctrl+C exits follow mode without stopping the service -- this only tails
logs, it doesn't touch the process.

## Licence

Copyright 2026 George Cann, CodingPA LLC.

Licensed under the Apache License, Version 2.0. See [LICENSE](LICENSE) for the
full text and [NOTICE](NOTICE) for the attribution notice that travels with
redistributions.

Apache-2.0 was chosen over MIT for its explicit patent grant and for making
contributions inbound-licensed on the same terms without a separate CLA. Note
again that it covers this code and not any provider's data -- see "Data sources
and their terms" above.
