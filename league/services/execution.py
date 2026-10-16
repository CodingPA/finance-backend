"""The rules engine -- the ONLY place that writes to Position/cash_balance.
Every trade, human or AI, goes through submit_trade(). A rejected trade
still gets a Trade row (status=rejected, rejection_reason set) so "the
model tried this and got turned down" is part of the audit trail, not
silently discarded.

settle_expired_position() is the other writer here, for a case
submit_trade can't handle at all: a call/put (long or short) whose
expiration has passed -- and, as of the same fix that added the option-
expiration check to submit_trade above, submit_trade now REFUSES to
close one manually once expired, so this is the ONLY way an expired
option ever gets resolved (a long position used to just sit open forever
past expiration -- see that function's own docstring for why exercising
it into a stock position isn't a safe option here). Nothing in this app
auto-expires or auto-assigns options on its own otherwise (there is no
such Celery task anywhere else). league.tasks.settle_expired_options is
the only caller, once daily after market close.

Sides, in standard options terminology (matches league.models.Trade.SIDE_*):
  BUY            -- open/add to a long position (stock or long call/put)
  SELL           -- close/reduce a long position
  SELL_TO_OPEN   -- open/add to a short position (the option leg of a
                    covered call or cash-covered put ONLY -- this league
                    disallows naked shorting/margin entirely)
  BUY_TO_CLOSE   -- close/reduce a short position

Fill price is always the conservative side of the spread -- ask for any
BUY-type side, bid for any SELL-type side -- per the rules doc, never mid.
"""

import logging
from datetime import date
from decimal import Decimal

from django.db import transaction

from companies.services import pricing

from ..models import Position, Trade
from . import market_hours, valuation

logger = logging.getLogger(__name__)

BUY_SIDES = {Trade.SIDE_BUY, Trade.SIDE_BUY_TO_CLOSE}
SELL_SIDES = {Trade.SIDE_SELL, Trade.SIDE_SELL_TO_OPEN}
OPENING_SIDES = {Trade.SIDE_BUY, Trade.SIDE_SELL_TO_OPEN}
CLOSING_SIDES = {Trade.SIDE_SELL, Trade.SIDE_BUY_TO_CLOSE}


class TradeRejected(Exception):
    """Raised internally with a structured code (Trade.REJECTION_* --
    league.models.Trade) plus the human-readable reason; submit_trade()
    catches this itself and turns it into a rejected Trade row rather than
    letting it propagate -- callers (the submit_trade tool) always get a
    Trade back, filled or rejected, never an exception."""

    def __init__(self, code: str, message: str):
        self.code = code
        super().__init__(message)


def _reject(code: str, message: str):
    raise TradeRejected(code, message)


def _broadcast_trade(trade: Trade, competitor) -> None:
    """Push this Trade onto its competition's live feed
    (realtime.consumers.LeagueFeedConsumer) the instant it's created --
    filled or rejected, human or AI, this is the one place every Trade row
    gets written (see module docstring), so it's the one place that needs
    to know how to broadcast one. competitor is passed in rather than read
    off trade.competitor to reuse submit_trade's already-in-scope instance
    instead of a fresh FK-cache-dependent lookup. Best-effort: a Redis/
    Channels hiccup must never fail an actual trade fill, only cost that
    one row its live push (GET .../trades/ still has it)."""
    try:
        from realtime.consumers import league_group_name
        from realtime.utils import broadcast_to_group

        broadcast_to_group(
            league_group_name(competitor.competition_id), "league.trade",
            id=trade.id,
            competitor_id=competitor.id,
            competitor_name=competitor.name,
            competitor_kind=competitor.kind,
            competition_id=competitor.competition_id,
            side=trade.side,
            instrument_type=trade.instrument_type,
            ticker=trade.ticker,
            strike=str(trade.strike) if trade.strike is not None else None,
            expiration=trade.expiration.isoformat() if trade.expiration else None,
            quantity=trade.quantity,
            limit_price=str(trade.limit_price) if trade.limit_price is not None else None,
            fill_price=str(trade.fill_price) if trade.fill_price is not None else None,
            thesis=trade.thesis,
            status=trade.status,
            rejection_reason=trade.rejection_reason,
            rejection_code=trade.rejection_code,
            created_at=trade.created_at.isoformat(),
        )
    except Exception:
        logger.exception("_broadcast_trade: failed to broadcast trade %s", trade.id)


def _find_position(competitor, instrument_type, ticker, strike, expiration, is_short):
    return Position.objects.filter(
        competitor=competitor, instrument_type=instrument_type, ticker=ticker,
        strike=strike, expiration=expiration, is_short=is_short,
    ).first()


