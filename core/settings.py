"""
Django settings for core project.
"""

import os
from datetime import timedelta
from pathlib import Path

import dj_database_url
from celery.schedules import crontab
from django.core.exceptions import ImproperlyConfigured
from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent.parent

load_dotenv(BASE_DIR / ".env")


def env_bool(name: str, default: bool) -> bool:
    val = os.environ.get(name)
    return default if val is None else val.strip().lower() in ("1", "true", "yes", "on")


def env_list(name: str, default: list[str]) -> list[str]:
    val = os.environ.get(name)
    return [item.strip() for item in val.split(",") if item.strip()] if val else default


DEBUG = env_bool("DEBUG", True)

# SECURITY WARNING: keep the secret key used in production secret!
# Falls back to a dev-only key so `manage.py runserver` works out of the box
# -- but only while DEBUG is true. The old version of this fallback applied
# unconditionally, including with DEBUG=False, which is exactly the insecure
# default a cloned-and-deployed public repo would silently inherit. Outside
# local dev, a missing SECRET_KEY is now a hard error instead of a shared key.
SECRET_KEY = os.environ.get("SECRET_KEY", "")
if not SECRET_KEY:
    if DEBUG:
        SECRET_KEY = "django-insecure-5*$6j4u+tr702nf4%c#43yi4%x03c6k*wdn!7c!r6$di6hs+&n"
    else:
        raise ImproperlyConfigured("SECRET_KEY must be set in the environment when DEBUG=False.")

ALLOWED_HOSTS = env_list("ALLOWED_HOSTS", ["localhost", "127.0.0.1"])

# media app: mp3/mp4 upload -> ffmpeg audio extraction -> diarized
# transcription. Off by default -- it's a niche feature on this platform,
# and the only one that needs a real system binary (ffmpeg/ffprobe)
# outside the Python dependency tree, so gating it is what lets a Docker
# image skip installing that binary entirely rather than always paying
# for a feature most deployments won't touch. Read at module level
# (unlike ENABLE_LEAGUE further down) because it also has to decide
# whether "media" belongs in INSTALLED_APPS below.
ENABLE_MEDIA = env_bool("ENABLE_MEDIA", False)


# Application definition

INSTALLED_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "channels",
    "rest_framework",
    "rest_framework_simplejwt.token_blacklist",
    "corsheaders",
    "accounts",
    "companies",
    "realtime",
    "notifications",
    "assistant",
    "league",
]
if ENABLE_MEDIA:
    INSTALLED_APPS.append("media")

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "corsheaders.middleware.CorsMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
]

ROOT_URLCONF = "core.urls"

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
            ],
        },
    },
]

WSGI_APPLICATION = "core.wsgi.application"
ASGI_APPLICATION = "core.asgi.application"


# Database
# SQLite for local dev by default. Set DATABASE_URL (e.g.
# postgres://user:pass@host:port/dbname) to point at Postgres in deployment --
# no code change needed, same settings file either way.
DATABASES = {
    "default": dj_database_url.parse(
        # dj_database_url.config()'s `default=` only applies when the env var
        # is entirely absent -- a present-but-blank DATABASE_URL=  (an easy
        # .env mistake) is treated as "set", producing an empty, brokenly
        # engine-less config instead of falling back to SQLite. Checking
        # truthiness ourselves avoids that trap.
        os.environ.get("DATABASE_URL") or f"sqlite:///{BASE_DIR / 'db.sqlite3'}",
        # Kept short (not Django's typical 300-600s) because celery's
        # --autoscale=8,1 (deploy/finance-celery-worker.service) forks up to
        # 8 worker processes under load, each holding its own persistent
        # connection -- on top of finance-api's uvicorn workers and beat.
        # Long-lived idle connections from that fleet exhausted Postgres's
        # max_connections and locked the app out with "remaining connection
        # slots are reserved for roles with the SUPERUSER attribute"
        # (2026-09-09 prod incident). A low conn_max_age lets Postgres
        # reclaim idle connections quickly instead of them piling up.
        conn_max_age=60,
    )
}

