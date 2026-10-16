# WebSockets

How the frontend connects for real-time updates (e.g. a Celery screening task
reporting progress while it runs), and how backend code pushes messages to a
connected user. Complements `docs/api-endpoints.md` (HTTP) and
`docs/authentication.md` (the JWTs used here too).

## Why this exists

Some backend work (screening a ticker, and eventually AI processing --
article ingestion, text extraction, LLM calls) takes long enough that the
frontend shouldn't just block on an HTTP request waiting for it. Those jobs
run as Celery tasks; this is how a task -- running in a totally separate
worker process -- pushes updates to whichever browser tab is waiting on the
result.

## Connecting

```
ws://<host>/ws/user/?token=<access_token>
```

Same access token you already send as `Authorization: Bearer` on HTTP
requests -- WebSocket handshakes don't carry custom headers the way normal
requests do, so it travels as a query param instead. Get it the same way:
login, register, or google response body, or a fresh one from
`/api/auth/token/refresh/`.

The connection is rejected (close code `4001`) if the token is missing,
malformed, expired, or belongs to an inactive user. There's no separate
"auth ack" message -- if the connection stays open past the initial
handshake, you're authenticated.

**Token expiry while connected:** the access token is only checked at
connect time, not continuously. A 30-minute-old connection doesn't get
dropped just because the token that opened it has since expired -- but if
the socket disconnects (network blip, server restart) after that, reconnecting
will need a fresh token. Standard practice: reconnect using the auto-refresh
pattern from `authentication.md` if a reconnect attempt gets rejected.

There's a second, differently-scoped connection for the paper trading
league specifically -- `ws://<host>/ws/league/{competition_id}/?token=<access_token>`,
same auth mechanics, but competition-wide rather than per-user. See
`docs/league-api.md`'s "Live competition feed" section for the full
detail; everything else on this page describes the `/ws/user/` socket.

## What you receive

Plain JSON messages, shape depends on the event -- there's no fixed schema
across all message types, only a `type` field to distinguish them.

Every connection to `ws://<host>/ws/user/` only ever receives messages meant
for the logged-in user -- there's no subscribing to other channels/topics
from the client side on this socket. One exception, its own separate
connection: `ws://<host>/ws/league/{competition_id}/`, competition-scoped
rather than user-scoped -- see `league.*` below and
`docs/league-api.md`'s "Live competition feed" section for why that one's
different (anyone watching a competition sees every competitor's activity,
not just their own).

### `screen.*` (POST `/api/companies/{ticker}/screen/`, see `docs/api-endpoints.md`)

That endpoint validates the ticker and returns `202` immediately; these are
what actually deliver the outcome. Exactly one of `screen.complete` /
`screen.error` follows `screen.progress` for a given screen -- never both.

```json
{ "type": "screen.progress", "ticker": "RKLB", "status": "running" }
```
Sent once, right when the Celery task (`companies/tasks.py::screen_ticker`)
starts running. There's no percent -- a screen is a handful of EDGAR/FMP
calls that are usually cache hits and finish in well under a second, so a
fake granular progress bar isn't worth the complexity; this is just "it's
started" for a frontend that wants to show a spinner.

```json
{ "type": "screen.complete", "ticker": "RKLB", "report": { "...": "the full report, same shape as GET /api/companies/{ticker}/" } }
```
The finished report is embedded directly -- no need for the frontend to
issue a follow-up `GET`.

```json
{ "type": "screen.error", "ticker": "ZZZZ", "message": "Ticker 'ZZZZ' not found in SEC's company_tickers.json" }
```
Only for failures *inside* the task (this shouldn't normally happen, since
`ScreenView.post()` already validates the ticker before queuing it -- this
covers anything that changes between that check and the task actually
running).

### `assistant.*` (POST `/api/assistant/conversations/{id}/messages/`, see `docs/api-endpoints.md`)

Chat assistant. Conversation history is persisted (`Conversation`/`Message`
models), but the streamed reply still arrives here, not over the HTTP
response -- every message carries `conversation_id` so a frontend with more
than one conversation open can route it to the right thread. Exactly one of
`assistant.complete` / `assistant.error` follows a run of `assistant.token`
-- never both, and `assistant.error` can also arrive with no preceding
tokens at all (e.g. the model or API key is misconfigured, or the model's
context window is exceeded even after trimming history).