def _covered_call_available(competitor, ticker) -> int:
    """How many shares of `ticker` are long and NOT already backing an
    open covered call -- the number available to cover a NEW sell-to-open.
    Sums across every long stock Position row for this ticker (there
    should only ever be one, since _find_position() updates in place, but
    summing is defensive) minus 100x every open short call whose `covers`
    points at one of those rows."""
    stock_positions = Position.objects.filter(
        competitor=competitor, instrument_type=Position.INSTRUMENT_STOCK, ticker=ticker,
    )
    total_shares = sum(p.quantity for p in stock_positions)
    covered_shares = sum(
        100 * p.quantity
        for p in Position.objects.filter(
            competitor=competitor, instrument_type=Position.INSTRUMENT_CALL, is_short=True, covers__in=stock_positions,
        )
    )
    return total_shares - covered_shares


def _cash_available_for_new_put_collateral(competitor) -> Decimal:
    """cash_balance minus the strike*100*qty collateral already reserved
    by every OTHER open cash-covered put (a cash pool, not per-ticker --
    unlike covered-call share collateral, which is tied to one ticker's
    shares)."""
    reserved = sum(
        (p.strike * 100 * p.quantity)
        for p in Position.objects.filter(
            competitor=competitor, instrument_type=Position.INSTRUMENT_PUT, is_short=True,
        )
    )
    return competitor.cash_balance - reserved


def _fill_price(ticker: str, instrument_type: str, strike, expiration, side: str) -> Decimal:
    """Live ask (BUY sides) or bid (SELL sides) for the instrument. Raises
    TradeRejected if no live quote/contract can be found -- an invalid
    ticker/strike/expiration is a rejection, not a 500.

    Also persists whatever live quote/chain it fetches as this ticker's
    TickerSnapshot (valuation.save_*_snapshot) -- a fill already pays for a
    live pricing-provider round trip, so reusing it means a brand-new position shows
    a fresh mark-to-market immediately instead of stale (avg_cost) until
    the next 10-minute Beat tick (league.tasks.refresh_open_position_quotes).
    Deliberately a plain save, not valuation.refresh_quote/refresh_chain --
    those would fetch AGAIN; this already has the payload in hand."""
    is_buy = side in BUY_SIDES
    if instrument_type == Position.INSTRUMENT_STOCK:
        quote = valuation.get_stock_quote(ticker)
        if quote is None:
            _reject(Trade.REJECTION_MARKET_UNAVAILABLE, f"No live quote found for {ticker} -- check the symbol.")
        valuation.save_quote_snapshot(ticker, quote)
        price = quote["ask"] if is_buy else quote["bid"]
        if price is None:
            _reject(
                Trade.REJECTION_MARKET_UNAVAILABLE,
                f"{ticker} has no live {'ask' if is_buy else 'bid'} right now -- market may be closed.",
            )
        return Decimal(str(price))

    expiration_str = expiration.isoformat() if expiration else None
    chain = pricing.get_options_chain(ticker, expiration=expiration_str)
    if chain is None:
        _reject(Trade.REJECTION_MARKET_UNAVAILABLE, "Live options data isn't available right now.")
    valuation.save_chain_snapshot(ticker, expiration_str, chain)
    contracts = chain["calls"] if instrument_type == Position.INSTRUMENT_CALL else chain["puts"]
    contract = next(
        (c for c in contracts if c["strike_price"] is not None and Decimal(str(c["strike_price"])) == strike), None,
    )
    if contract is None:
        _reject(
            Trade.REJECTION_MARKET_UNAVAILABLE,
            f"No {strike} {instrument_type} found for {ticker} exp {expiration} -- check strike/expiration/availability.",
        )
    price = contract["ask"] if is_buy else contract["bid"]
    if price is None:
        _reject(Trade.REJECTION_MARKET_UNAVAILABLE, f"That contract has no live {'ask' if is_buy else 'bid'} right now.")
    return Decimal(str(price))


def _apply_price_targets(position, target_price, dead_price, fields: list[str]) -> list[str]:
    """Mutates position.target_price/dead_price in place for whichever of
    the two was actually passed (None means "leave whatever's already
    there," not "clear it" -- an add-to-position BUY that doesn't mention
    a level shouldn't wipe one set on an earlier fill), returning the full
    update_fields list including whichever of the two changed."""
    if target_price is not None:
        position.target_price = target_price
        fields.append("target_price")
    if dead_price is not None:
        position.dead_price = dead_price
        fields.append("dead_price")
    return fields