AUTH_USER_MODEL = "accounts.User"


# Password validation

AUTH_PASSWORD_VALIDATORS = [
    {"NAME": "django.contrib.auth.password_validation.UserAttributeSimilarityValidator"},
    {"NAME": "django.contrib.auth.password_validation.MinimumLengthValidator"},
    {"NAME": "django.contrib.auth.password_validation.CommonPasswordValidator"},
    {"NAME": "django.contrib.auth.password_validation.NumericPasswordValidator"},
]


# Internationalization

LANGUAGE_CODE = "en-us"
TIME_ZONE = "UTC"
USE_I18N = True
USE_TZ = True


# Static files (CSS, JavaScript, Images)

STATIC_URL = "static/"

# Where `collectstatic` writes, and what nginx serves /static/ from in
# deployment (docs/deployment.md). Only the admin needs this -- the frontend
# is a separate app -- but with DEBUG=False Django stops serving admin CSS
# itself, so without it the admin renders unstyled. Also required for
# `collectstatic` to run at all: it hard-errors when STATIC_ROOT is unset,
# which would abort deploy.sh.
STATIC_ROOT = BASE_DIR / "staticfiles"

# User-uploaded media (media app -- MediaFile.file). S3-backed when
# AWS_STORAGE_BUCKET_NAME is set (real dev/prod use -- the whole point of
# this app is processing real mp3/mp4 uploads); falls back to local disk
# storage otherwise so `manage.py check`/tests don't need real AWS
# credentials just to import the app.
AWS_ACCESS_KEY_ID = os.environ.get("AWS_ACCESS_KEY_ID", "")
AWS_SECRET_ACCESS_KEY = os.environ.get("AWS_SECRET_ACCESS_KEY", "")
AWS_STORAGE_BUCKET_NAME = os.environ.get("AWS_STORAGE_BUCKET_NAME", "")
AWS_S3_REGION_NAME = os.environ.get("AWS_S3_REGION_NAME", "")

STORAGES = {
    "default": {
        "BACKEND": "storages.backends.s3.S3Storage" if AWS_STORAGE_BUCKET_NAME else "django.core.files.storage.FileSystemStorage",
    },
    "staticfiles": {
        "BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage",
    },
}

# Only used by the FileSystemStorage fallback above. Without it that backend
# writes relative to the process working directory, which under systemd is
# wherever WorkingDirectory happens to point -- uploads would land in
# unpredictable places instead of one known directory. Deliberately not
# `media/`: that name is already taken by the media app package.
MEDIA_ROOT = BASE_DIR / "uploads"

DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"


# Django REST Framework

REST_FRAMEWORK = {
    "DEFAULT_AUTHENTICATION_CLASSES": (
        "rest_framework_simplejwt.authentication.JWTAuthentication",
    ),
    "DEFAULT_PERMISSION_CLASSES": (
        "rest_framework.permissions.IsAuthenticated",
    ),
}

SIMPLE_JWT = {
    "ACCESS_TOKEN_LIFETIME": timedelta(minutes=30),
    "REFRESH_TOKEN_LIFETIME": timedelta(days=14),
    "ROTATE_REFRESH_TOKENS": True,
    "BLACKLIST_AFTER_ROTATION": True,
    "UPDATE_LAST_LOGIN": True,
    "AUTH_HEADER_TYPES": ("Bearer",),
    "USER_ID_FIELD": "id",
    "USER_ID_CLAIM": "user_id",
}


# CORS -- the frontend is a separate app, not served by Django.
CORS_ALLOWED_ORIGINS = env_list(
    "CORS_ALLOWED_ORIGINS",
    ["http://localhost:3000", "http://127.0.0.1:3000"] if DEBUG else [],
)

