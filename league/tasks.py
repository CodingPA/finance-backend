"""Celery tasks for the AI competitor wake-up loop. run_agent_wakeup is
triggered ONLY via apply_async(eta=...) from league.tools.schedule_wakeup
-- never by Celery Beat (mark_weekly_values, below, is the one Beat entry
this app adds, and it's unrelated scoreboard housekeeping). Each wake-up
rebuilds a fresh, bounded prompt from durable state (AgentScratchpad + live
portfolio + the wake-up's own reason) rather than replaying a growing
conversation -- the deliberate cost-control choice from this feature's
plan, closer to how persistent memory files work than to
assistant/tasks.py's growing-Conversation chat loop.

_run_wakeup branches on Competitor.assistant_model.provider: Anthropic
competitors run through _run_anthropic_turn (this module, a hand-rolled
loop over anthropic.beta.messages.parse -- NOT tool_runner, despite
tool_runner existing and being simpler, because prompt-cache breakpoints
need to move each round and tool_runner's loop gives no hook between its
internal append_messages and the next request to place one there); OpenAI
competitors run through
league.services.llm_openai.run_openai_turn (a hand-rolled loop over the
Responses API -- there's no OpenAI equivalent of tool_runner); Gemini
competitors run through league.services.llm_gemini.run_gemini_turn
(also hand-rolled, automatic_function_calling deliberately disabled for
parity with the OpenAI branch -- see that module's docstring). All three
return a provider-neutral LLMTurnResult that _run_wakeup turns into the
same AgentTurn regardless of which provider produced it."""

import logging
import zoneinfo
from datetime import date, datetime, time, timedelta
from decimal import Decimal

import anthropic
from celery import shared_task
from django.conf import settings
from django.db import IntegrityError
from django.utils import timezone

from assistant.models import AssistantModel
from companies.services import pricing, steady
from notifications.services import notify
from realtime.utils import send_to_user

from .models import (
    AccountValueSnapshot, AgentTickerMemory, AgentTurn, AgentWakeup, Competition, Competitor, DailyBrief,
    ForcedCheckin, Position, Trade, WeeklyMark,
)
from .services import budget, execution, llm_gemini, llm_openai, market_hours, provider_errors, valuation, wakeup_spacing
from .services.llm_common import LLMTurnResult
from .tools import LEAGUE_MAX_TOOL_ITERATIONS, LEAGUE_MAX_WAKEUPS_PER_DAY, _WebSearchTool, build_league_tools

logger = logging.getLogger(__name__)

EASTERN = zoneinfo.ZoneInfo("America/New_York")
_MARKET_OPEN = time(9, 30)
_MARKET_CLOSE = time(16, 0)

# How far past a ForcedCheckin's own (hour, minute) dispatch_forced_
# checkins will still catch it up today -- long enough to absorb a real
# worker backlog or a short outage, short enough that a slot missed by
# more than this doesn't surface hours later mixed in with whatever's
# actually due right now, and that creating a brand-new ForcedCheckin (or
# Competition) mid-afternoon doesn't fire its already-past-today morning
# slot immediately. See dispatch_forced_checkins' own docstring.
CHECKIN_CATCH_UP_WINDOW = timedelta(hours=2)

# The daily floor wake-up schedule used to be a fixed module-level list
# (3 slots: 8am news, 9:35am open, 3:15pm pre-close) applied identically
# to every competition. It's now per-Competition data instead --
# league.models.ForcedCheckin, admin-editable inline on CompetitionAdmin
# -- so an operator can tune how many guaranteed (i.e. always-billed) LLM
# calls per day a given competition's cost budget actually wants, rather
# than every competition paying for the same 3. See ForcedCheckin's own
# docstring and DEFAULT_FORCED_CHECKINS (the new default: one 8am news
# scan) for the 2026-08-13 strategy change this replaced.


def _headlines_block(limit: int = 8) -> str:
    """Today's top general-market headlines, formatted for direct inclusion
    in a DailyBrief -- fetched here rather than left to the agent's own
    get_market_news tool call, so an inject_news slot's news read can't be
    skipped. get_news has no Tradier equivalent (pricing.py's docstring),
    so this stays a direct steady.get_news call rather than going through
    pricing.py -- but that means a Tradier-only deployment (no
    STEADY_API_TOKEN) can't get headlines here at all. Said explicitly
    below instead of folding into the same "couldn't fetch" message a
    transient failure gets, since a misconfigured deployment is a
    different problem than a flaky one. Any other failure (Steady down,
    network error) is still swallowed: a missed headline fetch shouldn't
    fail the whole brief (or the wake-up that's waiting on it) when the
    agent can still call get_market_news itself as a fallback."""
    if not settings.STEADY_API_TOKEN:
        return "(Headlines aren't available on this deployment -- STEADY_API_TOKEN isn't configured.)"
    try:
        news = steady.get_news(None)
    except Exception:
        logger.exception("_headlines_block: get_news failed")
        news = None
    if not news:
        return "(Couldn't fetch headlines automatically -- use get_market_news if you need them.)"
    lines = []
    for item in news[:limit]:
        bits = [item.get("title") or "(no title)"]
        if item.get("source"):
            bits.append(f"[{item['source']}]")
        if item.get("ago"):
            bits.append(f"({item['ago']})")
        lines.append("- " + " ".join(bits))
    return "\n".join(lines)


# Broad-market ETFs snapshotted into every DailyBrief -- deliberately just
# the index funds already relevant to this league's own 30%-index-cap rule
# (Competition.max_index_pct), not an attempt at a full macro dashboard.
_BRIEF_INDEX_TICKERS = ["SPY", "QQQ", "DIA", "IWM"]


def _market_snapshot_block() -> str:
    """One line per ticker in _BRIEF_INDEX_TICKERS -- last price and percent
    change, via pricing.py (whichever of Steady/Tradier PRICING_PROVIDER
    selects -- same dispatch valuation.py and execution.py use for every
    other live quote, quotes/chains being the one capability both
    providers share). Used to call steady.get_quote directly, which meant
    a Tradier-only deployment (STEADY_API_TOKEN unset) silently lost the
    market snapshot from every daily brief with no error -- exactly the
    single-token operator pricing.py's own docstring says this project
    targets. Failures are still swallowed per-ticker (a partial snapshot
    beats none) and this returns "" outright if every quote came back
    empty/unconfigured, same graceful-degradation convention as
    _headlines_block -- _generate_brief_content below just omits the
    "Broad market" section entirely when this is blank."""
    lines = []
    for ticker in _BRIEF_INDEX_TICKERS:
        try:
            quote = pricing.get_quote(ticker, asset_class="ETF")
        except Exception:
            logger.exception("_market_snapshot_block: get_quote failed for %s", ticker)
            quote = None
        if not quote or quote.get("last_price") is None:
            continue
        pct = quote.get("percent_change_pct")
        pct_str = f"{pct:+.2f}%" if pct is not None else "n/a"
        lines.append(f"- {ticker}: {quote['last_price']} ({pct_str})")
    return "\n".join(lines)


def _generate_brief_content(slot: str) -> str:
    """The actual text stored on one DailyBrief row: headlines plus a
    broad-market snapshot. `slot` isn't used yet to vary the content by
    time of day (a "morning" and a "noon" brief are built identically
    today) -- kept as a parameter so a future slot-specific angle (e.g.
    noon leading with the morning session's move instead of overnight
    headlines) has somewhere to hook in without changing every caller."""
    parts = [f"Headlines:\n{_headlines_block()}"]
    market = _market_snapshot_block()
    if market:
        parts.append(f"Broad market:\n{market}")
    return "\n\n".join(parts)


def get_or_create_daily_brief(slot: str, today: date) -> str:
    """The shared brief content for (today, slot) -- generated once and
    reused by every competitor, in every competition, whose ForcedCheckin
    lands on this slot, instead of each one independently hitting Steady
    for the same headlines/quotes. This is the actual point of DailyBrief:
    turning N competitors' worth of news/quote fetches into 1 per slot per
    day. get_or_create's IntegrityError (two Beat ticks, or two competitions'
    rows for the same slot processed concurrently, both racing to create
    the same not-yet-existing row) is caught and re-fetched rather than
    left to bubble -- the loser of the race just reads what the winner
    wrote, same content either way."""
    try:
        brief, _ = DailyBrief.objects.get_or_create(
            date=today, slot=slot, defaults={"content": _generate_brief_content(slot)},
        )
    except IntegrityError:
        brief = DailyBrief.objects.get(date=today, slot=slot)
    return brief.content


def _format_et_time(hour: int, minute: int) -> str:
    period = "am" if hour < 12 else "pm"
    display_hour = hour % 12 or 12
    return f"{display_hour}:{minute:02d}{period} ET"