def submit_trade(
    competitor,
    ticker: str,
    side: str,
    instrument_type: str,
    quantity: int,
    thesis: str,
    limit_price: Decimal | None = None,
    strike: Decimal | None = None,
    expiration: date | None = None,
    target_price: Decimal | None = None,
    dead_price: Decimal | None = None,
    force_immediate: bool = False,
) -> Trade:
    """target_price/dead_price: purely informational take-profit/
    invalidation levels recorded on the resulting Position -- see that
    field's docstring. Only meaningful on an OPENING side (BUY/
    SELL_TO_OPEN); silently ignored on a closing trade, since there's no
    Position left afterward (or a smaller one) for them to describe.
    Never enforced here or anywhere else -- nothing auto-closes at these
    levels, the competitor still has to submit_trade themselves.

    A trade that ends up queuing (below) does NOT preserve target_price/
    dead_price -- Trade has no column for them (they're only ever applied
    directly to the Position at fill time, see _execute_trade), and adding
    one just for this narrow, already-optional case isn't worth it. Set
    them again via set_price_targets once a queued order actually fills.

    force_immediate: bypasses Competition.restrict_after_hours_trading's
    queuing even while the market's closed. ONLY for league.management.
    commands.sell_all_positions, an operator's "flatten everything now"
    escape hatch, not a competitor's own order -- queuing its closes would
    silently defer them and the command would misreport them as failed
    rather than pending."""
    ticker = ticker.upper()
    trade_kwargs = dict(
        competitor=competitor, side=side, instrument_type=instrument_type, ticker=ticker,
        strike=strike, expiration=expiration, quantity=quantity, limit_price=limit_price, thesis=thesis,
    )

    try:
        if not thesis or not thesis.strip():
            _reject(Trade.REJECTION_INVALID_REQUEST, "A thesis is required for every trade.")
        if quantity <= 0:
            _reject(Trade.REJECTION_INVALID_REQUEST, "Quantity must be positive.")
        if instrument_type != Position.INSTRUMENT_STOCK and (strike is None or expiration is None):
            _reject(Trade.REJECTION_INVALID_REQUEST, "Options trades require both strike and expiration.")
        if instrument_type != Position.INSTRUMENT_STOCK and market_hours.option_expired(expiration):
            # Nothing else in submit_trade checked this before -- an
            # already-expired contract is gone from a real chain the
            # instant it expires, but this app's own strike-only contract
            # lookup (_fill_price below) has no way to notice that on its
            # own; a same-day (0DTE) contract past its own 4pm close is
            # the same case (see market_hours.option_expired's docstring
            # for the real incident -- an after-close SELL on a contract
            # that hadn't actually expired yet -- this exists to catch
            # the version of that where it HAD). Applies to every side,
            # not just opening ones: closing a short position past its
            # own expiration is what league.tasks.settle_expired_options
            # exists to do automatically instead, not a manual trade.
            _reject(
                Trade.REJECTION_INVALID_REQUEST,
                f"{ticker} {strike} {instrument_type} expired {expiration.isoformat()} -- it can no longer be traded.",
            )
        if instrument_type == Position.INSTRUMENT_STOCK and side in (Trade.SIDE_SELL_TO_OPEN, Trade.SIDE_BUY_TO_CLOSE):
            _reject(
                Trade.REJECTION_INVALID_REQUEST,
                "Short selling stock isn't permitted -- only covered calls/cash-covered puts may open a short leg.",
            )
    except TradeRejected as exc:
        trade = Trade.objects.create(
            **trade_kwargs, status=Trade.STATUS_REJECTED, rejection_reason=str(exc), rejection_code=exc.code,
        )
        _broadcast_trade(trade, competitor)
        return trade

    # Every trade is born STATUS_PENDING and resolved via _execute_trade --
    # for the overwhelmingly common case (no after-hours restriction, or the
    # market's actually open) that resolution happens synchronously right
    # below, so this row never observably exists as "pending" to a caller.
    # It only stays pending when this competition restricts after-hours
    # trading and the market's closed right now -- league.tasks.
    # process_pending_trades (ticking every 5 minutes) resolves it later via
    # resolve_pending_trade, once the market reopens.
    trade = Trade.objects.create(**trade_kwargs, status=Trade.STATUS_PENDING)
    if (
        not force_immediate
        and competitor.competition.restrict_after_hours_trading
        and not market_hours.is_market_open()
    ):
        _broadcast_trade(trade, competitor)
        return trade

    return _execute_trade(competitor, trade, target_price, dead_price)