# Hosts Django accepts unsafe-method POSTs from with a session cookie -- in
# practice just the admin login form, since the API itself is JWT-authed and
# CSRF-exempt. Django rejects the admin login with a 403 over https without
# this, which reads like a wrong password rather than a config gap.
CSRF_TRUSTED_ORIGINS = env_list("CSRF_TRUSTED_ORIGINS", [])

# Behind nginx, every request arrives over plain HTTP on loopback -- Django
# would consider them all insecure and build http:// absolute URLs. Only
# honour the header when explicitly enabled: trusting it while the app is
# reachable directly would let a client spoof "this was https". Set it in
# deployment, where nginx overwrites the header on every proxied request and
# the app only listens on 127.0.0.1 (docs/deployment.md).
if env_bool("TRUST_PROXY_SSL_HEADER", False):
    SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")

# Standard Django production hardening -- gated on DEBUG rather than always
# on, since these break plain-http local dev (cookies marked Secure never
# get sent back over http://localhost, and SSL_REDIRECT would loop). Tied
# to the same DEBUG flag as the SECRET_KEY guard above: the one place both
# a real key and these settings are required is exactly "DEBUG=False",
# i.e. anything that isn't local dev.
if not DEBUG:
    SESSION_COOKIE_SECURE = True
    CSRF_COOKIE_SECURE = True
    SECURE_SSL_REDIRECT = env_bool("TRUST_PROXY_SSL_HEADER", False)
    SECURE_HSTS_SECONDS = 31536000
    SECURE_HSTS_INCLUDE_SUBDOMAINS = True
    SECURE_HSTS_PRELOAD = True

# Google sign-in: the frontend gets an ID token from Google Identity Services
# and POSTs it here; the backend verifies it was issued for this client ID.
GOOGLE_OAUTH_CLIENT_ID = os.environ.get("GOOGLE_OAUTH_CLIENT_ID", "")

# companies app: SEC EDGAR + FMP screening.
FMP_API_KEY = os.environ.get("FMP_API_KEY", "")
# SEC requires a contact email in the User-Agent on every request -- 403s
# without one. No hardcoded fallback on purpose; screening fails loudly
# rather than silently hitting SEC with a placeholder.
SEC_CONTACT_EMAIL = os.environ.get("SEC_CONTACT_EMAIL", "")

# SteadyAPI: live options chain (price, greeks, IV) -- companies/services/steady.py.
STEADY_API_TOKEN = os.environ.get("STEADY_API_TOKEN", "")

# Tradier: quotes + option chains, evaluated as a fresher alternative to
# SteadyAPI (whose options data only updates ~every 10 min) --
# companies/services/tradier.py. Base URL defaults to production, since
# that's the only Tradier environment with real-time data; sandbox uses
# the same API shape but serves delayed data (see .env.example).
TRADIER_API_TOKEN = os.environ.get("TRADIER_API_TOKEN", "")
TRADIER_BASE_URL = os.environ.get("TRADIER_BASE_URL", "https://api.tradier.com")

# Polygon.io (rebranded "Massive" in 2026; API still served from
# api.polygon.io): the companies app's only source of PERSISTED daily
# OHLCV history -- companies/services/polygon.py backs the DailyBar table
# with one full-market "grouped daily" call per session, unlike tradier.py's
# per-symbol get_history (which stores nothing). Free "Stocks Basic" tier
# works fine for this (confirmed live, 2026-09-20): 5 req/min, 2 years of
# history, end-of-day data.
POLYGON_API_KEY = os.environ.get("POLYGON_API_KEY", "")

# Brave Search API: the only GENERIC open-web search this app has --
# companies/services/brave_search.py, used by assistant/tools.py's and
# league/tools.py's search_web tool (both AI surfaces, human chat and the
# paper-trading league). Distinct from each provider's own native
# web_search server tool (also wired into league/tools.py) -- this is a
# second, always-available option alongside those.
BRAVE_API_KEY = os.environ.get("BRAVE_API_KEY", "")

