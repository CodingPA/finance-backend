# Deployment

> **Not investment advice.** This deploys a personal research tool and a
> paper trading simulation -- it places no real orders and holds no real
> money. For research and education only.

> **⚠️ Single-tenant only -- read this before putting it on a public domain.**
> Everything below walks through giving this app a real public hostname
> behind nginx. That's fine -- a reachable server is not the same as an
> open one -- but it only stays fine if sign-up NEVER opens beyond you.
> `accounts.AllowedEmail` starts empty and gates every new account (see
> README.md's "First login"); leave it that way. A production
> `TRADIER_API_TOKEN` is tied to your own funded brokerage account and its
> data redistribution is restricted by Tradier's terms -- someone else
> signing in and pulling quotes through your key is exactly the
> redistribution their agreement forbids, on top of burning your own rate
> limit. See README.md's "Crucial Security & ToS Compliance Notice" and
> "Data sources and their terms" for the full rundown. Don't advertise
> this URL, don't add rows to `AllowedEmail` for anyone but yourself (and
> anyone you've separately confirmed can use your keys), and don't wire
> up self-service registration.

Production target: an Ubuntu server, adding this app on `api.example.com`
(API) with a separately-deployed frontend on `app.example.com` (not this
repo). Written against, and tested on, a box that also hosts an unrelated
site — every step below is scoped to avoid touching anything that other
site already owns, but nothing here assumes you're in that situation; on
a fresh box, just skip the "existing" framing (a fresh Postgres, a fresh
Redis, DB `0` is as unused as any other number, etc).

**If this server does host something else: every step below is scoped to
avoid touching it** — a new database and user on the existing Postgres, a
new (unused) DB number on the existing Redis, a new collection namespace
on the existing Weaviate (if you're running one via Docker), and one new
nginx server-block *file* that never edits the other site's own config.
Read a step fully before running it; nothing here is meant to run
unattended on first use.

Files referenced below live in `deploy/`: `deploy.sh`,
`finance-api.service`, `finance-celery-worker.service`,
`finance-celery-beat.service`, `nginx-finance-api.conf`.

## Architecture

```
nginx (api.example.com, :443)
  -> Uvicorn (127.0.0.1:8001, 2 workers, ASGI -- serves HTTP + WebSocket)
       -> Postgres (existing instance, new "finance_backend" database)
       -> Redis   (existing instance, new REDIS_DB number -- Celery broker/
                    result backend AND the Channels layer)
       -> Weaviate (existing Docker instance, new WEAVIATE_ENVIRONMENT
                     namespace -- semantic search only, optional)
Celery worker (--autoscale=8,1 -- scales 1-8 processes with load)
Celery beat   (CELERY_BEAT_SCHEDULE: price refresh, wiki polling, league's
               forced check-ins/weekly marks)
```

Uvicorn runs multiple `--workers` safely here specifically because the
WebSocket layer is Redis-backed (`CHANNEL_LAYERS` in `core/settings.py`,
`channels_redis`) — a message from any worker process reaches every
connected client regardless of which worker holds their socket. Without
that, multi-worker ASGI would silently drop cross-worker WebSocket
messages.

## 1. One-time system setup

Run as a dedicated non-root user — `deployer` below is just an example
name; reuse whatever deploy account this box already has, or create one.
Substitute your actual username and path everywhere `deployer` and
`/home/deployer/finance-backend` appear below.

```bash
git clone <this repo's URL> /home/deployer/finance-backend
cd /home/deployer/finance-backend
python3 -m venv .venv
.venv/bin/pip install --upgrade pip
```
`requirements.txt` is the slim base -- everything runs on it except SEC
filing ingestion and the media app, which are optional extras
(`requirements-filings.txt`, `requirements-media.txt`) so a deployment
that wants neither isn't forced to install docling's ~1.2GB torch stack.
Skip either file below if you don't need that feature; both apply
independently of each other.
```bash
# --- Only if you want filing ingestion (requirements-filings.txt): ---
# torch (a transitive docling-slim dependency) comes from PyPI's default
# index as a GPU build by default, pulling several GB of unused
# nvidia-*/CUDA packages onto this GPU-less box. Installing a CPU-only
# build FIRST, pinned to whatever version pip would otherwise resolve,
# satisfies that dependency before requirements-filings.txt gets a
# chance to pull the GPU one -- confirmed live, this drops the install
# by ~4GB with no functional difference (torch runs on CPU automatically
# when no GPU is present either way).
.venv/bin/pip install torch torchvision --index-url https://download.pytorch.org/whl/cpu
.venv/bin/pip install -r requirements-filings.txt

# --- Only if you want the media app (requirements-media.txt), i.e.
#     ENABLE_MEDIA=true below: ---
.venv/bin/pip install -r requirements-media.txt

# --- Always: ---
.venv/bin/pip install -r requirements.txt
chmod +x deploy/deploy.sh   # git on the machine that authored this repo doesn't preserve the executable bit
```

## 2. Postgres — new database on the existing instance

```bash
sudo -u postgres psql
```
```sql
CREATE DATABASE finance_backend;
CREATE USER finance_backend WITH PASSWORD '<generate one -- openssl rand -base64 32>';
GRANT ALL PRIVILEGES ON DATABASE finance_backend TO finance_backend;
ALTER DATABASE finance_backend OWNER TO finance_backend;
\q
```
If this box already runs Postgres for something else, this is a plain
new database + user on that same instance — no version conflict, no
port conflict, nothing existing is touched. `DATABASE_URL` (below)
points at it either way.

## 3. Redis — pick an unused DB number on the existing instance

Redis databases are numbered 0–15 by default. If this Redis instance is
shared with something else, check what's already in use before picking
one so you don't collide with it:
```bash
redis-cli info keyspace
```
Pick a number not listed (e.g. `2`) — that becomes `REDIS_DB` below. If
the existing Redis has `requirepass` set, you'll also need that value for
`REDIS_PASSWORD`.

## 4. Weaviate — existing Docker instance, new collection namespace

No install needed — already running via Docker on this server. Confirm
its actual exposed ports (check `docker ps` or however it was started;
Weaviate's own defaults are `8080` REST / `50051` gRPC, but verify rather
than assume):
```bash
docker ps | grep weaviate
```
Set `WEAVIATE_URL=http://localhost:<rest-port>` and
`WEAVIATE_GRPC_HOST=localhost:<grpc-port>` accordingly. Set
`WEAVIATE_ENVIRONMENT=Prod` (or any value not already used) — this
namespaces the collection name (`FinanceApp_{env}_ResearchClip`,
`companies/services/vector_store.py`), so it can share the instance
without touching whatever collections already live there. This is
optional infra (semantic search only) — the rest of the app runs fine
without it configured.

## 5. `.env`

```bash
cp .env.example /home/deployer/finance-backend/.env
nano /home/deployer/finance-backend/.env
```
Fill in, at minimum:
- `SECRET_KEY` — generate a real one: `.venv/bin/python -c "from django.core.management.utils import get_random_secret_key; print(get_random_secret_key())"`
- `DEBUG=false`
- `ALLOWED_HOSTS=api.example.com`
- `CORS_ALLOWED_ORIGINS=https://app.example.com`
- `DATABASE_URL=postgres://finance_backend:<password>@localhost:5432/finance_backend`
- `REDIS_HOST=localhost`, `REDIS_DB=<the number you picked>`,
  `REDIS_USE_TLS=false` (local Redis, no TLS needed), plus
  `REDIS_PASSWORD` if the existing instance requires one
- `WEAVIATE_URL` / `WEAVIATE_GRPC_HOST` / `WEAVIATE_ENVIRONMENT=Prod` (step 4)
- `CSRF_TRUSTED_ORIGINS=https://api.example.com` — without this,
  the Django admin login form 403s once served over https behind nginx
  (`.env.example`'s own comment on this var).
- `TRUST_PROXY_SSL_HEADER=true` — this deployment terminates SSL at nginx
  and proxies to Uvicorn over plain HTTP on localhost, so Django needs to
  trust nginx's `X-Forwarded-Proto` header to know a request was actually
  HTTPS (`deploy/nginx-finance-api.conf` already sends that header;
  without this setting, `request.is_secure()` and secure-cookie behavior
  are wrong even though the browser sees a padlock).
- Every API key from `.env.example` this app actually uses (`FMP_API_KEY`,
  `SEC_CONTACT_EMAIL`, `ANTHROPIC_API_KEY`, `OPENAI_API_KEY`,
  `GEMINI_API_KEY`, `STEADY_API_TOKEN`, `GOOGLE_OAUTH_CLIENT_ID`, etc.)
- `ENABLE_LEAGUE=true` and/or `ENABLE_MEDIA=true` if this deployment
  wants those features -- both default off. `ENABLE_MEDIA` also needs
  `requirements-media.txt` installed (step 1) and `ffmpeg`/`ffprobe`
  on `PATH` (`apt install ffmpeg` on Ubuntu).

`systemd`'s `EnvironmentFile=` (the unit files below) reads this same
`.env` directly — no separate secrets file to keep in sync.

## 6. Django setup

```bash
cd /home/deployer/finance-backend
.venv/bin/python manage.py migrate
.venv/bin/python manage.py collectstatic --noinput
.venv/bin/python manage.py createsuperuser
```
That last command isn't just an admin convenience -- new-account sign-up
is gated by an allowlist (`accounts.AllowedEmail`) that starts out empty,
and creating a superuser is the one path that bypasses it (a management
command, not the sign-up API). Log into `/admin/` with it and add
whichever emails should be allowed to register, or nobody can sign up at
all. See README.md's "First login" section for the full walkthrough.

Run `init_weaviate` once too if you filled in step 4:
```bash
.venv/bin/python manage.py init_weaviate
```

## 7. systemd services

```bash
sudo cp deploy/finance-api.service deploy/finance-celery-worker.service deploy/finance-celery-beat.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now finance-api finance-celery-worker finance-celery-beat
sudo systemctl status finance-api finance-celery-worker finance-celery-beat
```
Three independent units — restarting/redeploying one never touches the
others, or (if this box runs other services) theirs. `finance-api` binds
only to `127.0.0.1:8001` (not a public port) — nginx is what's actually
public-facing.

**Celery worker capacity**: `finance-celery-worker.service` runs with
`--autoscale=8,1` — Celery spawns worker child processes between 1 and 8
as queue depth changes, so there's always at least one available without
manual intervention. Raise the max (`--autoscale=16,1`, etc.) in the unit
file + `daemon-reload` + restart if 8 isn't enough headroom once real
load shows up — check `sudo systemctl status finance-celery-worker` and
its journal (`journalctl -u finance-celery-worker`) for autoscale-related
log lines to tell if it's actually maxing out.

## 8. nginx

```bash
sudo cp deploy/nginx-finance-api.conf /etc/nginx/sites-available/api.example.com
sudo ln -s /etc/nginx/sites-available/api.example.com /etc/nginx/sites-enabled/
sudo nginx -t   # MUST pass before reloading -- a bad config here can also break every other site nginx serves
sudo systemctl reload nginx
```
`nginx -t` validates nginx's *entire* config, including any other site's
existing server blocks — if it fails, the problem is almost certainly a
syntax error in the new file you just added, not existing config; fix
before reloading regardless.

**Static files 403 / Django admin loads with no CSS**: nginx runs as
`www-data`, but `deployer`'s home directory defaults to `750` (owner-only)
on Ubuntu -- `www-data` can't traverse into it at all, even though the
files themselves are fine, so every `/static/*` request 403s. Confirmed
live: `curl -I https://api.example.com/static/admin/css/base.css`
returning `403` is the tell (a `404` would mean `collectstatic` itself
didn't run, a different problem). Fix with minimal exposure -- grant just
enough traversal to reach the static files, without opening up `.env` or
anything else in the repo:
```bash
chmod o+x /home/deployer
chmod o+x /home/deployer/finance-backend
chmod -R o+rX /home/deployer/finance-backend/staticfiles
```

## 9. DNS + SSL

Point `api.example.com`'s DNS A record at this server's IP first (in
whichever registrar/DNS provider manages that domain), then:
```bash
sudo apt install certbot python3-certbot-nginx   # skip if certbot is already installed on this box
sudo certbot --nginx -d api.example.com
```
Certbot rewrites `nginx-finance-api.conf`'s installed copy in place to
add the SSL server block + HTTP→HTTPS redirect and sets up auto-renewal —
don't hand-write SSL directives into that file yourself.

`app.example.com` (the frontend) is a separate deployment, not
covered here.

## Ongoing deploys

Once the above is done once, shipping new code is just:
```bash
/home/deployer/finance-backend/deploy/deploy.sh
```
`deploy.sh` pulls `main`, reinstalls dependencies, runs migrations,
collects static files, and restarts all three services. It never touches
nginx, systemd unit files, or `.env` — re-run the relevant step above by
hand if one of those genuinely needs to change.

`deploy.sh`'s restart step needs `sudo systemctl restart`, which
`deployer` doesn't have by default (only `ubuntu`, or whichever account
already has sudo on this box, does). Grant a narrowly-scoped exception —
just those three restarts, nothing else — once, as `ubuntu`:
```bash
echo 'deployer ALL=(root) NOPASSWD: /usr/bin/systemctl restart finance-api, /usr/bin/systemctl restart finance-celery-worker, /usr/bin/systemctl restart finance-celery-beat' | sudo tee /etc/sudoers.d/deployer-finance-backend
sudo visudo -c   # validates every sudoers file -- a syntax error here can lock out sudo entirely, always check before trusting it
```
Without this, run `deploy.sh`'s steps manually as `deployer` and do the
three `sudo systemctl restart` commands separately as `ubuntu`.

### Auto-deploy from GitHub Actions (optional)

`.github/workflows/test.yml` runs `deploy.sh` over SSH after tests pass on a
push to `main`. It only runs on the upstream repo (the `github.repository`
check in the `deploy` job) -- edit that condition to your own `owner/repo`
if you fork this. Add these repository secrets (Settings -> Secrets and
variables -> Actions):

- `DEPLOY_HOST` -- hostname or IP of your server.
- `DEPLOY_SSH_KEY` -- private key for the `deployer` user. Restrict its
  `authorized_keys` entry with `command="/home/deployer/finance-backend/deploy/deploy.sh",restrict`
  so the key can run that one script and nothing else.

## What this deliberately does NOT do

- Never edits an existing nginx server block — only adds
  `sites-available/api.example.com`.
- Never installs or reconfigures Postgres/Redis/Weaviate as services —
  only creates new logical resources (database, DB number, collection
  namespace) on whatever's already running, if anything.
- Never runs as `root` — a dedicated non-root account owns the code,
  venv, and running processes.