def resolve_pending_trade(trade: Trade) -> Trade:
    """Called by league.tasks.process_pending_trades once the market is
    open again, for a Trade still STATUS_PENDING (queued by submit_trade
    above because Competition.restrict_after_hours_trading was set and the
    market was closed at submission time). Re-runs the exact same price/
    limit/caps/cash/collateral checks submit_trade's immediate path already
    runs, using a live quote fetched right now -- there's no historical
    "opening print" available from this app's pricing provider (see
    market_hours.is_market_open's docstring), so "first live quote after
    the next open" is the closest honest approximation, and this is a
    genuine re-validation against current market/portfolio state, not a
    rubber stamp of whatever was true at submission time. target_price/
    dead_price are never recovered here -- Trade never stored them for a
    queued order (see submit_trade's own docstring) -- a resulting new/
    added-to position starts with neither set, same as any trade that
    didn't pass them, revisable after the fact via set_price_targets."""
    return _execute_trade(trade.competitor, trade, target_price=None, dead_price=None)


class TradeAlreadyResolved(Exception):
    """Raised by cancel_pending_trade when the trade is no longer
    STATUS_PENDING by the time the cancel actually reaches the database --
    it already filled, was rejected, or was already cancelled. Callers
    (league.views.CompetitorTradeCancelView, league.tools' cancel_trade)
    catch this and turn it into their own user-facing error shape (a DRF
    ValidationError / a ToolError) -- there's no Trade-row-level place to
    attach a reason the way TradeRejected has, since cancellation doesn't
    create or re-fill a row, just flips one that's already settled to
    something else."""


def cancel_pending_trade(trade: Trade) -> Trade:
    """Cancels a still-queued order (Trade.STATUS_PENDING -- see
    submit_trade's docstring for how one gets queued) before
    league.tasks.process_pending_trades has resolved it. A conditional
    UPDATE ... WHERE status='pending' (not a plain trade.save()) so this
    can never race resolve_pending_trade into overwriting a fill/rejection
    that already landed -- see Trade's own docstring for the race and which
    side wins it. Raises TradeAlreadyResolved (0 rows matched) rather than
    silently no-op'ing if that happened. No Position/cash to unwind: a
    pending trade never touched either (see _execute_trade) -- that's what
    makes cancellation this cheap and safe."""
    updated = Trade.objects.filter(id=trade.id, status=Trade.STATUS_PENDING).update(status=Trade.STATUS_CANCELLED)
    if not updated:
        # fields=["status"] only -- a full refresh would also re-read
        # strike/limit_price/etc. through their DB column's decimal_places,
        # silently reformatting an in-memory value like Decimal("999") into
        # Decimal("999.0000") anywhere this trade's fields get echoed back
        # (e.g. a rejection message) after this call.
        trade.refresh_from_db(fields=["status"])
        raise TradeAlreadyResolved(
            f"Trade {trade.id} is no longer pending (status={trade.status}) -- can't cancel it now.",
        )
    trade.status = Trade.STATUS_CANCELLED
    _broadcast_trade(trade, trade.competitor)
    return trade


