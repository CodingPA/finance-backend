"""Classifies an exception from an AI competitor's LLM call (league.tasks.
run_agent_wakeup, across all three provider branches) as TERMINAL -- an
invalid/revoked API key or an account with no credits/quota left, where
retrying the exact same call later is pointless without an operator fixing
something external -- or TRANSIENT -- a rate limit, a 5xx, a network blip,
likely to clear up on its own. run_agent_wakeup uses this to decide whether
to pause the competitor and alert loudly (terminal) or let the normal
safety-net retry handle it (transient, see _ensure_future_wakeup_scheduled).

Built directly from two real production failures (2026-08-25/26
investigation, see docs/ or the wakeup-findings writeup): an
anthropic.AuthenticationError (401, revoked ANTHROPIC_API_KEY) and an
openai.RateLimitError (429, but body.type == "insufficient_quota" -- an
exhausted account, not actual request-rate throttling). Status code alone
doesn't separate "your key is dead" from "you're sending requests too
fast" -- both surface as a 4xx APIStatusError -- so the response body's
own type/code field is checked for the 429 case specifically, not just
status_code.

Gemini's RESOURCE_EXHAUSTED (429) genuinely does cover both plain rate
limiting and true quota exhaustion with no documented way to tell them
apart from the exception alone (unlike Anthropic/OpenAI's distinct
"insufficient_quota" body marker) -- deliberately left classified as
transient here rather than guessing, since wrongly pausing a healthy
competitor on its first ordinary rate-limit hit is worse than a few extra
retries for a genuinely exhausted Gemini account. "A few" is a real bound,
not indefinite: league.tasks.CONSECUTIVE_FAILURE_PAUSE_THRESHOLD pauses
the competitor after that many straight failures regardless of what this
function returns, specifically so a non-terminal classification here
(this Gemini case, or any RuntimeError this module was never meant to
recognize -- a missing/misconfigured API key, for instance) can't retry
forever just because is_terminal said no.

Deliberately does NOT use openai.APIError's own `.type`/`.code`
convenience attributes -- confirmed against the installed openai==2.46.0
SDK (_exceptions.py's APIError.__init__) that they only read TOP-LEVEL
keys of the response body (`body.get("type")`, `body.get("code")`), but a
real OpenAI error body nests everything one level down under "error"
(`{"error": {"type": "insufficient_quota", "code":
"credit_balance_exhausted", ...}}`, confirmed against the actual
traceback from the 2026-08-26 incident), so those attributes are simply
None on a real quota-exhaustion RateLimitError -- checking them would have
silently failed to classify the exact failure this module exists to
catch. `.body` itself is read directly instead, one level in, for both
Anthropic and OpenAI (anthropic.APIStatusError's own `.type` DOES
correctly unwrap "error" -- confirmed in _exceptions.py -- but reading
`.body` uniformly for both means one code path instead of two, and
doesn't depend on that being true forever)."""

import anthropic
import openai
from google.genai import errors as genai_errors

# HTTP status codes that are terminal regardless of body content -- an
# invalid/revoked key (401) or a key without permission for this
# resource/model (403) doesn't get better on retry.
_TERMINAL_STATUS_CODES = {401, 403}

# Anthropic/OpenAI error body "type" or "code" values (lowercased) that
# mean "this account has no credits/quota left", found riding on an
# otherwise-ordinary-looking 429 RateLimitError. A plain 429 without one
# of these is real rate limiting -- transient, back off and retry.
_QUOTA_EXHAUSTED_MARKERS = {
    "insufficient_quota", "credit_balance_exhausted", "billing_hard_limit_reached",
}


def _error_envelope(exc) -> dict:
    """The inner {"type": ..., "code": ..., "message": ...} dict of an
    Anthropic/OpenAI error response body, unwrapped from its outer
    {"error": {...}} envelope -- see this module's docstring for why this
    is done by hand rather than trusting the SDK's own `.type`/`.code`."""
    body = getattr(exc, "body", None)
    if isinstance(body, dict):
        inner = body.get("error")
        if isinstance(inner, dict):
            return inner
    return {}


def is_terminal(exc: Exception) -> bool:
    """See this module's docstring for the two real cases this covers."""
    if isinstance(exc, (anthropic.APIStatusError, openai.APIStatusError)):
        if exc.status_code in _TERMINAL_STATUS_CODES:
            return True
        if exc.status_code == 429:
            envelope = _error_envelope(exc)
            marker = str(envelope.get("type") or envelope.get("code") or "").lower()
            return marker in _QUOTA_EXHAUSTED_MARKERS
        return False
    if isinstance(exc, genai_errors.APIError):
        return exc.code in _TERMINAL_STATUS_CODES
    return False


def describe_error(exc: Exception, max_length: int = 2000) -> str:
    """Short human string for AgentWakeup.error and the operator
    notification -- provider, status, and the API's own message, instead
    of "Check server logs for details." (the entire notification body
    before this existed)."""
    if isinstance(exc, (anthropic.APIStatusError, openai.APIStatusError)):
        provider = "Anthropic" if isinstance(exc, anthropic.APIStatusError) else "OpenAI"
        envelope = _error_envelope(exc)
        detail = envelope.get("type") or envelope.get("code") or ""
        message = envelope.get("message") or exc.message
        text = f"{provider} {exc.status_code}{f' ({detail})' if detail else ''}: {message}"
    elif isinstance(exc, genai_errors.APIError):
        text = f"Gemini {exc.code} ({exc.status}): {exc.message}"
    else:
        text = f"{type(exc).__name__}: {exc}"
    return text[:max_length]