# Which provider companies/services/pricing.py dispatches live quote/options
# chain calls to -- "steady" or "tradier". Everything that prices a trade or
# a mark-to-market (league's order filler, both AI agent tool surfaces) goes
# through pricing.py, never steady.py/tradier.py directly, so flipping this
# one value switches all of them together. Env-var rather than a DB-backed
# toggle on purpose: this project is meant to be run by people who'll only
# have one of the two API tokens, not both, so it's a deploy-time choice,
# not something to flip at runtime from admin. Defaults to "tradier" to
# match .env.example and this project's own production deployment -- a
# fresh .env with this line deleted (rather than copied verbatim from
# .env.example) should still behave the same way, not silently switch
# providers depending on which of the two files someone happened to look at.
PRICING_PROVIDER = os.environ.get("PRICING_PROVIDER", "tradier").strip().lower()
if PRICING_PROVIDER not in {"steady", "tradier"}:
    raise ValueError(f"PRICING_PROVIDER must be 'steady' or 'tradier', got {PRICING_PROVIDER!r}")


# Celery / Redis -- background jobs (batch screening, and eventually AI
# processing: article ingestion, text extraction, LLM calls). Built from
# discrete host/port/username/password/db/timeout rather than a single
# connection string, since that's what Redis Cloud hands out.
REDIS_HOST = os.environ.get("REDIS_HOST", "")
REDIS_PORT = os.environ.get("REDIS_PORT", "6379")
REDIS_USERNAME = os.environ.get("REDIS_USERNAME", "")
REDIS_PASSWORD = os.environ.get("REDIS_PASSWORD", "")
REDIS_DB = os.environ.get("REDIS_DB", "0")
REDIS_TIMEOUT = int(os.environ.get("REDIS_TIMEOUT", "10"))
REDIS_USE_TLS = env_bool("REDIS_USE_TLS", True)  # Redis Cloud databases typically require TLS

_redis_scheme = "rediss" if REDIS_USE_TLS else "redis"
_redis_auth = f"{REDIS_USERNAME}:{REDIS_PASSWORD}@" if REDIS_PASSWORD else ""
# celery's redis backend (unlike kombu's broker transport) hard-requires
# ssl_cert_reqs on any rediss:// URL -- confirmed live, `celery -A core
# worker` raises E_REDIS_SSL_CERT_REQS_MISSING_INVALID at startup without
# this. Prod never hits this: it runs a local redis-server with
# REDIS_USE_TLS=false (docs/deployment.md), i.e. plain redis://. This only
# matters for a local dev setup pointed at Redis Cloud (rediss://).
# "required" verifies against the system CA bundle, which Redis Cloud's
# cert (a public CA, not self-signed) satisfies.
_redis_query = "?ssl_cert_reqs=required" if REDIS_USE_TLS else ""
REDIS_URL = f"{_redis_scheme}://{_redis_auth}{REDIS_HOST}:{REDIS_PORT}/{REDIS_DB}{_redis_query}"

CELERY_BROKER_URL = REDIS_URL
CELERY_RESULT_BACKEND = REDIS_URL
# visibility_timeout: how long an unacked message can sit "reserved" before
# Redis considers it abandoned and redelivers it. Kombu's Redis transport
# defaults this to 3600s (1 hour) -- far shorter than the league's own
# apply_async(eta=...) wake-ups, which routinely schedule days out (e.g. a
# competitor waiting for an earnings date). An eta task isn't acked until
# it actually runs, so a default 1-hour timeout risks the SAME wake-up
# being redelivered/re-reserved and potentially double-firing well before
# its actual eta, not just on a worker crash -- this is a documented
# Celery+Redis caveat for long-ETA tasks, not specific to this app. Set
# comfortably past the longest ETA this app realistically schedules.
CELERY_BROKER_TRANSPORT_OPTIONS = {
    "socket_timeout": REDIS_TIMEOUT, "socket_connect_timeout": REDIS_TIMEOUT,
    "visibility_timeout": 1209600,  # 14 days
}
CELERY_BROKER_CONNECTION_RETRY_ON_STARTUP = True
CELERY_ACCEPT_CONTENT = ["json"]
CELERY_TASK_SERIALIZER = "json"
CELERY_RESULT_SERIALIZER = "json"
CELERY_TIMEZONE = TIME_ZONE
CELERY_TASK_TRACK_STARTED = True