def _execute_trade(
    competitor, trade: Trade, target_price: Decimal | None, dead_price: Decimal | None,
) -> Trade:
    """Price fetch + limit/caps/cash/collateral checks + the actual
    Position/cash mutation, for a `trade` already persisted as
    STATUS_PENDING -- either just-created by submit_trade's immediate path,
    or an existing row being resolved later by resolve_pending_trade.
    Writes the outcome onto that SAME row (FILLED with fill_price/position,
    or REJECTED with rejection_code/reason) rather than creating a new one,
    so a queued order's identity -- and anything already referencing its
    id, e.g. a websocket client that saw it queue -- survives into its
    resolution. See Trade's own docstring for why this isn't append-only
    after all.

    Re-checks trade.status is still PENDING (a fresh read) before doing
    anything else -- cheap and a no-op for the immediate-fill path (trade
    was just created one line above in submit_trade), but for the deferred
    path (resolve_pending_trade, called from a queue drained by
    league.tasks.process_pending_trades) it's the other half of the race
    cancel_pending_trade guards from its side: if a concurrent cancel won,
    this backs off and returns the trade as-is instead of filling/rejecting
    an order the competitor already pulled. fields=["status"] only -- see
    cancel_pending_trade's own comment on why a full refresh would silently
    reformat strike/limit_price/etc. through their DB column's
    decimal_places.

    Also refreshes competitor.cash_balance for the same reason, one level
    up: process_pending_trades loads its whole batch via a single
    select_related("competitor__...") query, so two still-PENDING trades
    for the SAME competitor each arrive here holding a SEPARATE Python
    `competitor` instance, both stamped with cash_balance as of that one
    shared query -- before either trade's fill has touched the DB. Without
    this refresh, the second trade's cash check (and its position-cap
    check, which also reads cash_balance via valuation.account_value)
    would validate against a balance that doesn't account for the first
    trade's fill, and its subsequent save() would overwrite the first
    trade's deduction outright (a lost update) -- letting two pre-market
    orders both "spend" the same starting cash instead of the second one
    validating against, and debiting from, what the first actually left
    behind. fields=["cash_balance"] only, same reasoning as trade's own
    refresh above -- a full refresh would also silently reformat any other
    in-memory field through its column's own decimal_places."""
    trade.refresh_from_db(fields=["status"])
    if trade.status != Trade.STATUS_PENDING:
        return trade
    competitor.refresh_from_db(fields=["cash_balance"])

    ticker, side, instrument_type = trade.ticker, trade.side, trade.instrument_type
    strike, expiration, quantity, limit_price = trade.strike, trade.expiration, trade.quantity, trade.limit_price

    try:
        fill_price = _fill_price(ticker, instrument_type, strike, expiration, side)

        if limit_price is not None:
            if side in BUY_SIDES and fill_price > limit_price:
                _reject(Trade.REJECTION_PRICE_LIMIT, f"Live price ${fill_price} is above your ${limit_price} limit.")
            if side in SELL_SIDES and fill_price < limit_price:
                _reject(Trade.REJECTION_PRICE_LIMIT, f"Live price ${fill_price} is below your ${limit_price} limit.")

        multiplier = 1 if instrument_type == Position.INSTRUMENT_STOCK else 100
        notional = fill_price * quantity * multiplier

        if side in OPENING_SIDES:
            # The 60%/30% caps bound NEW capital committed at entry.
            # SELL_TO_OPEN (covered call / cash-covered put) doesn't commit
            # new capital beyond what's already inside the cap from the
            # covering stock/cash -- it's gated by the collateral check
            # below instead, not the position-size cap.
            if side == Trade.SIDE_BUY:
                value = valuation.account_value(competitor)
                max_position = competitor.competition.max_position_pct * value
                if notional > max_position:
                    _reject(
                        Trade.REJECTION_POSITION_CAP,
                        f"${notional:.2f} exceeds the {competitor.competition.max_position_pct:.0%} "
                        f"single-position cap (${max_position:.2f} of ${value:.2f} account value).",
                    )
                if instrument_type == Position.INSTRUMENT_STOCK and ticker in competitor.competition.index_tickers:
                    max_index = competitor.competition.max_index_pct * value
                    if notional > max_index:
                        _reject(
                            Trade.REJECTION_INDEX_CAP,
                            f"${notional:.2f} exceeds the {competitor.competition.max_index_pct:.0%} "
                            f"index-fund cap (${max_index:.2f} of ${value:.2f} account value).",
                        )

            if side == Trade.SIDE_SELL_TO_OPEN:
                if instrument_type == Position.INSTRUMENT_CALL:
                    available = _covered_call_available(competitor, ticker)
                    if available < quantity * 100:
                        _reject(
                            Trade.REJECTION_INSUFFICIENT_COLLATERAL,
                            f"Covered call requires {quantity * 100} uncovered shares of {ticker}; "
                            f"only {available} available.",
                        )
                elif instrument_type == Position.INSTRUMENT_PUT:
                    available_cash = _cash_available_for_new_put_collateral(competitor)
                    required = strike * 100 * quantity
                    if available_cash < required:
                        _reject(
                            Trade.REJECTION_INSUFFICIENT_COLLATERAL,
                            f"Cash-covered put requires ${required:.2f} collateral; "
                            f"only ${available_cash:.2f} available.",
                        )
                else:
                    _reject(
                        Trade.REJECTION_INVALID_REQUEST,
                        "sell_to_open is only valid for covered calls or cash-covered puts.",
                    )

            if side == Trade.SIDE_BUY and notional > competitor.cash_balance:
                _reject(
                    Trade.REJECTION_INSUFFICIENT_CASH,
                    f"${notional:.2f} exceeds available cash (${competitor.cash_balance:.2f}).",
                )

        else:  # CLOSING_SIDES
            is_short = side == Trade.SIDE_BUY_TO_CLOSE
            position = _find_position(competitor, instrument_type, ticker, strike, expiration, is_short)
            if position is None or position.quantity < quantity:
                held = position.quantity if position else 0
                _reject(
                    Trade.REJECTION_INSUFFICIENT_HOLDINGS,
                    f"Can't {side.replace('_', ' ')} {quantity} -- only {held} held.",
                )
            if side == Trade.SIDE_SELL and instrument_type == Position.INSTRUMENT_STOCK:
                # Selling shares that are backing an open covered call would
                # strand that call without its collateral -- only the
                # uncovered surplus may be sold.
                uncovered = _covered_call_available(competitor, ticker)
                if quantity > uncovered:
                    _reject(
                        Trade.REJECTION_INSUFFICIENT_HOLDINGS,
                        f"Selling {quantity} shares of {ticker} would leave an open covered call "
                        f"without its collateral -- only {uncovered} shares are uncovered.",
                    )

        with transaction.atomic():
            if side == Trade.SIDE_BUY:
                position = _find_position(competitor, instrument_type, ticker, strike, expiration, False)
                if position is None:
                    position = Position.objects.create(
                        competitor=competitor, instrument_type=instrument_type, ticker=ticker,
                        strike=strike, expiration=expiration, quantity=quantity, is_short=False, avg_cost=fill_price,
                        target_price=target_price, dead_price=dead_price,
                    )
                else:
                    total_cost = position.avg_cost * position.quantity + fill_price * quantity
                    position.quantity += quantity
                    position.avg_cost = total_cost / position.quantity
                    fields = _apply_price_targets(position, target_price, dead_price, ["quantity", "avg_cost"])
                    position.save(update_fields=fields)
                competitor.cash_balance -= notional

            elif side == Trade.SIDE_SELL_TO_OPEN:
                position = _find_position(competitor, instrument_type, ticker, strike, expiration, True)
                covers = None
                if instrument_type == Position.INSTRUMENT_CALL:
                    covers = Position.objects.filter(
                        competitor=competitor, instrument_type=Position.INSTRUMENT_STOCK, ticker=ticker,
                    ).first()
                if position is None:
                    position = Position.objects.create(
                        competitor=competitor, instrument_type=instrument_type, ticker=ticker,
                        strike=strike, expiration=expiration, quantity=quantity, is_short=True,
                        avg_cost=fill_price, covers=covers, target_price=target_price, dead_price=dead_price,
                    )
                else:
                    total_credit = position.avg_cost * position.quantity + fill_price * quantity
                    position.quantity += quantity
                    position.avg_cost = total_credit / position.quantity
                    fields = _apply_price_targets(position, target_price, dead_price, ["quantity", "avg_cost"])
                    position.save(update_fields=fields)
                competitor.cash_balance += notional

            else:  # SELL or BUY_TO_CLOSE
                is_short = side == Trade.SIDE_BUY_TO_CLOSE
                position = _find_position(competitor, instrument_type, ticker, strike, expiration, is_short)
                position.quantity -= quantity
                if position.quantity == 0:
                    position.delete()
                    position = None
                else:
                    position.save(update_fields=["quantity"])
                if side == Trade.SIDE_SELL:
                    competitor.cash_balance += notional
                else:
                    competitor.cash_balance -= notional

            competitor.save(update_fields=["cash_balance"])
            trade.position = position
            trade.fill_price = fill_price
            trade.status = Trade.STATUS_FILLED
            trade.save(update_fields=["position", "fill_price", "status"])
        _broadcast_trade(trade, competitor)
        return trade

    except TradeRejected as exc:
        trade.status = Trade.STATUS_REJECTED
        trade.rejection_reason = str(exc)
        trade.rejection_code = exc.code
        trade.save(update_fields=["status", "rejection_reason", "rejection_code"])
        _broadcast_trade(trade, competitor)
        return trade