```json
{ "type": "assistant.token", "conversation_id": 4, "text": "The " }
```
One per streamed text chunk from the model, in order -- concatenate as they
arrive to render the response incrementally.

```json
{ "type": "assistant.complete", "conversation_id": 4, "message_id": 13,
  "text": "The full response text.", "model_id": "claude-sonnet-5",
  "input_tokens": 18019, "output_tokens": 777,
  "tool_calls": ["list_filings", "get_company_report"], "tool_use_output_tokens": 126,
  "cost_usd": "0.043808" }
```
The full response, same text the `assistant.token` chunks concatenate to --
included so the frontend doesn't have to trust its own concatenation was
lossless. `message_id` is the same row `GET .../messages/` returns, if the
frontend wants to reconcile rather than just append. `input_tokens`/
`output_tokens`/`cost_usd` are totals across every tool-calling round-trip
this turn took, not just the final one; `tool_calls` lists which tools
actually ran (`[]` if none), and `tool_use_output_tokens` is the slice of
`output_tokens` spent on those round-trips specifically -- subtract it from
`output_tokens` for what the final answer itself cost. Same fields, same
meaning, on the `GET .../messages/` row (`docs/api-endpoints.md`).

```json
{ "type": "assistant.error", "conversation_id": 4, "message": "Assistant isn't configured -- ANTHROPIC_API_KEY isn't set." }
```
Every failure -- config missing, bad model, an Anthropic API error, a
refusal, context still too big after trimming -- also gets its own
assistant-role row in `GET .../messages/` (its `text` is this same message),
not just this WebSocket push. That's deliberate: without a row there, the
next user turn would follow another user turn directly in history, and
Anthropic's API 400s on non-alternating roles.

The assistant has tools (`assistant/tools.py`) it can call mid-turn --
`list_filings`, `get_company_report`, `search_research_clips` run and
answer within the same turn (still just `assistant.token` /
`assistant.complete`, nothing new to handle). One tool,
`propose_filing_ingestion`, doesn't run immediately -- it fires the two
extra message types below instead.

```json
{ "type": "assistant.confirmation_required", "conversation_id": 4, "pending_action_id": 7,
  "description": "Ingest 10-Q filed 2026-05-12 for SLS into semantic search",
  "ticker": "SLS", "form": "10-Q", "filing_date": "2026-05-12" }
```
The assistant proposed a real action (currently just ingesting a filing --
a real fetch + parse + embed, not free or instant) and is waiting on the
user to approve or deny it via `POST /api/assistant/conversations/{id}/
pending-actions/{pending_action_id}/` (`docs/api-endpoints.md`) -- **not**
a WebSocket message back. This can arrive in the middle of an
`assistant.complete` for the same turn (the assistant's wrap-up text, e.g.
"I've proposed ingesting the 10-Q, confirm when ready", still comes through
normally) -- the two aren't mutually exclusive.

```json
{ "type": "assistant.action_complete", "conversation_id": 4, "pending_action_id": 7,
  "message_id": 22, "text": "Ingested 10-Q filed 2026-05-12 for SLS -- 24 clips added to semantic search." }
```
Sent once the confirmed action actually finishes running (approved) or
once a denial is recorded -- whichever the user chose on the confirm
endpoint. Not routed back through Claude: this is a plain Celery task's
result, same shape as `screen.complete`, not a continuation of the model
conversation. `message_id` is a normal assistant-role row, same as any
other turn -- `GET .../messages/` shows it inline with everything else.

### `media.*` (POST `/api/media/`, see `docs/api-endpoints.md`)

Upload processing (`media` app): audio extraction, diarized transcription,
embedding into semantic search. Starts automatically on upload -- no
confirmation step, since uploading the file is itself the user's
authorization. Exactly one of `media.complete` / `media.failed` follows a
run of `media.progress` -- never both.