def _forced_checkins_rule_line(competition) -> str:
    """The "guaranteed floor wake-ups" bullet in _system_prompt -- built
    from this competition's own ForcedCheckin rows (admin-editable, see
    that model's docstring) rather than a fixed count/list of times, so
    the prompt always tells the agent the truth about what it's actually
    getting for free vs. what it needs to schedule_wakeup itself."""
    checkins = list(competition.forced_checkins.all())
    if not checkins:
        return (
            "- You have NO guaranteed daily wake-ups configured for this competition. Market "
            "open, the close, news, everything -- if you want to be woken up for it, YOU must "
            "schedule_wakeup for it yourself, and every one of those counts against your daily "
            "wake-up cap. Nothing happens automatically here.\n\n"
        )
    times = ", ".join(f"~{_format_et_time(c.hour, c.minute)} ({c.label})" for c in checkins)
    plural = "wake-up" if len(checkins) == 1 else "wake-ups"
    return (
        f"- You're guaranteed at least {len(checkins)} {plural} every trading day for free, "
        f"regardless of what you schedule yourself: {times}. These floor wake-ups don't count "
        "against your daily scheduling cap, and skipping them on purpose isn't possible.\n"
        "- IMPORTANT: that floor is ALL you get for free. Market open, the close, an earnings "
        "date, a level you want to watch -- anything not listed above requires YOU to "
        "schedule_wakeup for it, and unlike the floor wake-ups, a self-scheduled market-open or "
        "pre-close check costs a slot against your daily cap exactly like any other wake-up you "
        "schedule. Don't assume one of those will happen automatically just because it used to "
        "be guaranteed -- it isn't anymore unless it's in the list above. Don't burn a slot on a "
        "check that isn't worth it either, just to avoid a long gap.\n\n"
    )


def _budget_rule_line(competitor) -> str:
    """The one-time explanation of what the budget numbers in every wake-up's
    _budget_block actually mean -- kept out of that per-wake-up block (which
    stays terse/numeric for prompt-cache efficiency, see _run_anthropic_turn's
    cache_control comments) and put here instead, in the system prompt, which
    is itself cached across every round of a turn and every future turn.
    Returns "" (nothing) when neither budget field is set, matching
    _forced_checkins_rule_line's sibling pattern of only mentioning a feature
    that's actually configured -- an agent with no budget configured
    shouldn't be told to think about one."""
    if competitor.total_budget_usd is None and competitor.weekly_budget_usd is None:
        return ""
    lines = []
    if competitor.total_budget_usd is not None:
        lines.append(
            "- You have a total API cost budget for this competition. A live readout (spent, "
            "remaining, average cost per wake-up, projected runway) is given to you every "
            "wake-up. Crossing it pauses you outright -- there is no way to un-pause yourself, "
            "only an operator can. Manage your own cadence as you approach it: schedule fewer, "
            "more-valuable check-ins rather than burning wake-ups on marginal ones.\n"
        )
    if competitor.weekly_budget_usd is not None:
        lines.append(
            "- You also get a weekly spending guide (trading week, Monday-Sunday ET). It's "
            "advisory only -- nothing stops you from going over it, and being under it isn't "
            "banked for later. Use it purely to judge whether your pace this week makes sense.\n"
        )
    lines.append("\n")
    return "".join(lines)


def _after_hours_rule_line(competition) -> str:
    """"" (nothing) when Competition.restrict_after_hours_trading isn't
    set -- same conditional-silence convention as _forced_checkins_rule_
    line/_budget_rule_line, no point telling an unaffected competitor about
    a restriction that doesn't apply to it. Set: submit_trade queues
    (Trade.STATUS_PENDING) instead of filling while the market's closed --
    see league.services.execution.submit_trade/resolve_pending_trade."""
    if not competition.restrict_after_hours_trading:
        return ""
    return (
        "- This account can't trade after hours (simulating a broker without extended-hours "
        "support). A trade submitted while the market is closed queues instead of filling -- "
        "it'll be evaluated at a live quote the next time the market opens, not right now. "
        "Check the 'Orders still queued' section below before resubmitting one, or call "
        "cancel_trade on it if it no longer makes sense to wait for.\n"
    )


def _system_prompt(competitor) -> str:
    competition = competitor.competition
    index_list = ", ".join(competition.index_tickers) or "none configured"
    rules = (
        f'You are a competitor ("{competitor.name}") in a paper-trading competition, '
        f'"{competition.name}". This is 100% paper money -- no real trades, ever.\n\n'
        "Rules:\n"
        f"- Max {competition.max_position_pct:.0%} of your CURRENT account value in any single "
        "position, checked at entry.\n"
        f"- Max {competition.max_index_pct:.0%} of account value in index-fund SHARES "
        f"({index_list}) -- this does not apply to options on those tickers.\n"
        "- Buy fills at the live ask, sell fills at the live bid -- never mid.\n"
        f"- Uninvested cash earns {competition.cash_apy:.2%} APY (SPAXX), accrued weekly.\n"
        "- No margin, no naked/uncovered short selling. The ONLY way to open a short "
        "position is sell_to_open a covered call (against shares you own) or a "
        "cash-covered put (collateralized by your own cash).\n"
        "- Every trade needs a written thesis.\n"
        f"{_after_hours_rule_line(competition)}"
        f"{_forced_checkins_rule_line(competition)}"
        f"{_budget_rule_line(competitor)}"
    )
    if competitor.system_prompt:
        rules += competitor.system_prompt
    return rules


def _portfolio_snapshot(competitor) -> str:
    positions = list(competitor.positions.all())
    lines = [
        f"Cash: ${competitor.cash_balance}",
        f"Account value: ${valuation.account_value(competitor)}",
    ]
    if not positions:
        lines.append("No open positions.")
    for p in positions:
        if p.instrument_type == Position.INSTRUMENT_STOCK:
            label = p.ticker
        else:
            label = f"{p.ticker} {p.strike}{p.instrument_type[0].upper()} exp {p.expiration}"
        lines.append(f"{'-' if p.is_short else '+'}{p.quantity} {label} @ avg ${p.avg_cost}")
    return "\n".join(lines)


def _budget_block(competitor) -> str:
    """Live spend/budget readout, rebuilt fresh every wake-up straight from
    AgentTurn.cost_usd (league.services.budget.snapshot) -- same "no
    conversation memory, rebuild from durable state" reasoning as
    _portfolio_snapshot/_pending_wakeups_block above. Kept numeric and terse
    (the one-time explanation of what these numbers mean lives in
    _budget_rule_line instead, in the cached system prompt) so this doesn't
    bloat every single wake-up's uncached user turn.

    "" (nothing) when neither budget field is set -- matches
    _budget_rule_line's sibling silence, no point printing a stats block for
    numbers that don't exist."""
    if competitor.total_budget_usd is None and competitor.weekly_budget_usd is None:
        return ""
    snap = budget.snapshot(competitor)
    lines = [f"Spent so far (all-time): ${snap.total_spent:.4f}"]
    if snap.total_budget is not None:
        lines[-1] += (
            f" of ${snap.total_budget:.2f} total budget (${snap.total_remaining:.4f} left -- "
            "HARD CAP, you're auto-paused once this hits zero)."
        )
    else:
        lines[-1] += " (no total budget configured)."

    week_line = f"This trading week (Mon-Sun ET): ${snap.week_spent:.4f}"
    if snap.weekly_budget is not None:
        week_line += f" of a ${snap.weekly_budget:.2f}/week pace guide -- advisory only."
    else:
        week_line += " (no weekly guide configured)."
    lines.append(week_line)

    lines.append(f"Today: ${snap.today_spent:.4f}.")

    if snap.avg_cost_per_turn is not None:
        line = f"Average cost per wake-up so far: ${snap.avg_cost_per_turn:.4f}"
        if snap.remaining_calls_estimate is not None:
            line += f" (about {snap.remaining_calls_estimate} more wake-ups left in your total budget at that rate)."
        else:
            line += "."
        lines.append(line)

    if snap.projected_week_total is not None:
        lines.append(f"At this week's pace so far, you're on track for about ${snap.projected_week_total:.2f} by week's end.")

    if snap.projected_runway_days is not None:
        lines.append(
            f"At your overall average spend rate (${snap.daily_avg_overall:.4f}/day), your total "
            f"budget has about {snap.projected_runway_days:.1f} days of runway left "
            f"({snap.competition_days_left} days remain in the competition)."
        )

    return "\n".join(lines)


def _scratchpad_content(competitor) -> str:
    scratchpad = getattr(competitor, "scratchpad", None)
    if scratchpad is None or not scratchpad.content:
        return "(nothing saved yet)"
    return scratchpad.content