def _apply_assignment(competitor, position: Position, is_call: bool, stock_quantity: int, notional: Decimal, trades: list) -> bool:
    """The stock side of an assignment: delivers shares at strike for an
    assigned short call, or takes shares at strike for an assigned short
    put. Appends the resulting Trade to `trades` and returns True on
    success.

    For a call, the shares to deliver are `position.covers` -- the exact
    stock row _covered_call_available() checked when this call was opened
    -- not just "some stock position for this ticker": a competitor can
    hold more than one stock row for the same ticker (defensively summed
    elsewhere, per Position's own docstring), and more than one short call
    can cover the same stock row, so looking up "by ticker" instead of by
    the actual FK risks deducting from the wrong row or double/under-
    counting when settling multiple calls. Falls back to a by-ticker
    lookup only if `covers` is somehow null (should only happen if the
    covering row was itself fully closed elsewhere and cascade-nulled it,
    which the covering-shares check below then catches anyway). Puts have
    no `covers` FK by design (cash-covered puts collateralize against
    cash_balance, not a specific stock row) -- always by-ticker for them.

    Returns False (logs, does not raise) if the shares/cash this specific
    assignment needs aren't actually there. That should never happen --
    submit_trade's own SELL/collateral checks keep this invariant while a
    short option is open -- but this runs unattended nightly across every
    competitor, so a broken invariant logs loudly and lets the caller fall
    back to closing the option leg at $0 (extinguishing the obligation
    with no stock trade) rather than crashing the whole batch."""
    stock_position = position.covers if is_call else None
    if stock_position is None:
        stock_position = _find_position(competitor, Position.INSTRUMENT_STOCK, position.ticker, None, None, False)

    if is_call:
        if stock_position is None or stock_position.quantity < stock_quantity:
            logger.error(
                "settle_expired_position: %s owes %s shares of %s for call assignment (position %s), only %s held.",
                competitor.name, stock_quantity, position.ticker, position.id,
                stock_position.quantity if stock_position else 0,
            )
            return False
        stock_position.quantity -= stock_quantity
        if stock_position.quantity == 0:
            stock_position.delete()
            stock_position = None
        else:
            stock_position.save(update_fields=["quantity"])
        competitor.cash_balance += notional
        side, verb = Trade.SIDE_SELL, "delivered"
    else:
        if notional > competitor.cash_balance:
            logger.error(
                "settle_expired_position: %s needs $%s cash for put assignment on %s (position %s), only $%s available.",
                competitor.name, notional, position.ticker, position.id, competitor.cash_balance,
            )
            return False
        if stock_position is None:
            stock_position = Position.objects.create(
                competitor=competitor, instrument_type=Position.INSTRUMENT_STOCK, ticker=position.ticker,
                quantity=stock_quantity, is_short=False, avg_cost=position.strike,
            )
        else:
            total_cost = stock_position.avg_cost * stock_position.quantity + position.strike * stock_quantity
            stock_position.quantity += stock_quantity
            stock_position.avg_cost = total_cost / stock_position.quantity
            stock_position.save(update_fields=["quantity", "avg_cost"])
        competitor.cash_balance -= notional
        side, verb = Trade.SIDE_BUY, "received"

    trades.append(Trade.objects.create(
        competitor=competitor, position=stock_position, side=side, instrument_type=Position.INSTRUMENT_STOCK,
        ticker=position.ticker, quantity=stock_quantity, fill_price=position.strike,
        thesis=(
            f"Automatic settlement: assigned on {position.quantity} short {position.instrument_type}(s), "
            f"{verb} {stock_quantity} shares at strike ${position.strike}."
        ),
        status=Trade.STATUS_FILLED,
    ))
    return True