```json
{ "type": "media.progress", "media_file_id": 3, "stage": "extracting_audio" }
```
`stage` is one of `downloading`, `extracting_audio` (video uploads only),
`transcribing` -- milestone markers, not a percentage. This can genuinely
take minutes for a long recording (several diarization API calls per
file), so these exist to show real progress rather than a stalled spinner,
without faking a granular percent the backend doesn't actually know.

```json
{ "type": "media.complete", "media_file_id": 3, "segment_count": 42, "duration_seconds": 1842.3 }
```
`segment_count` is how many searchable `MediaSegment` chunks were created
-- each one now findable via the assistant's `search_media_transcripts`
tool. `GET /api/media/{id}/` shows the same completed state (`status:
"ready"`) if a page reload misses this event.

```json
{ "type": "media.failed", "media_file_id": 3, "message": "ffmpeg not found on PATH or as .../ffmpeg.exe -- install it, or set FFMPEG_PATH in .env." }
```
Also reflected in `GET /api/media/{id}/` (`status: "failed"`,
`error_message` populated) -- not a WebSocket-only signal.

### `subreddit.*` (POST `/api/companies/subreddits/{id}/refresh/` or
`/api/companies/subreddits/refresh/`, see `docs/api-endpoints.md`)

On-demand re-scan of one (or, for the bulk endpoint, every) watched
subreddit, instead of waiting for the next twice-daily scheduled scan.
Both endpoints return `202` immediately; these are what actually deliver
the outcome, one full `progress` → `complete`/`error` sequence **per
subreddit** even from the bulk endpoint (not one combined event) -- a
slow or failing subreddit doesn't hold up the others' results. Exactly
one of `subreddit.complete` / `subreddit.error` follows a
`subreddit.progress` for a given `subreddit_id` -- never both.

```json
{ "type": "subreddit.progress", "subreddit_id": 4, "name": "wallstreetbets", "status": "running" }
```
Sent once, right when the Celery task (`companies/tasks.py::refresh_tracked_subreddit`)
starts running -- same "it's started" spinner signal as `screen.progress`
above, no percent for the same reason (a handful of Steady/OpenAI calls,
not a long-running job with meaningful sub-steps).

```json
{ "type": "subreddit.complete", "subreddit_id": 4, "name": "wallstreetbets",
  "tickers": ["SLS", "MRNA"], "new_mentions": 3 }
```
`tickers` is every ticker genuinely being discussed in this subreddit's
current hot posts (whether or not any of them were already seen in an
earlier scan); `new_mentions` is how many `SubredditMention` rows this
run actually created -- `0` is a normal, successful outcome (nothing new
since the last scan), not an error. `GET /api/companies/subreddit-mentions/`
reflects the same result afterward if a frontend would rather re-fetch
than read counts off this push.

```json
{ "type": "subreddit.error", "subreddit_id": 4, "name": "wallstreetbets", "message": "Steady is down" }
```
A Steady or OpenAI failure while scanning -- logged server-side with the
full traceback; this push is just the user-facing signal, same convention
as `screen.error`/`media.failed` above.

### `league.*` (see `docs/league-api.md`, not this doc, for full payloads)

Three different delivery shapes under this one prefix, all documented
alongside the rest of that app's concepts instead of here:
- A wake-up starting (`league.wakeup_started`) -- pushed directly to the
  owning user (`send_to_user`, not wrapped in `notification.new`), same
  fire-and-forget shape as `screen.progress` above: sent once, right when
  `league.tasks.run_agent_wakeup` actually starts running (past its
  not-scheduled/paused/over-budget early exits), so a frontend wake-up
  button can flip from "queued" to "thinking" instead of sitting on the
  bare `202` from `POST .../wakeups/` until completion. A missed one just
  means the button stays disabled a little longer -- not worth persisting.
  ```json
  { "type": "league.wakeup_started", "competitor_id": 4, "wakeup_id": 91 }
  ```
- AI wake-up outcomes (`league.trade_activity` / `league.wakeup_complete`
  / `league.wakeup_failed`) -- delivered as `notification.new` pushes to
  the owning user, exactly like `wiki.page_changed` below. Exactly one of
  these three follows a `league.wakeup_started` for a given `wakeup_id` --
  same "starts, then exactly one terminal event" shape as `screen.*` and
  `media.*` above, just split across an unwrapped push and a
  `notification.new` envelope instead of all three being the same shape.
