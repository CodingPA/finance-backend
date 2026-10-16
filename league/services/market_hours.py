"""Whether a specific option contract has stopped trading -- shared by
league.services.execution (rejects a new/closing trade on an already-
expired contract) and league.services.valuation (stops marking one at
avg_cost once it's expired). A separate module rather than importing
league.tasks' own private _MARKET_OPEN/_MARKET_CLOSE: tasks.py imports
this app's services modules at load time, so services importing back
from tasks.py would be a cycle.

Deliberately narrow -- this is NOT a general "is the market open right
now" gate on trading. docs/paper-trading-league-rules.md Rule 6 is
explicit: "Trades can be posted any time between check-ins." A live stock
trade at 9pm or on a Saturday is intentional (whatever the pricing
provider's last quote was), not a bug -- see execution.py's own docstring
on fill price being "whatever's live right now." What this module guards
against is different in kind: naming a contract that has already stopped
existing as a tradeable instrument, which "any time" was never meant to
permit. A 0DTE contract stops trading at the close of its own expiration
day same as any other trading day, not at midnight -- confirmed against a
real incident this module exists for: a short put sold at 4:29pm ET was
fine (it expired two days later), but nothing in the code would have
stopped the same sell if that put's expiration had been that same day."""

from datetime import date, datetime, time

from django.utils import timezone

# Reuses wakeup_spacing's own zoneinfo.ZoneInfo("America/New_York")
# construction rather than a third independent one (league.tasks has its
# own private copy too, for the same import-cycle reason as above) --
# cheap either way, but no reason to triplicate it when this module
# already sits next to wakeup_spacing.py with no risk of circularity.
from .wakeup_spacing import EASTERN  # noqa: F401 (re-exported for callers that want it)

MARKET_OPEN = time(9, 30)
MARKET_CLOSE = time(16, 0)


def is_market_open(now: datetime | None = None) -> bool:
    """Regular NYSE session, Mon-Fri, 9:30am-4:00pm ET -- market holidays NOT
    accounted for (same known gap as dispatch_forced_checkins/
    _time_context_block elsewhere in league.tasks). Single shared definition
    so league.tasks._time_context_block (wake-up prompt display) and
    league.services.execution.submit_trade's after-hours order-queuing
    decision (Competition.restrict_after_hours_trading) can't drift against
    each other -- both call this instead of keeping their own copy of the
    same weekday/9:30/16:00 check."""
    now_et = (now or timezone.now()).astimezone(EASTERN)
    return now_et.weekday() < 5 and MARKET_OPEN <= now_et.time() < MARKET_CLOSE


def option_expired(expiration: date, now: datetime | None = None) -> bool:
    """True once the contract expiring on `expiration` has stopped
    trading: strictly past its expiration date, or ON its expiration date
    at/after the 4:00pm ET close. A same-day (0DTE) contract trades
    normally right up to that close, same as any other option on any
    other day -- this only catches the moment after."""
    now_et = (now or timezone.now()).astimezone(EASTERN)
    if expiration < now_et.date():
        return True
    return expiration == now_et.date() and now_et.time() >= MARKET_CLOSE