# Whether Celery beat schedules the paper trading league's own recurring
# jobs (below). Off by default: several of these bill a real LLM call on
# a recurring schedule (league-forced-checkins ticks every 5 minutes and
# dispatches a wake-up whenever one is due) -- a fresh clone that sets an
# API key and starts beat because the README says to shouldn't get billed
# for a competition it never created. This is a deploy-time choice, same
# reasoning as PRICING_PROVIDER above -- not something to flip at runtime.
# A deployment that runs a competition must set ENABLE_LEAGUE=true in its
# .env: with it unset, the league's beat jobs (forced check-ins especially)
# silently stop firing -- no error, just nothing scheduled.
ENABLE_LEAGUE = env_bool("ENABLE_LEAGUE", False)

# Daily price-only refresh (see companies/tasks.py::refresh_prices) --
# 21:30 UTC lands after 4pm ET in both EDT and EST, so it's always after
# market close regardless of the time of year. A fixed crontab entry here
# doesn't need django-celery-beat; that's only for schedules editable at
# runtime, which this isn't.
CELERY_BEAT_SCHEDULE = {
    "refresh-prices-daily": {
        "task": "companies.tasks.refresh_prices",
        "schedule": crontab(hour=21, minute=30),
    },
    "poll-wiki-sources-daily": {
        "task": "companies.tasks.poll_wiki_sources",
        "schedule": crontab(hour=22, minute=0),
    },
    # Twice-daily subreddit scan (companies/tasks.py::scan_tracked_subreddits)
    # -- two plain fixed-time crontab entries calling the same task, not the
    # "tick every 5 min + internal window/no-op logic" pattern the league
    # tasks below use. That pattern exists specifically for market-hours-
    # gated tasks where a static UTC hour would drift across DST; a fixed
    # "twice a day" cadence with no market-hours dependency doesn't have
    # that problem, so two crontab entries is simpler and is exactly what
    # Beat is for.
    "scan-tracked-subreddits-morning": {
        "task": "companies.tasks.scan_tracked_subreddits",
        "schedule": crontab(hour=12, minute=0),
    },
    "scan-tracked-subreddits-evening": {
        "task": "companies.tasks.scan_tracked_subreddits",
        "schedule": crontab(hour=0, minute=0),
    },
    # Daily Reddit pull for every screener-tracked ticker (companies/
    # tasks.py::scan_reddit_for_tracked_tickers) -- right after
    # refresh-prices-daily, same "after market close" reasoning.
    "scan-reddit-for-tracked-tickers-daily": {
        "task": "companies.tasks.scan_reddit_for_tracked_tickers",
        "schedule": crontab(hour=21, minute=45),
    },
    # Full-market daily-bar ingest (companies.tasks.ingest_daily_bars) --
    # 22:15 UTC, half an hour after refresh-prices-daily, to give Polygon's
    # grouped-daily payload time to populate after the close. This gap is a
    # first guess, not a confirmed minimum -- tighten or loosen once actual
    # post-close availability is observed.
    "ingest-daily-bars": {
        "task": "companies.tasks.ingest_daily_bars",
        "schedule": crontab(hour=22, minute=15),
    },
}

