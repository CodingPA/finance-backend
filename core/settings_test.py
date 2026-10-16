"""Settings for `manage.py test`/CI -- imports the real settings.py
unchanged, then swaps the handful of things that make the suite depend on
live infrastructure or real credentials it has no business needing.

Two, and only two, actual problems on a clean clone (no .env filled in,
no Redis running), confirmed by running the suite that way and tracing
each failure to its source rather than guessing:

1. league/services/valuation.py is the ONLY module in this codebase that
   imports Django's cache framework (`from django.core.cache import
   cache`) -- its get_stock_quote() remembers which SteadyAPI asset class
   a ticker resolves to. With CACHES pointed at a real Redis server (the
   default in settings.py, by design -- see its own comment), a
   cache.get() with no Redis reachable raises redis.exceptions.
   ConnectionError uncaught, which was the actual cause of the ~74
   failures/errors seen in a from-scratch run, most of them nowhere near
   caching on their face (the exception propagates up out of
   get_stock_quote and aborts whatever the test was actually checking).
   Swapped for LocMemCache here, which behaves identically for every
   caller -- nothing in this codebase asserts anything about CACHES'
   specific backend.

   CHANNEL_LAYERS is swapped the same way alongside it, even though nothing
   in the current suite actually exercises it (every test that could --
   realtime.utils.broadcast_to_group's callers -- mocks that function
   directly, confirmed by checking every call site). Cheap insurance in
   the one module built specifically to keep tests hermetic, so a future
   test added without that same care doesn't reintroduce this class of
   failure.

2. Four settings are checked with a hard `raise RuntimeError(...)` before
   ever reaching the (correctly mocked) SDK call the test actually cares
   about: GEMINI_API_KEY (league/services/llm_gemini.py),
   OPENAI_API_KEY (league/services/llm_openai.py), ANTHROPIC_API_KEY
   (league/tasks.py's _run_anthropic_turn), and SEC_CONTACT_EMAIL
   (companies/services/http.py's user_agent(), called by any SEC EDGAR
   request that doesn't pass its own headers). None of these tests want
   to make a real API call -- they patch the client/SDK -- they just need
   the settings guard to pass first. Real values in a developer's own
   .env silently papered over this; a fresh clone with blank values hits
   the raise before the mock ever gets a chance. Unconditional dummy
   values here (not "keep the real one if set") on purpose: a real key
   sitting in the environment during a test run only shrinks the chance a
   mocking gap gets caught before it makes an actual network call.

Nothing else changes -- same DATABASE_URL/PRICING_PROVIDER/etc. handling
as settings.py, same SQLite-by-default fallback. Use with:

    manage.py test --settings=core.settings_test
"""

from .settings import *  # noqa: F401,F403

CACHES = {
    "default": {
        "BACKEND": "django.core.cache.backends.locmem.LocMemCache",
    }
}

CHANNEL_LAYERS = {
    "default": {
        "BACKEND": "channels.layers.InMemoryChannelLayer",
    }
}

GEMINI_API_KEY = "test-gemini-key"
OPENAI_API_KEY = "test-openai-key"
ANTHROPIC_API_KEY = "test-anthropic-key"
SEC_CONTACT_EMAIL = "test@example.com"