def _ticker_memory_index(competitor) -> str:
    """Which tickers have a saved AgentTickerMemory note -- NOT the note
    content itself (that's pulled on demand via get_ticker_memories, see
    that tool's own docstring for why: unlike the scratchpad, this is
    meant to scale across many tickers, most of which aren't relevant to
    any given wake-up). Injected into the wake-up prompt as just a ticker
    list so the agent actually knows this exists and has something to
    pull, rather than only discovering it by choosing to call
    list_ticker_memories with no prompt-level reason to."""
    tickers = list(AgentTickerMemory.objects.filter(competitor=competitor).values_list("ticker", flat=True))
    if not tickers:
        return "(none yet)"
    return ", ".join(tickers)


def _format_timedelta(td: timedelta) -> str:
    total_minutes = max(0, int(td.total_seconds() // 60))
    hours, minutes = divmod(total_minutes, 60)
    if hours and minutes:
        return f"{hours}h {minutes}m"
    return f"{hours}h" if hours else f"{minutes}m"


def next_occurrence(hour: int, minute: int, now_et: datetime) -> datetime:
    """Next America/New_York wall-clock occurrence of (hour, minute) on a
    weekday, at or after now_et -- weekends skipped, market holidays not
    (same known gap noted throughout this module). Generalizes what used
    to be hardcoded to 9:30am only; reused for both market-open display
    (_time_context_block) and computing each ForcedCheckin row's next
    firing time (league.views.CompetitorNextWakeupsView)."""
    target = time(hour, minute)
    candidate_date = now_et.date()
    if now_et.weekday() >= 5 or now_et.time() >= target:
        candidate_date += timedelta(days=1)
    while candidate_date.weekday() >= 5:
        candidate_date += timedelta(days=1)
    return datetime.combine(candidate_date, target, tzinfo=EASTERN)


def _next_market_open(now_et: datetime) -> datetime:
    return next_occurrence(_MARKET_OPEN.hour, _MARKET_OPEN.minute, now_et)


def _time_context_block(now_et: datetime) -> str:
    """Explicit day/time/market-status context for every wake-up prompt --
    without this, the only time signal in the prompt was a bare UTC ISO
    timestamp, leaving the model to mentally convert to ET and work out
    market hours itself before it could judge whether a schedule_wakeup
    time it's about to pick actually makes sense (e.g. not scheduling a
    "market open check" for a Saturday). NYSE regular hours only
    (9:30am-4:00pm ET, Mon-Fri) -- market holidays aren't accounted for,
    same known gap as FORCED_CHECKINS/dispatch_forced_checkins.

    ET only, deliberately, in two formats (readable + ISO) rather than
    ET-and-UTC side by side -- confirmed live on a real wake-up: a model
    given both grabbed the UTC clock number and mislabeled it "ET" in its
    own scratchpad summary (prompt correctly said "08:18PM ET
    (...T00:18:34+00:00 UTC)"; the model wrote "12:18am ET", the UTC
    number with the wrong zone's name on it). It never needs UTC anyway --
    schedule_wakeup already wants an ET-offset ISO string -- so
    now_et.isoformat() (offset baked in) replaces the old
    timezone.now().isoformat() UTC value, removing the second timezone
    entirely instead of trying to word the labels more carefully."""
    is_open = market_hours.is_market_open(now_et)
    if is_open:
        closes_at = now_et.replace(hour=_MARKET_CLOSE.hour, minute=_MARKET_CLOSE.minute, second=0, microsecond=0)
        market_status = f"Market is OPEN now, closes 4:00pm ET (in {_format_timedelta(closes_at - now_et)})."
    else:
        next_open = _next_market_open(now_et)
        market_status = (
            f"Market is CLOSED right now. Next open: {next_open.strftime('%A %Y-%m-%d %I:%M%p ET')} "
            f"(in {_format_timedelta(next_open - now_et)}, excluding holidays -- this doesn't check "
            f"for market holidays, so verify one doesn't fall in between if it matters)."
        )
    return (
        f"Right now: {now_et.strftime('%A, %Y-%m-%d %I:%M%p ET')} "
        f"({now_et.isoformat()}). {market_status}"
    )


def _pending_wakeups_block(competitor, exclude: AgentWakeup) -> str:
    """Every wake-up rebuilds its prompt from scratch (no conversation
    memory -- see this module's docstring), so without this an agent has
    no way to know it already has a future wake-up queued and will happily
    stack duplicates (confirmed live: Claude Gambling Daytrader scheduled
    two NVDA check-ins 35 minutes apart on the same day). Listing them here
    lets the agent actually decide whether to leave them, or schedule a
    better-timed replacement -- there's no cancel_wakeup tool, so "update"
    in practice means scheduling a new one and treating the stale one as
    superseded, not a real cancellation."""
    pending = AgentWakeup.objects.filter(
        competitor=competitor, status=AgentWakeup.STATUS_SCHEDULED, run_at__gt=timezone.now(),
    ).exclude(id=exclude.id).order_by("run_at")
    if not pending:
        return "(none)"
    return "\n".join(f"- {w.run_at.isoformat()}: {w.reason or '(no reason given)'}" for w in pending)


def _pending_orders_block(competitor) -> str:
    """Trade rows still STATUS_PENDING -- queued by execution.submit_trade
    because this competition restricts after-hours trading (Competition.
    restrict_after_hours_trading) and the market was closed at submission
    time, waiting on league.tasks.process_pending_trades to resolve them at
    the next open. "" (nothing, section omitted entirely) when there are
    none, same silence convention as _budget_block -- most wake-ups have
    nothing queued. Without this an agent has no way to know an order it
    already placed is still sitting unresolved and may resubmit a
    duplicate, same reasoning as _pending_wakeups_block above."""
    pending = Trade.objects.filter(competitor=competitor, status=Trade.STATUS_PENDING).order_by("created_at")
    if not pending:
        return ""
    lines = [
        f"- [id={t.id}] {t.side} {t.quantity} {t.instrument_type} {t.ticker} "
        f"(submitted {t.created_at.isoformat()}): {t.thesis}"
        for t in pending
    ]
    return (
        "Orders still queued (market was closed when submitted -- will fill/reject at the next open, "
        "no need to resubmit; call cancel_trade(trade_id) on one of the ids below if it no longer "
        "makes sense):\n" + "\n".join(lines) + "\n\n"
    )


def _with_trailing_cache_control(message: dict) -> dict:
    """Copy of `message` with a cache_control breakpoint on its last content
    block -- never mutates the stored message, and never written back into
    the canonical `messages` list (see _run_anthropic_turn). Only ever
    called on a message whose content is a plain list of dicts we built
    ourselves (the initial user turn, or a tool_result round) -- the
    growing `messages` list only ever ends with an assistant message (raw
    SDK content-block objects, not dicts) on the final round, where no
    further request is made and this is never called."""
    content = message["content"]
    return {**message, "content": [*content[:-1], {**content[-1], "cache_control": {"type": "ephemeral"}}]}


def _run_anthropic_turn(competitor, system_prompt: str, user_content: str) -> LLMTurnResult:
    if not settings.ANTHROPIC_API_KEY:
        raise RuntimeError("ANTHROPIC_API_KEY isn't configured.")

    client = anthropic.Anthropic(api_key=settings.ANTHROPIC_API_KEY)
    tools = build_league_tools(competitor)
    tools_by_name = {tool.name: tool for tool in tools}

    # One cache_control breakpoint on the last tool definition caches the
    # whole `tools` array (identical every round of this turn, and across
    # every other turn this competitor ever takes) -- cheap to write once,
    # then a near-free read on every subsequent round instead of repricing
    # ~3k tokens of tool schemas from scratch each time.
    tool_defs = [tool.to_dict() for tool in tools] + [_WebSearchTool().to_dict()]
    tool_defs[-1] = {**tool_defs[-1], "cache_control": {"type": "ephemeral"}}

    messages = [{"role": "user", "content": [{"type": "text", "text": user_content}]}]
    transcript = [{"role": "user", "text": user_content}]

    total_input_tokens = total_output_tokens = 0
    total_cache_creation = total_cache_read = 0
    tool_calls: list[str] = []

    for _ in range(LEAGUE_MAX_TOOL_ITERATIONS):
        # A trailing breakpoint on the growing `messages` list is what
        # actually matters for cost here -- tool results (get_market_news's
        # dump especially) otherwise get repriced at full rate on every
        # later round of the same turn. Applied to a per-request copy only:
        # Anthropic caps a request at 4 marked blocks total, so leaving a
        # marker permanently in `messages` would stack a new one each round
        # until a request got rejected outright. The API itself recognizes
        # the unchanged prefix from the previous round's breakpoint as a
        # cache hit, so this "moving" marker is the standard pattern, not a
        # workaround -- each round reads everything before its new tail and
        # only pays the write rate for what's actually new.
        request_messages = [*messages[:-1], _with_trailing_cache_control(messages[-1])] if messages else messages

        message = client.beta.messages.parse(
            model=competitor.assistant_model.model_id,
            max_tokens=4096,
            system=[{"type": "text", "text": system_prompt, "cache_control": {"type": "ephemeral"}}],
            tools=tool_defs,
            messages=request_messages,
        )

        usage = message.usage
        total_input_tokens += usage.input_tokens
        total_output_tokens += usage.output_tokens
        total_cache_creation += usage.cache_creation_input_tokens or 0
        total_cache_read += usage.cache_read_input_tokens or 0

        # model_dump() rather than hand-picking fields per block.type: a
        # round can contain more block types than plain text/tool_use --
        # confirmed live, server_tool_use + web_search_tool_result appear
        # for a real web_search call, and hand-picking only text/tool_use
        # silently dropped both from the transcript (and dropped
        # "web_search" from tool_calls) until this was caught by actually
        # triggering a live search rather than trusting the type contract.
        round_blocks = []
        tool_use_blocks = []
        for block in message.content:
            round_blocks.append(block.model_dump())
            if block.type in ("tool_use", "server_tool_use"):
                tool_calls.append(block.name)
            if block.type == "tool_use":
                tool_use_blocks.append(block)
        transcript.append({"role": "assistant", "content": round_blocks})

        # Raw SDK content-block objects, same as this turn's own message --
        # matches how anthropic's own tool_runner feeds a response back into
        # the next request (_beta_runner.py's append_messages), not
        # model_dump()'d like the transcript copy above.
        messages.append({"role": "assistant", "content": message.content})
        if not tool_use_blocks:
            break

        results = []
        for block in tool_use_blocks:
            tool = tools_by_name.get(block.name)
            if tool is None:
                results.append({
                    "type": "tool_result", "tool_use_id": block.id,
                    "content": f"Error: Tool '{block.name}' not found", "is_error": True,
                })
                continue
            try:
                results.append({"type": "tool_result", "tool_use_id": block.id, "content": tool.call(block.input)})
            except Exception as exc:
                results.append({"type": "tool_result", "tool_use_id": block.id, "content": repr(exc), "is_error": True})
        messages.append({"role": "user", "content": results})
    else:
        logger.warning("_run_anthropic_turn: hit LEAGUE_MAX_TOOL_ITERATIONS for competitor %s", competitor.id)

    return LLMTurnResult(
        transcript=transcript, input_tokens=total_input_tokens, output_tokens=total_output_tokens,
        cache_creation_input_tokens=total_cache_creation, cache_read_input_tokens=total_cache_read,
        tool_calls=tool_calls,
    )


def _run_wakeup(
    wakeup: AgentWakeup, trigger: str = AgentTurn.TRIGGER_SCHEDULED, news_content: str = "",
) -> AgentTurn:
    competitor = wakeup.competitor
    if competitor.assistant_model_id is None:
        raise RuntimeError(f"{competitor.name} has no assistant_model configured.")

    intro = {
        AgentTurn.TRIGGER_MANUAL: (
            "Manual check-in -- the competition operator woke you up directly (not one you scheduled)."
        ),
        AgentTurn.TRIGGER_FORCED: (
            "Forced daily check-in -- one of this competition's guaranteed floor wake-ups "
            "(see Reason below for which one), not one you scheduled yourself and not counted "
            "against your daily scheduling cap."
        ),
    }.get(trigger, "Scheduled check-in -- this is the wake-up you asked for.")
    now_et = timezone.now().astimezone(EASTERN)
    news_section = f"Market brief (fetched for you, no tool call needed):\n{news_content}\n\n" if news_content else ""
    budget_text = _budget_block(competitor)
    budget_section = f"Budget:\n{budget_text}\n\n" if budget_text else ""
    user_content = (
        f"{intro}\n"
        f"{_time_context_block(now_et)}\n"
        f"Reason: {wakeup.reason or '(none given)'}\n\n"
        f"{news_section}"
        f"Your notes from last time:\n{_scratchpad_content(competitor)}\n\n"
        f"Saved ticker research notes (use get_ticker_memories to pull one or more): "
        f"{_ticker_memory_index(competitor)}\n\n"
        f"Current portfolio:\n{_portfolio_snapshot(competitor)}\n\n"
        f"{budget_section}"
        f"{_pending_orders_block(competitor)}"
        f"Your other already-scheduled future wake-ups:\n{_pending_wakeups_block(competitor, exclude=wakeup)}\n\n"
        "Decide what to do: check prices/news if useful, place trades if warranted, "
        "update your scratchpad with anything worth remembering. Before you finish, also "
        "check the list above and schedule_wakeup again if it's stale or missing, picking a "
        "specific ET time that actually makes sense given the day/market status above (not, "
        "say, a 'market open' check scheduled for a Saturday) -- do this every time, "
        "regardless of whether THIS check-in was one you scheduled, a manual one triggered "
        "from the operator/frontend, or a forced daily floor wake-up. There's no way to "
        "cancel an old one, so if it no longer makes sense just schedule a better one and "
        "treat the old one as superseded."
    )

    system_prompt = _system_prompt(competitor)
    provider = competitor.assistant_model.provider
    if provider == AssistantModel.PROVIDER_OPENAI:
        result = llm_openai.run_openai_turn(competitor, system_prompt, user_content)
    elif provider == AssistantModel.PROVIDER_GEMINI:
        result = llm_gemini.run_gemini_turn(competitor, system_prompt, user_content)
    else:
        result = _run_anthropic_turn(competitor, system_prompt, user_content)

    # OpenAI's and Gemini's input-token counts both INCLUDE cached tokens
    # (billed at a steep discount, not full price, not free); Anthropic's
    # EXCLUDES cache reads (tracked separately in cache_read_input_tokens).
    # Costing the raw input_tokens for those two providers here would price
    # their cached tokens at full rate, making cost_usd not apples-to-apples
    # with an Anthropic competitor on the scoreboard -- so the effective
    # input count is reduced by the cache read for both, while
    # AgentTurn.input_tokens below still stores the full raw count for audit
    # fidelity. This assumption comes from each provider's own field
    # naming/docs, not a confirmed live-traffic check -- worth verifying
    # against a real bill from each if the numbers ever look off on the
    # scoreboard.
    cache_adjusted = provider in (AssistantModel.PROVIDER_OPENAI, AssistantModel.PROVIDER_GEMINI)
    billed_input = result.input_tokens - result.cache_read_input_tokens if cache_adjusted else result.input_tokens
    # Anthropic's usage.input_tokens already EXCLUDES both cache_creation
    # and cache_read tokens (unlike OpenAI/Gemini, which fold cache reads
    # into input_tokens and get the billed_input reduction above instead) --
    # they're billed here as their own line items, at cost_for's 1.25x/0.1x
    # multipliers, or a caching turn would silently look cheaper than it
    # really was (cache reads/writes vanishing from the count entirely
    # rather than being priced at their real, non-zero rate).
    cost = competitor.assistant_model.cost_for(
        billed_input, result.output_tokens,
        cache_creation_tokens=0 if cache_adjusted else result.cache_creation_input_tokens,
        cache_read_tokens=0 if cache_adjusted else result.cache_read_input_tokens,
    )

    return AgentTurn.objects.create(
        competitor=competitor, wakeup=wakeup, trigger=trigger,
        transcript=result.transcript,
        input_tokens=result.input_tokens, output_tokens=result.output_tokens,
        cache_creation_input_tokens=result.cache_creation_input_tokens,
        cache_read_input_tokens=result.cache_read_input_tokens,
        cost_usd=cost, tool_calls=result.tool_calls,
    )


# How soon a competitor gets another shot at scheduling itself after a
# turn that ended with NO future wake-up pending at all -- see
# _ensure_future_wakeup_scheduled. Short enough that a day-trading
# competitor mid-session doesn't lose a same-day window (the incident
# this exists for: a competitor's first-ever wake-up stated it wanted to
# check back same-day at 3:30pm, then exhausted LEAGUE_MAX_TOOL_ITERATIONS
# before ever reaching its own schedule_wakeup call, and got nothing
# until the next day's 8am floor instead), long enough not to spam the
# LLM API retrying every couple minutes.
SAFETY_NET_WAKEUP_DELAY = timedelta(minutes=15)


def _ensure_future_wakeup_scheduled(competitor: Competitor) -> None:
    """Safety net for a turn that ended without the competitor having ANY
    future wake-up pending -- most commonly because it exhausted
    LEAGUE_MAX_TOOL_ITERATIONS (6 rounds, same cap on every provider) mid-
    decision, before ever reaching its own schedule_wakeup call. All three
    provider loops (this module's _run_anthropic_turn,
    llm_openai.run_openai_turn, llm_gemini.run_gemini_turn) just stop when
    the cap is hit -- there's no forced final "wrap up and schedule"
    round -- so without this, a competitor whose turn ran out of budget
    goes dark until its competition's next ForcedCheckin (often just once
    a day), not merely delayed a few minutes.

    Called after a COMPLETED turn, or a FAILED one that's neither TERMINAL
    nor part of a REPEATED failure streak (see run_agent_wakeup below,
    provider_errors.is_terminal, _has_repeated_failures) -- either of
    those instead pauses the competitor outright, so scheduling a retry
    for it here would just get cancelled as a no-op the moment it fired
    (run_agent_wakeup's own is_paused check) while still cluttering the
    wake-up list. A genuinely transient failure (a 5xx, a dropped
    connection, ordinary rate limiting) is exactly the kind of thing a
    retry is likely to clear up within a call or two, so it gets the same
    safety net as a budget-exhausted COMPLETED turn -- but only up to
    CONSECUTIVE_FAILURE_PAUSE_THRESHOLD straight failures, past which it's
    treated as repeated instead (see that constant's own comment for the
    unbounded-retry-loop this bounds).

    Pure insurance, not a replacement for the agent's own scheduling
    discretion -- doesn't count against LEAGUE_MAX_WAKEUPS_PER_DAY (same
    reasoning as ForcedCheckin not counting against it: this is operator/
    system-mandated, not the agent's own choice) and never fires if the
    agent DID manage to schedule something, however far out.

    If the reason nothing is scheduled is that the competitor had ALREADY
    used today's full LEAGUE_MAX_WAKEUPS_PER_DAY budget (wakeups_today_
    count), the usual 15-minute delay is skipped in favor of timing this
    for just after the cap resets (wakeup_spacing.next_cap_reset) --
    scheduling 15 minutes out would just get refused by the same cap
    again, which is exactly the closed loop (cap refuses -> nothing
    scheduled -> safety net fires -> cap refuses again) that produced 17
    consecutive safety-net wake-ups for two competitors on 2026-08-25
    before the two cap fixes (excluding these very rows from the count,
    and this reset-aware retiming) went in."""
    has_pending = AgentWakeup.objects.filter(
        competitor=competitor, status=AgentWakeup.STATUS_SCHEDULED, run_at__gte=timezone.now(),
    ).exists()
    if has_pending:
        return

    now = timezone.now()
    if wakeup_spacing.wakeups_today_count(competitor, now) >= LEAGUE_MAX_WAKEUPS_PER_DAY:
        run_at = wakeup_spacing.next_cap_reset(now)
        reason = (
            f"{wakeup_spacing.SAFETY_NET_REASON_PREFIX} your last turn ended without any future "
            "wake-up scheduled, and you'd already used today's full wake-up cap -- this is timed "
            "for just after the cap resets (America/New_York midnight) instead of the usual 15 "
            "minutes, so it doesn't just get refused again. Make your next schedule_wakeup call "
            "early in the turn, before you risk running out of budget."
        )
    else:
        run_at = now + SAFETY_NET_WAKEUP_DELAY
        reason = (
            f"{wakeup_spacing.SAFETY_NET_REASON_PREFIX} your last turn ended without any future "
            "wake-up scheduled -- most likely you ran out of tool-call budget before calling "
            "schedule_wakeup, or the previous attempt hit a transient error. This isn't a "
            "substitute for your own scheduling: pick a real next check-in time and call "
            "schedule_wakeup early this turn, not last, so it doesn't get cut off again."
        )

    wakeup = AgentWakeup.objects.create(competitor=competitor, run_at=run_at, reason=reason)
    run_agent_wakeup.apply_async(args=[wakeup.id], eta=run_at)


# How many STATUS_FAILED turns in a row (most recent first, ignoring
# STATUS_SCHEDULED/CANCELLED housekeeping rows -- only actual outcomes
# count) turn a "transient" error into an effectively terminal one. A
# genuinely transient error (a dropped connection, ordinary rate
# limiting) clears up within a retry or two; one that hasn't after this
# many straight attempts is functionally permanent and needs a human, not
# another SAFETY_NET_WAKEUP_DELAY retry forever.
#
# Added after code review caught a real gap in the first version of the
# terminal-error/safety-net fix: excluding ForcedCheckin/safety-net rows
# from LEAGUE_MAX_WAKEUPS_PER_DAY (wakeup_spacing.wakeups_today_count) --
# necessary to fix the 2026-08-25 runaway loop -- also removed the ONE
# thing that had been (accidentally) bounding a persistent non-terminal
# failure: the daily cap itself. Without this, a misconfigured/missing
# API key (RuntimeError, not a provider exception provider_errors can
# classify -- see _run_anthropic_turn/llm_openai.run_openai_turn/
# llm_gemini.run_gemini_turn's own settings.*_API_KEY checks) or a Gemini
# quota exhaustion (deliberately classified transient by provider_errors,
# see that module's docstring) would retry every 15 minutes forever --
# the same shape as the incident this whole fix exists to prevent, minus
# the accidental brake.
CONSECUTIVE_FAILURE_PAUSE_THRESHOLD = 4


def _has_repeated_failures(competitor: Competitor) -> bool:
    """True if competitor's most recent CONSECUTIVE_FAILURE_PAUSE_THRESHOLD
    resolved turns (COMPLETED or FAILED; SCHEDULED/CANCELLED rows carry no
    outcome and are excluded) are ALL STATUS_FAILED -- see
    CONSECUTIVE_FAILURE_PAUSE_THRESHOLD's own comment for why this exists.
    A single COMPLETED turn anywhere in that window resets the streak to
    0, same as a real recovery should."""
    recent = AgentWakeup.objects.filter(
        competitor=competitor, status__in=(AgentWakeup.STATUS_FAILED, AgentWakeup.STATUS_COMPLETED),
    ).order_by("-completed_at")[:CONSECUTIVE_FAILURE_PAUSE_THRESHOLD]
    streak = 0
    for w in recent:
        if w.status != AgentWakeup.STATUS_FAILED:
            break
        streak += 1
    return streak >= CONSECUTIVE_FAILURE_PAUSE_THRESHOLD


@shared_task
def run_agent_wakeup(wakeup_id, trigger=AgentTurn.TRIGGER_SCHEDULED, news_content=""):
    """Wrapped in a broad try/except, mirroring
    companies/tasks.py::poll_wiki_sources' per-item error isolation: one
    bad wake-up (a transient Steady/Anthropic failure, a malformed tool
    call) must not corrupt AgentWakeup bookkeeping or block whatever the
    competitor scheduled next.

    `trigger` distinguishes an agent's own schedule_wakeup call (the
    default) from an operator manually firing a wake-up via the API
    (league.views.CompetitorWakeupTriggerView) -- purely for
    AgentTurn.trigger's audit value, doesn't change execution. `news_content`
    is the already-generated DailyBrief text, set by dispatch_forced_checkins
    for inject_news slots -- see get_or_create_daily_brief. Passed as a
    plain string (not fetched here) so N competitors sharing a slot share
    the SAME Steady fetch instead of each one repeating it.

    Skips outright if the wakeup is no longer STATUS_SCHEDULED, or gone
    entirely -- either way covers a wakeup superseded after its
    apply_async(eta=...) was already handed to the broker, which has no
    way to pull the task back out of Redis: enforce_wakeup_spacing only
    ever cancels (row still exists, caught by the status check below), but
    a row deleted outright (e.g. by hand via the admin) leaves the queued
    task with nothing to fetch -- confirmed live, that raised an unhandled
    AgentWakeup.DoesNotExist and a Celery ERROR log instead of the same
    quiet no-op a cancellation gets.

    After a COMPLETED turn, or a FAILED one whose error is neither
    TERMINAL (provider_errors.is_terminal) nor part of a REPEATED failure
    streak (_has_repeated_failures), _ensure_future_wakeup_scheduled
    backstops a competitor that finished without scheduling anything for
    itself (see that function's docstring). A TERMINAL failure (a
    revoked API key, an account with no credits left -- confirmed live
    for both: an anthropic.AuthenticationError and an openai.
    RateLimitError with an insufficient_quota body, 2026-08-25/26) or a
    REPEATED one (CONSECUTIVE_FAILURE_PAUSE_THRESHOLD straight failures --
    catches a misconfigured API key, a Gemini quota exhaustion, or any
    other error provider_errors can't positively classify as terminal but
    that isn't actually clearing up) instead pauses the competitor via
    Competitor.is_paused and skips the safety net entirely, since
    retrying the exact same call yet again would just fail the exact same
    way. Before the terminal-error fix, ANY failure instead broke the
    self-scheduling chain silently: the only thing left firing was the
    next ForcedCheckin floor slot, which failed too, so two competitors
    went dark for multiple trading days with no operator-visible signal
    beyond a same generic notification repeated once a day. Before the
    repeated-failure fix (same review pass, a gap caught before it ever
    shipped), a persistent non-terminal error would have retried every
    SAFETY_NET_WAKEUP_DELAY forever instead."""
    try:
        wakeup = AgentWakeup.objects.select_related(
            "competitor", "competitor__assistant_model", "competitor__competition",
        ).get(id=wakeup_id)
    except AgentWakeup.DoesNotExist:
        logger.info("run_agent_wakeup: skipping wakeup %s, row no longer exists", wakeup_id)
        return
    competitor = wakeup.competitor

    if wakeup.status != AgentWakeup.STATUS_SCHEDULED:
        logger.info(
            "run_agent_wakeup: skipping wakeup %s for %s, status is %s (not scheduled)",
            wakeup.id, competitor.name, wakeup.status,
        )
        return

    if competitor.is_paused:
        # Catches a wake-up that was already queued via apply_async(eta=...)
        # (self-scheduled, or a forced check-in dispatched moments before
        # the pause) before an operator flipped Competitor.is_paused --
        # there's no way to pull it back out of the Celery broker (same
        # reasoning as enforce_wakeup_spacing's own STATUS_CANCELLED use),
        # so this is the actual backstop that makes pausing airtight, not
        # just "stops NEW ones." No LLM call happens -- this is the whole
        # point, since that's the cost dispatch_forced_checkins/manual
        # triggers exist to avoid running up.
        logger.info("run_agent_wakeup: skipping wakeup %s, %s is paused", wakeup.id, competitor.name)
        wakeup.status = AgentWakeup.STATUS_CANCELLED
        wakeup.completed_at = timezone.now()
        wakeup.save(update_fields=["status", "completed_at"])
        return

    if competitor.total_budget_usd is not None and budget.is_over_total_budget(competitor):
        # Same shape as the is_paused block above -- cancel without an LLM
        # call (that's the actual cost this exists to stop), pause the
        # competitor so every other is_paused check point (dispatch_forced_
        # checkins, schedule_wakeup's own future firings) stays airtight too,
        # and skip _ensure_future_wakeup_scheduled entirely: retrying would
        # just hit this same check again, the same reasoning
        # terminal_error/repeated_failure below already use to skip the
        # safety net for a dead credential.
        logger.info(
            "run_agent_wakeup: skipping wakeup %s, %s is over its total budget (cap $%s)",
            wakeup.id, competitor.name, competitor.total_budget_usd,
        )
        wakeup.status = AgentWakeup.STATUS_CANCELLED
        wakeup.completed_at = timezone.now()
        wakeup.save(update_fields=["status", "completed_at"])
        competitor.is_paused = True
        competitor.save(update_fields=["is_paused"])
        if competitor.user_id:
            spent = budget.snapshot(competitor).total_spent
            notify(
                competitor.user, "league.wakeup_failed",
                title=f"{competitor.name} paused -- total budget exhausted",
                body=(
                    f"Spent ${spent:.2f} of its ${competitor.total_budget_usd:.2f} total budget. "
                    "Raise the budget or leave it paused."
                ),
                payload={"competitor_id": competitor.id, "wakeup_id": wakeup.id},
            )
        return

    # Fire-and-forget, same shape/placement as companies/tasks.py::
    # screen_ticker's own send_to_user(..., "screen.progress", status=
    # "running") -- past every early-exit guard above (not scheduled,
    # paused, over budget), right as the actual LLM call is about to start,
    # so a frontend wake-up button can flip from "queued" to "thinking"
    # instead of sitting on the bare 202 from CompetitorWakeupsView.post
    # until wakeup_complete/wakeup_failed arrives, possibly a while later.
    # Not notify() -- a missed "started" ping costs nothing (the button
    # just stays disabled a little longer until the real completion
    # notification lands), so it isn't worth a Notification row + redelivery.
    if competitor.user_id:
        send_to_user(
            competitor.user_id, "league.wakeup_started",
            competitor_id=competitor.id, wakeup_id=wakeup.id,
        )

    trades_before = set(Trade.objects.filter(competitor=competitor).values_list("id", flat=True))
    terminal_error = False
    try:
        _run_wakeup(wakeup, trigger=trigger, news_content=news_content)
        wakeup.status = AgentWakeup.STATUS_COMPLETED
    except Exception as exc:
        logger.exception("run_agent_wakeup failed for wakeup %s (competitor %s)", wakeup.id, competitor.name)
        wakeup.status = AgentWakeup.STATUS_FAILED
        wakeup.error = provider_errors.describe_error(exc)
        terminal_error = provider_errors.is_terminal(exc)
    wakeup.completed_at = timezone.now()
    wakeup.save(update_fields=["status", "completed_at", "error"])

    # _has_repeated_failures reads AgentWakeup rows including this one, so
    # it must run after the save above -- otherwise the current failure
    # wouldn't count toward its own streak.
    repeated_failure = (
        wakeup.status == AgentWakeup.STATUS_FAILED and not terminal_error and _has_repeated_failures(competitor)
    )
    if (terminal_error or repeated_failure) and not competitor.is_paused:
        competitor.is_paused = True
        competitor.save(update_fields=["is_paused"])

    if wakeup.status == AgentWakeup.STATUS_COMPLETED or (
        wakeup.status == AgentWakeup.STATUS_FAILED and not terminal_error and not repeated_failure
    ):
        _ensure_future_wakeup_scheduled(competitor)

    if not competitor.user_id:
        return

    # Always notify on completion, not just when a trade happened -- a
    # manually-triggered "go research this" wake-up is useful to know
    # about even when it decides not to trade; a failure is worth
    # surfacing too, not just logged server-side.
    new_trades = Trade.objects.filter(competitor=competitor).exclude(id__in=trades_before)
    payload = {"competitor_id": competitor.id, "wakeup_id": wakeup.id}
    if wakeup.status == AgentWakeup.STATUS_FAILED:
        title = f"{competitor.name}'s wake-up failed"
        if terminal_error:
            title += " -- competitor PAUSED (needs an operator to fix the account)"
        elif repeated_failure:
            title += f" -- competitor PAUSED ({CONSECUTIVE_FAILURE_PAUSE_THRESHOLD} failures in a row)"
        notify(
            competitor.user, "league.wakeup_failed",
            title=title, body=wakeup.error or "Check server logs for details.", payload=payload,
        )
    elif new_trades.exists():
        summary = "; ".join(f"{t.side} {t.quantity} {t.ticker} ({t.status})" for t in new_trades)
        notify(competitor.user, "league.trade_activity", title=f"{competitor.name} traded", body=summary, payload=payload)
    else:
        notify(
            competitor.user, "league.wakeup_complete",
            title=f"{competitor.name} finished thinking", body="No trades this time.", payload=payload,
        )


@shared_task
def dispatch_forced_checkins():
    """Runs every 5 minutes (CELERY_BEAT_SCHEDULE) and fires a wake-up for
    every active, non-paused AI competitor (Competitor.is_paused -- an
    operator kill switch for API cost, see that field's docstring) in
    every running competition, at each of THAT competition's own
    ForcedCheckin rows whose (hour, minute) has already passed today, in
    America/New_York wall-clock time -- deliberately not a static UTC
    crontab (a competition's window can straddle a DST change, which
    would drift a fixed UTC hour off the intended ET time right when it
    matters). These are a floor, not a replacement for schedule_wakeup:
    an agent can still schedule its own check-ins on top (see
    _system_prompt), and unlike those, these don't count against
    LEAGUE_MAX_WAKEUPS_PER_DAY -- that cap is about the agent's own
    scheduling discretion, not an operator-mandated minimum. Always fires
    every matching row, even if the agent already has a self-scheduled
    wake-up around the same time -- simpler than trying to dedupe near a
    threshold, and a redundant check is cheap next to an agent going
    dark.

    Matching is "has today's slot time passed (but not too long ago), and
    has it not already fired today" (checked per America/New_York
    calendar day via wakeup_spacing.et_day_bounds), NOT an exact
    `hour=X, minute=Y` equality against the instant this task happens to
    execute. The previous exact-equality version had two real failure
    modes, both fixed by switching to a passed/not-yet-fired check: (1) a
    ForcedCheckin whose minute isn't a multiple of 5 could never match ANY
    tick of this Beat schedule's crontab(minute="*/5") and would silently
    never fire, ever; (2) a tick that ran even ~60s late (worker backlog,
    --autoscale spin-up under load) would miss the one exact minute and
    skip the slot for the whole day with no catch-up, since the old guard
    was only a rolling 4-minute "did I already create this in the last
    few minutes" window, not "did today's slot already happen." Now any
    row whose time has passed within CHECKIN_CATCH_UP_WINDOW simply fires
    on the very next tick, at most 5 minutes late in the common case, and
    the per-competitor day-scoped "already fired today" check (not a
    rolling window) is what prevents re-firing it for the rest of the
    day.

    The catch-up is bounded by CHECKIN_CATCH_UP_WINDOW rather than
    unlimited, for two reasons caught in review before this shipped:
    creating a brand-new ForcedCheckin (or Competition -- CompetitionList
    CreateView.post/seed_league both seed DEFAULT_FORCED_CHECKINS with an
    8am row) at, say, 2pm would otherwise fire its already-past-today 8am
    slot for every AI competitor IMMEDIATELY on the next tick instead of
    waiting for tomorrow; and a Beat/worker outage longer than the window
    means a stale morning slot doesn't surface hours later mixed in with
    whatever slot is actually due right now. A row missed past the window
    is simply not caught up for the rest of that day -- logged, not
    silent -- and tries again at its normal time tomorrow.

    Market holidays aren't filtered (no trading-calendar source wired in
    yet) -- a holiday still gets a wake-up that'll just find stale/closed
    quotes. Harmless, just a wasted API call; weekends are skipped
    outright since they're free to detect from the date alone.

    An inject_news row's brief is fetched at most once per distinct
    news_slot per tick (brief_cache below), and only once at least one
    competitor is actually pending for that row -- multiple competitions
    with a ForcedCheckin sharing the same news_slot (e.g. every
    competition's default "morning" 8am row) all read the SAME generated
    DailyBrief, so N competitors' worth of Steady calls collapses to 1 per
    slot per day rather than N. See get_or_create_daily_brief."""
    now_et = timezone.now().astimezone(EASTERN)
    if now_et.weekday() >= 5:
        return
    today = now_et.date()
    day_start, _day_end = wakeup_spacing.et_day_bounds(now_et)

    checkins = ForcedCheckin.objects.filter(
        competition__start_date__lte=today, competition__end_date__gte=today,
    ).select_related("competition")
    brief_cache: dict[str, str] = {}
    for checkin in checkins:
        slot_at = now_et.replace(hour=checkin.hour, minute=checkin.minute, second=0, microsecond=0)
        if slot_at > now_et:
            continue  # today's slot hasn't happened yet
        if now_et - slot_at > CHECKIN_CATCH_UP_WINDOW:
            logger.warning(
                "dispatch_forced_checkins: %s's %02d:%02d ET '%s' slot is %s past due (beyond the %s catch-up "
                "window) -- not firing it today, will try again tomorrow",
                checkin.competition.name, checkin.hour, checkin.minute, checkin.label,
                now_et - slot_at, CHECKIN_CATCH_UP_WINDOW,
            )
            continue

        reason = f"{wakeup_spacing.FORCED_CHECKIN_REASON_PREFIX}{checkin.label}] {checkin.prompt}"
        competitors = list(Competitor.objects.filter(
            kind=Competitor.KIND_AI, is_paused=False, competition=checkin.competition,
        ))
        if not competitors:
            continue
        already_fired_ids = set(
            AgentWakeup.objects.filter(
                competitor__in=competitors, reason=reason, created_at__gte=day_start,
            ).values_list("competitor_id", flat=True)
        )
        pending = [c for c in competitors if c.id not in already_fired_ids]
        if not pending:
            continue

        news_content = ""
        if checkin.inject_news:
            slot = checkin.news_slot or f"{checkin.hour:02d}{checkin.minute:02d}"
            if slot not in brief_cache:
                brief_cache[slot] = get_or_create_daily_brief(slot, today)
            news_content = brief_cache[slot]

        for competitor in pending:
            wakeup = AgentWakeup.objects.create(competitor=competitor, run_at=timezone.now(), reason=reason)
            run_agent_wakeup.delay(wakeup.id, AgentTurn.TRIGGER_FORCED, news_content)


@shared_task
def process_pending_trades():
    """Drains every still-STATUS_PENDING Trade once the market is actually
    open -- a pending row only exists because Competition.
    restrict_after_hours_trading queued it (execution.submit_trade) while
    the market was closed at submission time. Ticks every 5 minutes
    (CELERY_BEAT_SCHEDULE), same DST-safe wall-clock-ET gating convention
    as dispatch_forced_checkins above -- a no-op tick outside market hours
    is one boolean check and no query, so there's no need for a once-daily-
    only guard the way ForcedCheckin has for its specific slot.

    One global FIFO queue (Trade.Meta.ordering is already created_at), not
    scoped per competition -- cross-competitor/competition processing order
    doesn't matter, since each trade only ever touches its own competitor's
    cash_balance/Position rows, no shared state between them. Per-row error
    isolation, same as companies/tasks.py::poll_wiki_sources and this
    module's own run_agent_wakeup: a provider outage mid-drain on one trade
    must not block the rest of the queue."""
    if not market_hours.is_market_open():
        return
    pending = Trade.objects.filter(status=Trade.STATUS_PENDING).select_related(
        "competitor__competition",
    ).order_by("created_at")
    for trade in pending:
        try:
            execution.resolve_pending_trade(trade)
        except Exception:
            logger.exception("process_pending_trades: failed to resolve trade %s", trade.id)


# How overdue a still-STATUS_SCHEDULED AgentWakeup must be before
# sweep_overdue_wakeups treats it as lost rather than merely running a
# little late -- comfortably longer than any realistic worker backlog
# (--autoscale spin-up, a burst of forced check-ins), so this never races
# a wake-up's own apply_async(eta=...) firing on time. run_agent_wakeup's
# own STATUS_SCHEDULED check is the only guard against double-firing one
# that's merely late rather than genuinely lost; this grace window is
# what makes that guard sufficient in practice instead of a real race.
OVERDUE_SWEEP_GRACE = timedelta(minutes=10)

# Past this age, re-firing an overdue wake-up means acting on a genuinely
# stale prompt instead of recovering a missed one -- "check SPY at 9:45am"
# recovered the next afternoon isn't a useful check-in, it's a confusing
# one, and this is also what stops a long-dead sweep task (or an extended
# outage) from dumping its entire backlog into the LLM the moment it
# starts running again. Cancelled instead of fired past this point.
MAX_SWEEP_AGE = timedelta(days=1)


@shared_task
def sweep_overdue_wakeups():
    """Beat safety net for AgentWakeup rows whose apply_async(eta=...)
    task the Celery/Redis broker silently dropped. Confirmed to happen in
    practice during the 2026-08-25/26 investigation (wakeup #192: still
    STATUS_SCHEDULED, run_at nearly a full day past, and absent from
    `celery -A core inspect scheduled`'s live ETA list -- i.e. genuinely
    gone from the broker, not just running late) -- the leading theory is
    a worker restart (deploy.sh's `systemctl restart finance-celery-
    worker` runs on every deploy) dropping an ETA task the worker was
    still holding in memory. Nothing else in this app ever queries
    run_at__lt on AgentWakeup, so without this a lost wake-up stays lost
    forever; CELERY_BROKER_TRANSPORT_OPTIONS's 14-day visibility_timeout
    (core/settings.py) means Redis's own unacked-message redelivery isn't
    a realistic backstop either, for a wake-up.

    Re-dispatches via .delay() (fires now, not another apply_async(eta=)
    for later) with the default TRIGGER_SCHEDULED -- the original trigger
    kind isn't recorded on AgentWakeup itself (only on the AgentTurn a
    successful run produces), and treating a lost wake-up as "the
    schedule you asked for, just late" is the closest approximation
    available. Skips a paused competitor's rows the same way a normal
    firing would (run_agent_wakeup's own is_paused check), so this can't
    revive a wake-up an operator paused for.

    A row overdue past MAX_SWEEP_AGE is cancelled instead of re-dispatched
    -- see that constant's own comment."""
    now = timezone.now()
    cutoff = now - OVERDUE_SWEEP_GRACE
    stale_cutoff = now - MAX_SWEEP_AGE
    overdue = AgentWakeup.objects.filter(status=AgentWakeup.STATUS_SCHEDULED, run_at__lt=cutoff)
    for wakeup in overdue:
        if wakeup.run_at < stale_cutoff:
            logger.warning(
                "sweep_overdue_wakeups: cancelling wakeup %s for competitor %s, run_at=%s is more than "
                "%s stale -- too old to usefully act on",
                wakeup.id, wakeup.competitor_id, wakeup.run_at, MAX_SWEEP_AGE,
            )
            wakeup.status = AgentWakeup.STATUS_CANCELLED
            wakeup.completed_at = now
            wakeup.save(update_fields=["status", "completed_at"])
            continue
        logger.warning(
            "sweep_overdue_wakeups: re-dispatching wakeup %s for competitor %s, run_at=%s (%s overdue)",
            wakeup.id, wakeup.competitor_id, wakeup.run_at, now - wakeup.run_at,
        )
        run_agent_wakeup.delay(wakeup.id)


@shared_task
def mark_weekly_values():
    """The one recurring Beat job this app adds (see
    core/settings.py::CELERY_BEAT_SCHEDULE) -- scoreboard housekeeping, not
    the AI's own scheduling. Per-competitor try/except, same reasoning as
    poll_wiki_sources: a bad quote for one competitor's positions must not
    block marking everyone else. Also rolls the weekly SPAXX cash accrual
    onto cash_balance."""
    week_ending = timezone.now().date()
    for competitor in Competitor.objects.select_related("competition"):
        try:
            value = valuation.account_value(competitor)
            WeeklyMark.objects.update_or_create(
                competitor=competitor, week_ending=week_ending, defaults={"account_value": value},
            )
            weekly_rate = competitor.competition.cash_apy / 52
            competitor.cash_balance *= 1 + weekly_rate
            competitor.save(update_fields=["cash_balance"])
        except Exception:
            logger.exception("mark_weekly_values: failed for competitor %s", competitor.id)


def _currently_running_positions():
    """Every open Position belonging to a competition whose date range
    covers today -- same "running" test dispatch_forced_checkins uses
    (competition__start_date__lte / end_date__gte), not Competition.status
    (that field exists but nothing transitions it automatically, so it
    can't be trusted as "is this live right now")."""
    today = timezone.now().astimezone(EASTERN).date()
    return Position.objects.filter(
        competitor__competition__start_date__lte=today, competitor__competition__end_date__gte=today,
    )


@shared_task
def refresh_open_position_quotes():
    """Celery Beat, every 10 minutes (CELERY_BEAT_SCHEDULE) -- no-ops
    outside NYSE regular hours (9:30am-4:00pm ET, Mon-Fri; market holidays
    not filtered, same known gap as FORCED_CHECKINS -- a holiday tick just
    finds stale/closed quotes, harmless). Keeps TickerSnapshot warm (see
    league/services/valuation.py's module docstring) for every
    ticker/contract actually held in a currently-running competition, so
    ScoreboardView/CompetitionCompetitorsView/CompetitorDetailView read a
    persisted value instead of ever live-quoting on a page load.

    Also the natural hook for the league page's equity-curve chart: every
    tick that refreshes quotes, mark and snapshot EVERY competitor in a
    currently-running competition (not just ones with open positions -- an
    all-cash competitor still needs a point, so its curve renders as a flat
    line instead of a gap). This is the only writer of AccountValueSnapshot
    -- ~10-minute resolution, market hours only, so there are gaps
    overnight/weekends the frontend should connect across."""
    now_et = timezone.now().astimezone(EASTERN)
    if now_et.weekday() >= 5 or not (_MARKET_OPEN <= now_et.time() < _MARKET_CLOSE):
        return
    valuation.refresh_quotes_for_positions(_currently_running_positions())

    now = timezone.now()
    today = now_et.date()
    running_competitions = Competition.objects.filter(start_date__lte=today, end_date__gte=today)
    competitors = Competitor.objects.filter(competition__in=running_competitions).prefetch_related("positions")
    AccountValueSnapshot.objects.bulk_create(
        AccountValueSnapshot(competitor=c, at=now, account_value=valuation.account_value(c)) for c in competitors
    )


@shared_task
def refresh_competitor_quotes(competitor_id):
    """Operator-triggered manual refresh for ONE competitor's holdings --
    POST /api/league/competitors/<id>/refresh/ (league.views.
    CompetitorRefreshView) enqueues this and returns 202 immediately; the
    refreshed row arrives over ws://.../ws/league/<competition_id>/ as
    league.competitor_refreshed instead of the HTTP response (docs/
    websockets.md), same "trigger via HTTP, deliver over the socket" shape
    as companies.tasks.screen_ticker. Broadcasts to the competition-wide
    group (not just the requesting user) since the scoreboard/competitor
    list any connected viewer has open should reflect it too."""
    try:
        competitor = Competitor.objects.select_related("competition", "user", "assistant_model").get(id=competitor_id)
    except Competitor.DoesNotExist:
        logger.info("refresh_competitor_quotes: skipping, competitor %s no longer exists", competitor_id)
        return

    valuation.refresh_quotes_for_positions(competitor.positions.all())

    # Imported locally, not at module level -- league.views already imports
    # from league.tasks (run_agent_wakeup, etc.), so a module-level import
    # the other way would be circular. Same reasoning realtime.consumers'
    # _flush_notification_backlog gives for its own local import.
    from .views import _competitor_detail_row
    from realtime.consumers import league_group_name
    from realtime.utils import broadcast_to_group

    broadcast_to_group(
        league_group_name(competitor.competition_id), "league.competitor_refreshed",
        competitor=_competitor_detail_row(competitor),
    )


@shared_task
def refresh_competition_quotes(competition_id):
    """Operator-triggered manual refresh for EVERY competitor in a
    competition -- POST /api/league/competitions/<id>/refresh/ (league.
    views.CompetitionRefreshView). One click updates everyone's scoreboard,
    not just the caller's own competitor -- see refresh_competitor_quotes
    for the narrower version and the general delivery shape."""
    try:
        competition = Competition.objects.get(id=competition_id)
    except Competition.DoesNotExist:
        logger.info("refresh_competition_quotes: skipping, competition %s no longer exists", competition_id)
        return

    valuation.refresh_quotes_for_positions(Position.objects.filter(competitor__competition=competition))

    from .views import _competitor_stats, _competitor_summary, _max_drawdown_pct, _rejected_trade_breakdown
    from realtime.consumers import league_group_name
    from realtime.utils import broadcast_to_group

    trade_counts, turn_stats = _competitor_stats(competition)
    rows = []
    for competitor in competition.competitors.prefetch_related("positions", "weekly_marks"):
        cost, last_woke_at = turn_stats.get(competitor.id, (None, None))
        row = _competitor_summary(
            competitor, filled_trades_count=trade_counts.get(competitor.id, 0),
            api_cost_usd=cost, last_woke_at=last_woke_at,
        )
        row["max_drawdown_pct"] = str(_max_drawdown_pct(competitor))
        rows.append(row)
    rows.sort(key=lambda r: (Decimal(r["total_return_pct"]), r["id"]), reverse=True)

    broadcast_to_group(
        league_group_name(competition.id), "league.scoreboard_refreshed",
        competition=competition.name, competitors=rows,
        rejected_trades=_rejected_trade_breakdown(competition),
    )


def _expiring_option_positions(today: date):
    """Every open call/put Position (long or short) whose expiration has
    passed -- across EVERY competition, not just currently-running ones
    (see _currently_running_positions above): a competition whose
    end_date is already behind us shouldn't leave an expired option
    rotting open just because dispatch_forced_checkins/
    refresh_open_position_quotes have stopped touching it.

    Was short-only (`is_short=True`) until the fix that also made
    submit_trade refuse to close an expired contract manually (execution.
    py's own docstring) -- before that, a LONG call/put that expired
    simply stayed open forever with no way to resolve it at all, since
    nothing else in this app ever touches one. See league.services.
    execution.settle_expired_position's docstring for how a long is
    settled differently from a short (cash-settled to intrinsic value,
    never exercised into a stock position)."""
    return Position.objects.filter(
        instrument_type__in=[Position.INSTRUMENT_CALL, Position.INSTRUMENT_PUT],
        expiration__lte=today,
    ).select_related("competitor", "covers")


@shared_task
def settle_expired_options():
    """Celery Beat, once daily after market close (CELERY_BEAT_SCHEDULE) --
    the only thing in this app that resolves a call/put (long or short)
    past its expiration (see league.services.execution.
    settle_expired_position's docstring for why nothing else does this,
    and why it can't just reuse submit_trade -- which, as of the same
    fix, now refuses an expired contract outright rather than silently
    filling it). Batches one closing-price fetch per distinct ticker
    (not per position) across everything expiring today or earlier, same
    "one round trip per ticker, not per row" shape as
    refresh_quotes_for_positions.

    Deliberately does NOT gate on weekday()/market hours the way
    refresh_open_position_quotes does: if a run gets missed (worker down
    over a weekend, a holiday), the NEXT run's expiration__lte=today query
    still finds every position that should have settled already, and
    Friday's close is still the right settlement price for a holiday-
    delayed catch-up run. Per-position try/except, same reasoning as
    mark_weekly_values: one bad settlement must not block the rest of the
    batch."""
    today = timezone.now().astimezone(EASTERN).date()
    positions = list(_expiring_option_positions(today))
    if not positions:
        return

    # "close" (the settled session print) over "last_price" (can be a
    # post-market print during extended hours), with the None-vs-falsy
    # care a legitimate $0 close needs -- see execution.
    # closing_price_for_settlement's own docstring for the full reasoning
    # (shared with sell_all_positions' manual escape hatch for the same
    # settlement).
    closing_prices: dict[str, Decimal] = {}
    for ticker in {p.ticker for p in positions}:
        price = execution.closing_price_for_settlement(ticker)
        if price is not None:
            closing_prices[ticker] = price

    for position in positions:
        closing_price = closing_prices.get(position.ticker)
        if closing_price is None:
            logger.warning(
                "settle_expired_options: no closing price for %s, position %s left for the next run.",
                position.ticker, position.id,
            )
            continue
        try:
            execution.settle_expired_position(position, closing_price)
        except Exception:
            logger.exception("settle_expired_options: failed to settle position %s", position.id)