def closing_price_for_settlement(ticker: str) -> Decimal | None:
    """The settlement price for an expired option: the underlying's session
    close, falling back to last_price if a provider never populates "close"
    (Steady never does -- see pricing.py's _tradier_quote_to_steady_shape).
    None if no quote is available at all (an unlisted/halted/delisted
    symbol, or a provider outage) -- callers must handle that by leaving
    the position for a later retry, not by guessing a price.

    Shared by league.tasks.settle_expired_options (the daily batch, one
    call per distinct ticker) and sell_all_positions (the operator's
    manual escape hatch for a position that batch hasn't reached yet --
    submit_trade refuses to touch an expired contract at all, so this is
    the only remaining way to close one on demand)."""
    try:
        quote = pricing.get_quote(ticker)
    except Exception:
        logger.exception("closing_price_for_settlement: failed to fetch quote for %s", ticker)
        return None
    if not quote:
        return None
    # `is None`, not falsy -- see settle_expired_options' own comment on
    # why a legitimate close of exactly 0 must not fall back to last_price.
    price = quote.get("close")
    if price is None:
        price = quote.get("last_price")
    return Decimal(str(price)) if price is not None else None


def settle_expired_position(position: Position, closing_price: Decimal) -> list[Trade]:
    """Force-closes one call/put Position (long or short) whose expiration
    has passed -- the only caller is league.tasks.settle_expired_options
    (see that task and this module's docstring for why this exists at
    all, and why submit_trade itself now refuses to touch an expired
    contract instead). Deliberately does NOT go through submit_trade/
    _fill_price: an already-expired contract is gone from the provider's
    chain the instant it expires, so there is no live price to fetch it
    at, and settlement is defined in terms of the STRIKE and the
    underlying's closing price, not "the live ask/bid" _fill_price exists
    to find.

    ITM at `closing_price` (calls: close > strike; puts: close < strike;
    exactly at strike counts as OTM for both, i.e. expires worthless) is
    the only case that moves money/shares; OTM expires worthless -- the
    option leg alone closes out, no further cash movement (whatever
    premium was paid/collected at open was already realized then). Real
    brokers auto-exercise ITM by as little as $0.01; this uses a plain
    >/< comparison against `closing_price`, not that exact convention.

    SHORT (the option leg of a covered call or cash-covered put -- the
    only kind of short this league permits at all): the covering stock
    trades at the strike price via _apply_assignment (SELL for a call --
    delivering shares; BUY for a put -- taking shares); the option leg
    itself closes at $0 (the premium collected at open is already
    realized, closing it out costs nothing further).

    LONG: cash-settled to intrinsic value instead of exercised into a
    stock position. Exercising an ITM long call needs cash to pay the
    strike, and nothing reserves that the way SELL_TO_OPEN's collateral
    checks reserve shares/cash for the short side; exercising an ITM long
    put needs shares to deliver, and -- absent them -- would create a
    naked short stock position, which this league disallows entirely
    (Position's own docstring: "this league disallows naked shorting/
    margin entirely"). Cash-settling the intrinsic value (0 if OTM) is
    the same simplification a real broker falls back to for an account
    that hasn't elected, or can't support, physical exercise -- and it's
    the only option here that can never violate either constraint. The
    option leg closes at that per-share intrinsic value, same shape as an
    ordinary closing SELL.

    Returns every Trade row created (one or two for a short settlement,
    always exactly one for a long), STATUS_FILLED same as submit_trade
    produces -- indistinguishable in the Trade history except for the
    "Automatic settlement:" thesis text, the same convention
    league/management/commands/sell_all_positions.py already uses for a
    system-generated (non-competitor) trade rather than a new DB field."""
    if position.instrument_type not in (Position.INSTRUMENT_CALL, Position.INSTRUMENT_PUT):
        raise ValueError(f"settle_expired_position called on a non-option position: {position}")

    is_call = position.instrument_type == Position.INSTRUMENT_CALL
    itm = closing_price > position.strike if is_call else closing_price < position.strike
    competitor = position.competitor
    # Captured before position.delete() below -- Django only resets pk/id
    # on a deleted instance, not its other fields, so reading these off
    # `position` afterward would actually still work, but grabbing them
    # up front (same as submit_trade's own trade_kwargs) doesn't rely on
    # that.
    option_quantity, option_strike, option_expiration, option_type, option_ticker, is_short = (
        position.quantity, position.strike, position.expiration, position.instrument_type, position.ticker,
        position.is_short,
    )

    trades: list[Trade] = []
    with transaction.atomic():
        if is_short:
            stock_quantity = option_quantity * 100
            notional = option_strike * stock_quantity
            assigned = itm and _apply_assignment(competitor, position, is_call, stock_quantity, notional, trades)
            label = "assigned" if assigned else "expired worthless"
            position.delete()
            trades.append(Trade.objects.create(
                competitor=competitor, position=None, side=Trade.SIDE_BUY_TO_CLOSE,
                instrument_type=option_type, ticker=option_ticker, strike=option_strike,
                expiration=option_expiration, quantity=option_quantity, fill_price=Decimal("0"),
                thesis=(
                    f"Automatic settlement: {option_quantity} short {option_type}(s) {label} at expiration "
                    f"(closing price ${closing_price} vs strike ${option_strike})."
                ),
                status=Trade.STATUS_FILLED,
            ))
        else:
            intrinsic = (
                max(closing_price - option_strike, Decimal("0")) if is_call
                else max(option_strike - closing_price, Decimal("0"))
            )
            notional = intrinsic * option_quantity * 100
            label = "exercised (cash-settled)" if itm else "expired worthless"
            competitor.cash_balance += notional
            position.delete()
            trades.append(Trade.objects.create(
                competitor=competitor, position=None, side=Trade.SIDE_SELL,
                instrument_type=option_type, ticker=option_ticker, strike=option_strike,
                expiration=option_expiration, quantity=option_quantity, fill_price=intrinsic,
                thesis=(
                    f"Automatic settlement: {option_quantity} long {option_type}(s) {label} at expiration "
                    f"(closing price ${closing_price} vs strike ${option_strike}, "
                    f"intrinsic value ${intrinsic}/share, cash-settled -- not exercised into shares)."
                ),
                status=Trade.STATUS_FILLED,
            ))
        competitor.save(update_fields=["cash_balance"])

    for trade in trades:
        _broadcast_trade(trade, competitor)
    return trades