- Live trade fills (`league.trade`) and manual quote-refresh results
  (`league.competitor_refreshed` / `league.scoreboard_refreshed`) --
  broadcast unwrapped (no `notification.new` envelope) to every socket
  watching `ws://<host>/ws/league/{competition_id}/`, competition-wide,
  not per-owner.

### `notification.*` (see `docs/api-endpoints.md` for the REST side)

Durable notifications (`notifications` app) -- unlike every event type
above, these are **not** fire-and-forget. `notifications/services.py::notify()`
persists a `Notification` row before pushing it live, so if you're not
connected when something happens, it isn't lost: every unacknowledged
notification is redelivered, oldest first, the moment you connect (or
reconnect) -- no separate "catch me up" request needed, it just arrives
right after the connection opens.

```json
{ "type": "notification.new", "id": 7, "notification_type": "wiki.page_changed",
  "title": "RKLB: \"Rocket Lab\" changed on Wikipedia",
  "body": "Content changed -- use get_wiki_page_changes to see what.",
  "payload": { "ticker": "RKLB", "wiki_source_id": 3, "page_title": "Rocket Lab" },
  "created_at": "2026-07-28T22:00:03Z" }
```
Identical shape whether it arrived as a live push or as part of the
reconnect backlog -- a frontend never has to special-case which path
delivered it. Acknowledge it with `POST /api/notifications/{id}/ack/`
(`docs/api-endpoints.md`) once the user's seen it; an unacknowledged
notification keeps being redelivered on every future connect until then.
`notification_type` is dot-namespaced like every other event family here --
`wiki.page_changed` is the only producer today, from the daily wiki-poll
Celery Beat job (`companies/tasks.py::poll_wiki_sources`).

## Reconnection

No built-in heartbeat or auto-reconnect on the server side -- that's on the
frontend. A dropped connection (tab backgrounded, network change, server
deploy) just closes; reconnect with the current access token when the app
notices, e.g. via the browser's `WebSocket.onclose` handler with backoff.

## For backend work (adding a new task that pushes updates)

```python
from realtime.utils import send_to_user

send_to_user(user.id, "screen.progress", ticker="RKLB", pct=50)
```

This is safe to call from anywhere synchronous -- a Celery task, a Django
view -- and reaches every open connection for that user (all their open
tabs), regardless of which server process is holding the socket. Under the
hood: it's a Redis pub/sub message (via `channels-redis`, the "channel
layer"), which is *not* the same Redis usage as Celery's task queue -- same
Redis instance, different mechanism, no interference between the two.

For anything that should survive the user being offline right now (not just
a transient progress ping), use `notifications.services.notify()` instead:

```python
from notifications.services import notify

notify(user, "wiki.page_changed", title="...", body="...", payload={"ticker": "RKLB"})
```

It wraps `send_to_user()` with persistence + reconnect redelivery (see
`notification.*` above), at the cost of one DB write per call. Reach for
`send_to_user()` directly when a missed message just means the frontend
re-fetches state on its own (`screen.*`, `assistant.token`, `media.progress`)
-- reach for `notify()` when a missed message means something genuinely
goes unseen.

## Known issue: don't let `redis` drift past 4.x

`requirements.txt` pins `redis==4.6.0`, below what `pip install -U` would
otherwise grab (`channels-redis` declares `redis>=4.6` with no upper bound).
Newer `redis` (5.x/6.x/8.x seen in practice) changed the async client's
timeout/retry behavior in a way `channels-redis 4.3.0`'s internal `BRPOP`
long-polling loop doesn't handle -- every connection would accept and open
fine, then die a few seconds later (once idle, mid-poll-cycle) with an
uncaught `redis.exceptions.TimeoutError`, and the frontend's reconnect would
walk straight back into the same crash. If this pin ever needs to move
(upgrading `channels-redis` too, most likely), verify by holding a
connection open and *idle* for 15+ seconds before declaring it fixed --
sending a message immediately after connecting doesn't exercise the crash
path at all.