if ENABLE_LEAGUE:
    CELERY_BEAT_SCHEDULE.update({
        # Scoreboard housekeeping for the paper trading league (league/tasks.py)
        # -- unrelated to an AI competitor's own scheduled wake-ups, which use
        # Celery's native apply_async(eta=...) instead of Beat. Sunday 22:00 UTC:
        # after Friday's close in every US timezone, ahead of the next week's
        # first session.
        "league-mark-weekly-values": {
            "task": "league.tasks.mark_weekly_values",
            "schedule": crontab(hour=22, minute=0, day_of_week=0),
        },
        # Guaranteed floor of 3 daily wake-ups per AI competitor (news/open/close),
        # on top of whatever an agent schedules for itself -- ticks every 5 minutes
        # so league.tasks.dispatch_forced_checkins can compare America/New_York
        # wall-clock time itself (DST-aware via zoneinfo) rather than relying on a
        # fixed UTC hour, which would drift across this competition's Nov 1 DST
        # change. See FORCED_CHECKINS in that module for the actual times.
        "league-forced-checkins": {
            "task": "league.tasks.dispatch_forced_checkins",
            "schedule": crontab(minute="*/5"),
        },
        # Fills/rejects every Trade still STATUS_PENDING (league.tasks.
        # process_pending_trades) -- only exist when a competition opts into
        # Competition.restrict_after_hours_trading (simulating a broker with
        # no extended-hours trading) and a trade was submitted while the
        # market was closed. Same 5-minute DST-aware wall-clock-ET tick as
        # league-forced-checkins above -- the task itself no-ops outside
        # market hours.
        "league-process-pending-trades": {
            "task": "league.tasks.process_pending_trades",
            "schedule": crontab(minute="*/5"),
        },
        # Keeps TickerSnapshot (companies/models.py) warm so the scoreboard/
        # competitor-list/competitor-detail endpoints never live-quote Steady on
        # a page load (league/services/valuation.py's module docstring). Ticks
        # every 10 minutes, same DST-aware wall-clock-ET gating as
        # dispatch_forced_checkins above, rather than trying to encode 9:30am-
        # 4pm ET into a fixed UTC crontab -- the task itself no-ops outside
        # market hours (league.tasks.refresh_open_position_quotes).
        "league-refresh-open-position-quotes": {
            "task": "league.tasks.refresh_open_position_quotes",
            "schedule": crontab(minute="*/10"),
        },
        # Resolves every short call/put Position whose expiration has passed
        # (league.tasks.settle_expired_options) -- assigned or expired
        # worthless, since nothing else in this app auto-expires/auto-assigns
        # options. 21:35 UTC: 5 minutes after refresh-prices-daily, so it runs
        # against the same just-closed session's prices, and (like that entry)
        # lands after 4pm ET close in both EDT and EST.
        "league-settle-expired-options": {
            "task": "league.tasks.settle_expired_options",
            "schedule": crontab(hour=21, minute=35),
        },
        # Recovers AgentWakeups whose apply_async(eta=...) task the Celery/
        # Redis broker silently dropped -- see league.tasks.
        # sweep_overdue_wakeups's docstring (confirmed happening in
        # production, 2026-08-25/26 investigation, most plausibly a worker
        # restart -- deploy.sh restarts finance-celery-worker on every
        # deploy -- dropping an in-memory ETA task). Ticks every 10 minutes;
        # the task itself only acts on rows overdue by its own
        # OVERDUE_SWEEP_GRACE (also 10 minutes), well past any realistic
        # on-time delivery, so this doesn't race a wake-up that's merely
        # running a little late.
        "league-sweep-overdue-wakeups": {
            "task": "league.tasks.sweep_overdue_wakeups",
            "schedule": crontab(minute="*/10"),
        },
    })

# Weaviate + OpenAI embeddings -- semantic search infra over ResearchClip
# text (companies/services/vector_store.py, embeddings.py). No search
# endpoint uses this yet; it's just the ingestion pipeline for now.
# WEAVIATE_ENVIRONMENT prefixes the collection name -- this Weaviate server
# is shared across other apps, and this app doesn't have a prod instance
# yet, so the collection name has to carry both namespaces itself.
WEAVIATE_URL = os.environ.get("WEAVIATE_URL", "")
WEAVIATE_GRPC_HOST = os.environ.get("WEAVIATE_GRPC_HOST", "")
WEAVIATE_API_KEY = os.environ.get("WEAVIATE_API_KEY", "")
WEAVIATE_ENVIRONMENT = os.environ.get("WEAVIATE_ENVIRONMENT", "Dev")

# No vectorizer module is enabled on that Weaviate server (DEFAULT_VECTORIZER_
# MODULE=none) -- every object is inserted with a precomputed vector, hence
# needing an embeddings provider directly.
OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY", "")
OPENAI_EMBEDDING_MODEL = "text-embedding-3-large"

# Chat assistant (assistant app). Which model a given chat uses is a per-row
# choice on AssistantModel (admin-managed), not a setting -- this key is the
# only thing that's global.
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
# league's AI competitors can also run on OpenAI or Gemini
# (AssistantModel.provider, league/services/llm_openai.py /
# llm_gemini.py) -- the assistant app above stays Anthropic-only.
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "")
# How many of a conversation's most recent messages (across both roles) ride
# along as context on every new turn -- trimmed further at request time if
# even that many blows past the model's max_input_tokens.
ASSISTANT_HISTORY_LENGTH = int(os.environ.get("ASSISTANT_HISTORY_LENGTH", "10"))

# Media ingestion (media app): how many 60s audio chunks get diarized
# concurrently (via a thread pool -- these are I/O-bound OpenAI calls, not
# CPU-bound work) once the first minute's reference speaker samples are
# in hand. Independent of Celery worker concurrency -- this bounds one
# task's own fan-out, not how many tasks a worker runs at once.
ASSISTANT_MEDIA_CHUNK_CONCURRENCY = int(os.environ.get("ASSISTANT_MEDIA_CHUNK_CONCURRENCY", "4"))
# Explicit ffmpeg/ffprobe binary override (see media/services/ffmpeg.py) --
# empty by default, which triggers PATH lookup first (the real server
# install) then a project-root ffmpeg.exe/ffprobe.exe fallback (local
# Windows dev, where it's simplest to just drop the binaries in next to
# manage.py rather than fight with PATH).
FFMPEG_PATH = os.environ.get("FFMPEG_PATH", "")
FFPROBE_PATH = os.environ.get("FFPROBE_PATH", "")

# Channels' channel layer -- the message bus that lets a Celery worker process
# push a message to a WebSocket held open by the ASGI server process. Same
# Redis instance as Celery; different key namespace internally, no collision.
CHANNEL_LAYERS = {
    "default": {
        "BACKEND": "channels_redis.core.RedisChannelLayer",
        "CONFIG": {"hosts": [REDIS_URL]},
    }
}

# Django's cache framework -- same Redis instance as Celery/Channels again
# (KEY_PREFIX keeps it out of their way, same "no collision" reasoning as
# CHANNEL_LAYERS above). Consumer: league/services/valuation.py's
# get_stock_quote() remembers which SteadyAPI asset class (STOCKS/ETF/
# MUTUALFUNDS) a ticker actually resolves to, so a known ETF/mutual fund
# skips the up-to-3-sequential-call probe on every subsequent lookup.
# (The actual display/mark-to-market values live in a real DB table now --
# companies.models.TickerSnapshot -- not this cache; this is just the
# one remaining Redis-TTL optimization in that module.)
CACHES = {
    "default": {
        "BACKEND": "django.core.cache.backends.redis.RedisCache",
        "LOCATION": REDIS_URL,
        "KEY_PREFIX": "django-cache",
        "TIMEOUT": 300,
    }
}
