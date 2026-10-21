"""Tests for the rules engine (league.services.execution) -- the one
module everything else in this app trusts, per its own docstring. All
live Steady calls are mocked at companies.services.steady's function
objects, patched once here since both execution.py and valuation.py call
through that same module reference."""

import base64
import json
from datetime import date, datetime, time, timedelta
from decimal import Decimal
from io import StringIO
from unittest.mock import patch

import anthropic
import httpx
import openai
from anthropic.lib.tools import ToolError
from anthropic.types.beta import BetaMessage
from django.core.management import call_command
from django.test import TestCase as _DjangoTestCase
from django.test import override_settings
from django.utils import timezone
from google.genai import types as genai_types
from openai.types.responses import Response
from rest_framework_simplejwt.tokens import RefreshToken

from accounts.models import User

from assistant.models import AssistantModel
from companies.models import Company, TickerSnapshot

from .models import (
    AgentScratchpad, AgentTickerMemory, AgentTurn, AgentWakeup, Competition, Competitor, DailyBrief, ForcedCheckin,
    Position, Trade, WeeklyMark,
)
from .services import budget, execution, llm_gemini, llm_openai, market_hours, valuation, wakeup_spacing
from .services.execution import submit_trade
from .tasks import (
    EASTERN, _headlines_block, _market_snapshot_block, _run_anthropic_turn, _run_wakeup, dispatch_forced_checkins,
    process_pending_trades, refresh_competition_quotes, refresh_competitor_quotes, refresh_open_position_quotes,
    run_agent_wakeup, settle_expired_options,
)
from .tools import run_gemini_tool, run_openai_tool


@override_settings(PRICING_PROVIDER="steady")
class TestCase(_DjangoTestCase):
    """Every test below mocks companies.services.steady.get_quote/
    get_options_chain directly (see module docstring), which only
    intercepts execution.py/valuation.py's actual calls when
    PRICING_PROVIDER="steady" -- companies.services.pricing dispatches to
    Tradier instead otherwise, bypassing these mocks entirely and hitting
    the real network. Pinning it here, once, means this suite is correct
    regardless of whichever provider happens to be set in the developer's
    own .env."""


def _quote(ask, bid, last=None):
    return {
        "symbol": "TEST", "company_name": "Test Co", "exchange": "TEST", "market_status": "Open",
        "last_price": last if last is not None else (ask + bid) / 2,
        "net_change": 0, "percent_change_pct": 0, "is_real_time": True, "last_trade_timestamp": "now",
        "bid": bid, "ask": ask, "volume": 1000, "after_hours": None,
        "fifty_two_week_range": None, "day_range": None,
    }


def _contract(strike, ask, bid, option_type="Call"):
    return {
        "symbol": f"TEST|{strike}{option_type[0]}", "base_symbol": "TEST", "strike_price": strike,
        "expiration_date": "12/19/26", "expiration_type": "monthly", "moneyness_pct": 0,
        "bid": bid, "mid": (ask + bid) / 2, "ask": ask, "last": (ask + bid) / 2,
        "price_change": None, "percent_change_pct": None, "volume": 100, "open_interest": 100,
        "open_interest_change": None, "implied_volatility_pct": 25.0, "delta": 0.5, "vega": 0.1,
        "theta": -0.05, "rho": 0.01, "option_type": option_type, "days_to_expiration": 30,
        "average_iv_pct": 25.0, "historic_volatility_30d_pct": 25.0, "next_earnings_date": None,
        "implied_volatility_rank_1y_pct": 50.0,
    }


def _chain(calls=None, puts=None):
    return {"expirations": {"weekly": [], "monthly": ["2026-12-19"]}, "calls": calls or [], "puts": puts or []}


EXPIRATION = date(2026, 12, 19)


def _anthropic_auth_error():
    """A real anthropic.AuthenticationError, shaped exactly like the one
    that killed the "Trend" competitor on 2026-08-25 (revoked
    ANTHROPIC_API_KEY) -- used to exercise league.services.provider_errors
    against the actual exception type/body shape it has to classify,
    not a generic stand-in."""
    req = httpx.Request("POST", "https://api.anthropic.com/v1/messages")
    resp = httpx.Response(
        401, request=req,
        json={"type": "error", "error": {"type": "authentication_error", "message": "invalid x-api-key"}},
    )
    return anthropic.AuthenticationError("Error code: 401", response=resp, body=resp.json())


def _openai_quota_exhausted_error():
    """A real openai.RateLimitError shaped exactly like the one that
    killed "RATCHET-12" on 2026-08-26 (an OpenAI account with no credits
    left) -- a 429, same as ordinary rate limiting, distinguished only by
    the nested error body's type/code (see provider_errors' docstring for
    why those live nested, not on the exception's own .type/.code)."""
    req = httpx.Request("POST", "https://api.openai.com/v1/responses")
    resp = httpx.Response(
        429, request=req,
        json={"error": {
            "message": "You have no credits remaining.", "type": "insufficient_quota",
            "param": None, "code": "credit_balance_exhausted",
        }},
    )
    return openai.RateLimitError("Error code: 429 - ...", response=resp, body=resp.json())


class ExecutionTests(TestCase):
    def setUp(self):
        self.competition = Competition.objects.create(
            name="Test League",
            starting_capital=Decimal("5000"),
            max_position_pct=Decimal("0.60"),
            max_index_pct=Decimal("0.30"),
            index_tickers=["SPY"],
            start_date=date(2026, 8, 12),
            end_date=date(2026, 11, 4),
        )
        self.competitor = Competitor.objects.create(
            competition=self.competition, name="Tester", kind=Competitor.KIND_AI,
            cash_balance=self.competition.starting_capital,
        )

    def _buy_stock(self, ticker="TEST", quantity=10, ask=100, bid=99, limit_price=None, thesis="test thesis"):
        with patch("companies.services.steady.get_quote", return_value=_quote(ask, bid)):
            return submit_trade(
                self.competitor, ticker, Trade.SIDE_BUY, Position.INSTRUMENT_STOCK, quantity,
                thesis, limit_price=limit_price,
            )

    # --- basic fills ---

    def test_buy_stock_fills_at_ask(self):
        trade = self._buy_stock(quantity=10, ask=100, bid=99)
        self.assertEqual(trade.status, Trade.STATUS_FILLED)
        self.assertEqual(trade.fill_price, Decimal("100"))
        self.competitor.refresh_from_db()
        self.assertEqual(self.competitor.cash_balance, Decimal("4000"))
        position = Position.objects.get(competitor=self.competitor, ticker="TEST")
        self.assertEqual(position.quantity, 10)
        self.assertEqual(position.avg_cost, Decimal("100"))

    def test_sell_stock_fills_at_bid(self):
        self._buy_stock(quantity=10, ask=100, bid=99)
        with patch("companies.services.steady.get_quote", return_value=_quote(ask=105, bid=102)):
            trade = submit_trade(
                self.competitor, "TEST", Trade.SIDE_SELL, Position.INSTRUMENT_STOCK, 5, "taking profit",
            )
        self.assertEqual(trade.status, Trade.STATUS_FILLED)
        self.assertEqual(trade.fill_price, Decimal("102"))
        self.competitor.refresh_from_db()
        # 5000 - 1000 (buy) + 5*102 (sell) = 4510
        self.assertEqual(self.competitor.cash_balance, Decimal("4510"))
        position = Position.objects.get(competitor=self.competitor, ticker="TEST")
        self.assertEqual(position.quantity, 5)

    def test_selling_entire_position_deletes_it(self):
        self._buy_stock(quantity=10, ask=100, bid=99)
        with patch("companies.services.steady.get_quote", return_value=_quote(ask=105, bid=102)):
            submit_trade(self.competitor, "TEST", Trade.SIDE_SELL, Position.INSTRUMENT_STOCK, 10, "exit")
        self.assertFalse(Position.objects.filter(competitor=self.competitor, ticker="TEST").exists())

    def test_selling_more_than_held_rejected(self):
        self._buy_stock(quantity=5, ask=100, bid=99)
        with patch("companies.services.steady.get_quote", return_value=_quote(ask=105, bid=102)):
            trade = submit_trade(self.competitor, "TEST", Trade.SIDE_SELL, Position.INSTRUMENT_STOCK, 10, "exit")
        self.assertEqual(trade.status, Trade.STATUS_REJECTED)
        self.assertIn("only 5 held", trade.rejection_reason)
        self.competitor.refresh_from_db()
        self.assertEqual(self.competitor.cash_balance, Decimal("4500"))  # unchanged by the rejected trade

    # --- limit price guard ---

    def test_limit_price_rejects_buy_above_limit(self):
        trade = self._buy_stock(quantity=10, ask=100, bid=99, limit_price=Decimal("90"))
        self.assertEqual(trade.status, Trade.STATUS_REJECTED)
        self.assertIn("above your $90", trade.rejection_reason)
        self.competitor.refresh_from_db()
        self.assertEqual(self.competitor.cash_balance, self.competition.starting_capital)
        self.assertFalse(Position.objects.filter(competitor=self.competitor).exists())

    def test_limit_price_allows_buy_at_exactly_limit(self):
        trade = self._buy_stock(quantity=10, ask=100, bid=99, limit_price=Decimal("100"))
        self.assertEqual(trade.status, Trade.STATUS_FILLED)

    def test_limit_price_rejects_sell_below_limit(self):
        self._buy_stock(quantity=10, ask=100, bid=99)
        with patch("companies.services.steady.get_quote", return_value=_quote(ask=95, bid=90)):
            trade = submit_trade(
                self.competitor, "TEST", Trade.SIDE_SELL, Position.INSTRUMENT_STOCK, 5, "exit",
                limit_price=Decimal("95"),
            )
        self.assertEqual(trade.status, Trade.STATUS_REJECTED)
        self.assertIn("below your $95", trade.rejection_reason)

    # --- position / index caps (measured against CURRENT account value) ---

    def test_position_cap_rejects_oversized_buy(self):
        # 60% of $5000 = $3000; 31 * $100 = $3100 > cap.
        trade = self._buy_stock(quantity=31, ask=100, bid=99)
        self.assertEqual(trade.status, Trade.STATUS_REJECTED)
        self.assertIn("single-position cap", trade.rejection_reason)

    def test_position_cap_allows_at_boundary(self):
        # Exactly 60% of $5000 = $3000; 30 * $100 = $3000, not over.
        trade = self._buy_stock(quantity=30, ask=100, bid=99)
        self.assertEqual(trade.status, Trade.STATUS_FILLED)

    def test_position_cap_uses_current_not_starting_value(self):
        # Grow the account first (sell nothing, just directly bump cash to
        # simulate a prior winning trade), then confirm the cap scales with
        # the new value, not the original $5000.
        self.competitor.cash_balance = Decimal("6500")
        self.competitor.save()
        # 60% of $6500 = $3900; 39 * $100 = $3900 should just fit.
        trade = self._buy_stock(quantity=39, ask=100, bid=99)
        self.assertEqual(trade.status, Trade.STATUS_FILLED)

    def test_index_cap_binds_tighter_than_position_cap(self):
        # 30% of $5000 = $1500 (index cap); 60% = $3000 (position cap).
        # 20 * $100 = $2000 is under the position cap but over the index cap.
        trade = self._buy_stock(ticker="SPY", quantity=20, ask=100, bid=99)
        self.assertEqual(trade.status, Trade.STATUS_REJECTED)
        self.assertIn("index-fund cap", trade.rejection_reason)

    def test_index_cap_does_not_apply_to_non_index_ticker(self):
        # Same $2000 notional on a non-index ticker only faces the 60% cap.
        trade = self._buy_stock(ticker="RKLB", quantity=20, ask=100, bid=99)
        self.assertEqual(trade.status, Trade.STATUS_FILLED)

    def test_index_cap_does_not_apply_to_options_on_index_ticker(self):
        # A SPY call is a directional bet with theta, not the passive
        # share-parking the index cap exists to discourage.
        with patch("companies.services.steady.get_options_chain", return_value=_chain(calls=[_contract(600, 20, 19)])):
            trade = submit_trade(
                self.competitor, "SPY", Trade.SIDE_BUY, Position.INSTRUMENT_CALL, 1, "SPY call",
                strike=Decimal("600"), expiration=EXPIRATION,
            )
        # 1 contract * $20 * 100 = $2000, over the $1500 index cap but under
        # the $3000 position cap -- and options aren't index-capped at all.
        self.assertEqual(trade.status, Trade.STATUS_FILLED)

    # --- options: long calls/puts ---

    def test_buy_call_fills_at_ask_and_creates_position(self):
        with patch("companies.services.steady.get_options_chain", return_value=_chain(calls=[_contract(150, 5, 4.5)])):
            trade = submit_trade(
                self.competitor, "TEST", Trade.SIDE_BUY, Position.INSTRUMENT_CALL, 2, "bullish thesis",
                strike=Decimal("150"), expiration=EXPIRATION,
            )
        self.assertEqual(trade.status, Trade.STATUS_FILLED)
        self.assertEqual(trade.fill_price, Decimal("5"))
        self.competitor.refresh_from_db()
        self.assertEqual(self.competitor.cash_balance, Decimal("5000") - Decimal("5") * 2 * 100)
        position = Position.objects.get(competitor=self.competitor, instrument_type=Position.INSTRUMENT_CALL)
        self.assertEqual(position.quantity, 2)
        self.assertFalse(position.is_short)

    def test_option_requires_strike_and_expiration(self):
        trade = submit_trade(
            self.competitor, "TEST", Trade.SIDE_BUY, Position.INSTRUMENT_CALL, 1, "thesis",
        )
        self.assertEqual(trade.status, Trade.STATUS_REJECTED)
        self.assertIn("require both strike and expiration", trade.rejection_reason)

    def test_no_matching_contract_rejected(self):
        with patch("companies.services.steady.get_options_chain", return_value=_chain(calls=[_contract(150, 5, 4.5)])):
            trade = submit_trade(
                self.competitor, "TEST", Trade.SIDE_BUY, Position.INSTRUMENT_CALL, 1, "thesis",
                strike=Decimal("999"), expiration=EXPIRATION,
            )
        self.assertEqual(trade.status, Trade.STATUS_REJECTED)
        self.assertIn("No 999 call found", trade.rejection_reason)

    # --- expired options can no longer be traded (market_hours.option_expired) ---

    def test_buying_an_already_expired_option_is_rejected(self):
        """Real incident this guards against: nothing previously stopped a
        BUY/SELL on a contract whose expiration date had already passed --
        the strike-only contract lookup has no way to notice on its own."""
        past = date(2020, 1, 17)
        trade = submit_trade(
            self.competitor, "TEST", Trade.SIDE_BUY, Position.INSTRUMENT_CALL, 1, "thesis",
            strike=Decimal("150"), expiration=past,
        )
        self.assertEqual(trade.status, Trade.STATUS_REJECTED)
        self.assertIn("expired", trade.rejection_reason)
        self.assertEqual(trade.rejection_code, Trade.REJECTION_INVALID_REQUEST)

    def test_selling_an_already_expired_option_is_rejected(self):
        """Closing sides are checked too, not just opening ones -- once a
        short position's own contract has expired, league.tasks.
        settle_expired_options is the only thing allowed to resolve it,
        not a manual trade."""
        past = date(2020, 1, 17)
        Position.objects.create(
            competitor=self.competitor, instrument_type=Position.INSTRUMENT_CALL, ticker="TEST",
            strike=Decimal("150"), expiration=past, quantity=1, is_short=False, avg_cost=Decimal("5"),
        )
        trade = submit_trade(
            self.competitor, "TEST", Trade.SIDE_SELL, Position.INSTRUMENT_CALL, 1, "closing an expired one",
            strike=Decimal("150"), expiration=past,
        )
        self.assertEqual(trade.status, Trade.STATUS_REJECTED)
        self.assertIn("expired", trade.rejection_reason)

    def test_same_day_option_after_the_close_is_rejected(self):
        """A 0DTE contract stops trading at the close of its OWN
        expiration day, not at midnight -- expiration == today alone
        isn't enough to reject (that would break same-day trading before
        the close, which is legitimate)."""
        today = timezone.now().astimezone(EASTERN).date()
        after_close = datetime.combine(today, time(16, 1), tzinfo=EASTERN)
        with patch("league.services.market_hours.timezone.now", return_value=after_close):
            trade = submit_trade(
                self.competitor, "TEST", Trade.SIDE_BUY, Position.INSTRUMENT_CALL, 1, "thesis",
                strike=Decimal("150"), expiration=today,
            )
        self.assertEqual(trade.status, Trade.STATUS_REJECTED)
        self.assertIn("expired", trade.rejection_reason)

    def test_same_day_option_before_the_close_still_fills(self):
        """Regression guard for real 0DTE trading observed in production
        (e.g. same-day SPY/QQQ calls bought and sold hours before their
        own expiration) -- the new expired-contract check must not catch
        this."""
        today = timezone.now().astimezone(EASTERN).date()
        before_close = datetime.combine(today, time(10, 0), tzinfo=EASTERN)
        with patch("league.services.market_hours.timezone.now", return_value=before_close), \
             patch("companies.services.steady.get_options_chain", return_value=_chain(calls=[_contract(150, 5, 4.5)])):
            trade = submit_trade(
                self.competitor, "TEST", Trade.SIDE_BUY, Position.INSTRUMENT_CALL, 1, "0DTE thesis",
                strike=Decimal("150"), expiration=today,
            )
        self.assertEqual(trade.status, Trade.STATUS_FILLED)

    # --- short selling stock disallowed ---

    def test_short_selling_stock_not_permitted(self):
        trade = submit_trade(
            self.competitor, "TEST", Trade.SIDE_SELL_TO_OPEN, Position.INSTRUMENT_STOCK, 10, "bearish",
        )
        self.assertEqual(trade.status, Trade.STATUS_REJECTED)
        self.assertIn("Short selling stock isn't permitted", trade.rejection_reason)

    # --- covered calls ---

    def _sell_covered_call(self, quantity=1, ask=5, bid=4.5, strike=150, thesis="covered call"):
        with patch("companies.services.steady.get_options_chain", return_value=_chain(calls=[_contract(strike, ask, bid)])):
            return submit_trade(
                self.competitor, "TEST", Trade.SIDE_SELL_TO_OPEN, Position.INSTRUMENT_CALL, quantity,
                thesis, strike=Decimal(str(strike)), expiration=EXPIRATION,
            )

    def test_covered_call_rejected_without_shares(self):
        trade = self._sell_covered_call()
        self.assertEqual(trade.status, Trade.STATUS_REJECTED)
        self.assertIn("only 0 available", trade.rejection_reason)

    def test_covered_call_succeeds_with_enough_shares(self):
        self._buy_stock(quantity=100, ask=10, bid=9.9)  # $1000, well under caps
        trade = self._sell_covered_call(quantity=1, ask=5, bid=4.5)
        self.assertEqual(trade.status, Trade.STATUS_FILLED)
        self.assertEqual(trade.fill_price, Decimal("4.5"))  # sell side fills at bid
        short_position = Position.objects.get(competitor=self.competitor, instrument_type=Position.INSTRUMENT_CALL)
        self.assertTrue(short_position.is_short)
        stock_position = Position.objects.get(competitor=self.competitor, instrument_type=Position.INSTRUMENT_STOCK)
        self.assertEqual(short_position.covers_id, stock_position.id)
        self.competitor.refresh_from_db()
        # 5000 - 1000 (buy 100 shares @ $10) + 4.5*1*100 (premium) = 4450
        self.assertEqual(self.competitor.cash_balance, Decimal("4450"))

    def test_covered_call_blocks_selling_the_covering_shares(self):
        self._buy_stock(quantity=100, ask=10, bid=9.9)
        self._sell_covered_call(quantity=1)
        with patch("companies.services.steady.get_quote", return_value=_quote(ask=12, bid=11)):
            trade = submit_trade(self.competitor, "TEST", Trade.SIDE_SELL, Position.INSTRUMENT_STOCK, 100, "exit")
        self.assertEqual(trade.status, Trade.STATUS_REJECTED)
        self.assertIn("uncovered", trade.rejection_reason)

    def test_covered_call_double_cover_rejected_once_shares_exhausted(self):
        self._buy_stock(quantity=200, ask=10, bid=9.9)
        first = self._sell_covered_call(quantity=1)
        self.assertEqual(first.status, Trade.STATUS_FILLED)
        second = self._sell_covered_call(quantity=1)
        self.assertEqual(second.status, Trade.STATUS_FILLED)  # 200 shares covers 2 contracts
        third = self._sell_covered_call(quantity=1)
        self.assertEqual(third.status, Trade.STATUS_REJECTED)
        self.assertIn("only 0 available", third.rejection_reason)

    def test_buy_to_close_short_call(self):
        self._buy_stock(quantity=100, ask=10, bid=9.9)
        self._sell_covered_call(quantity=1, ask=5, bid=4.5)
        with patch("companies.services.steady.get_options_chain", return_value=_chain(calls=[_contract(150, 3, 2.5)])):
            trade = submit_trade(
                self.competitor, "TEST", Trade.SIDE_BUY_TO_CLOSE, Position.INSTRUMENT_CALL, 1, "closing early",
                strike=Decimal("150"), expiration=EXPIRATION,
            )
        self.assertEqual(trade.status, Trade.STATUS_FILLED)
        self.assertEqual(trade.fill_price, Decimal("3"))  # buy side fills at ask
        self.assertFalse(Position.objects.filter(competitor=self.competitor, instrument_type=Position.INSTRUMENT_CALL).exists())

    # --- cash-covered puts ---

    def _sell_cash_covered_put(self, quantity=1, ask=5, bid=4.5, strike=100, thesis="csp"):
        with patch("companies.services.steady.get_options_chain", return_value=_chain(puts=[_contract(strike, ask, bid, "Put")])):
            return submit_trade(
                self.competitor, "TEST", Trade.SIDE_SELL_TO_OPEN, Position.INSTRUMENT_PUT, quantity,
                thesis, strike=Decimal(str(strike)), expiration=EXPIRATION,
            )

    def test_cash_covered_put_requires_collateral(self):
        # strike 100 * 100 * 1 contract = $10,000 collateral, more than the $5000 account.
        trade = self._sell_cash_covered_put(strike=100)
        self.assertEqual(trade.status, Trade.STATUS_REJECTED)
        self.assertIn("collateral", trade.rejection_reason)

    def test_cash_covered_put_succeeds_with_enough_cash(self):
        # strike 40 * 100 * 1 = $4000 collateral, fits in $5000 cash.
        trade = self._sell_cash_covered_put(strike=40, ask=2, bid=1.5)
        self.assertEqual(trade.status, Trade.STATUS_FILLED)
        self.assertEqual(trade.fill_price, Decimal("1.5"))
        self.competitor.refresh_from_db()
        # 5000 + 1.5*1*100 premium = 5150 (collateral is reserved via the
        # aggregate check, not physically debited -- selling the put is a
        # cash CREDIT, same as any real cash-covered put).
        self.assertEqual(self.competitor.cash_balance, Decimal("5150"))

    def test_cash_covered_put_collateral_pools_across_positions(self):
        # First put uses $4000 of collateral (strike 40); a second put
        # needing another $2000 (strike 20) should be rejected since only
        # ~$1150 of headroom remains ($5000 + $150 premium - $4000 reserved).
        first = self._sell_cash_covered_put(strike=40, ask=2, bid=1.5)
        self.assertEqual(first.status, Trade.STATUS_FILLED)
        second = self._sell_cash_covered_put(strike=20, ask=1, bid=0.9)
        self.assertEqual(second.status, Trade.STATUS_REJECTED)
        self.assertIn("collateral", second.rejection_reason)

    # --- misc validation ---

    def test_missing_thesis_rejected(self):
        trade = self._buy_stock(thesis="")
        self.assertEqual(trade.status, Trade.STATUS_REJECTED)
        self.assertIn("thesis", trade.rejection_reason.lower())

    def test_zero_quantity_rejected(self):
        trade = self._buy_stock(quantity=0)
        self.assertEqual(trade.status, Trade.STATUS_REJECTED)

    def test_rejected_trade_still_persisted_for_audit(self):
        before = Trade.objects.count()
        self._buy_stock(quantity=31, ask=100, bid=99)  # over the position cap
        self.assertEqual(Trade.objects.count(), before + 1)
        self.assertEqual(Trade.objects.last().status, Trade.STATUS_REJECTED)

    def test_insufficient_cash_rejected(self):
        # With zero existing positions, account_value == cash_balance, so
        # the 60% position cap is always stricter than the raw cash check
        # and would trigger first -- that's not what this test is for. To
        # isolate the cash check, give the account real marked-up value
        # from an EXISTING position (so the cap passes) while cash itself
        # stays too low to pay for the new buy: cash=$100, a held position
        # worth $4500 -> account_value=$4600, 60% cap=$2760, well above the
        # $500 new buy -- but only $100 of that value is liquid cash.
        self.competitor.cash_balance = Decimal("100")
        self.competitor.save()
        Position.objects.create(
            competitor=self.competitor, instrument_type=Position.INSTRUMENT_STOCK,
            ticker="OTHER", quantity=90, avg_cost=Decimal("50"),
        )
        with patch("companies.services.steady.get_quote", return_value=_quote(ask=100, bid=99, last=50)):
            trade = submit_trade(
                self.competitor, "TEST", Trade.SIDE_BUY, Position.INSTRUMENT_STOCK, 5, "test thesis",
            )
        self.assertEqual(trade.status, Trade.STATUS_REJECTED)
        self.assertIn("available cash", trade.rejection_reason)

    # --- live trade feed broadcast ---

    def test_filled_trade_broadcasts_to_its_competition_group(self):
        with patch("realtime.utils.broadcast_to_group") as mock_broadcast:
            trade = self._buy_stock(ticker="TEST", quantity=10, ask=100, bid=99)
        self.assertEqual(trade.status, Trade.STATUS_FILLED)
        mock_broadcast.assert_called_once()
        (group_name, message_type), payload = mock_broadcast.call_args
        self.assertEqual(group_name, f"league_{self.competition.id}")
        self.assertEqual(message_type, "league.trade")
        self.assertEqual(payload["ticker"], "TEST")
        self.assertEqual(payload["status"], Trade.STATUS_FILLED)
        self.assertEqual(payload["competitor_name"], "Tester")

    def test_rejected_trade_also_broadcasts(self):
        with patch("realtime.utils.broadcast_to_group") as mock_broadcast:
            trade = self._buy_stock(quantity=10, ask=100, bid=99, limit_price=Decimal("90"))
        self.assertEqual(trade.status, Trade.STATUS_REJECTED)
        mock_broadcast.assert_called_once()
        payload = mock_broadcast.call_args.kwargs
        self.assertEqual(payload["status"], Trade.STATUS_REJECTED)

    def test_broadcast_failure_does_not_break_the_trade(self):
        """_broadcast_trade is best-effort -- a Redis/Channels outage must
        never fail an actual fill, only cost that row its live push."""
        with patch("realtime.utils.broadcast_to_group", side_effect=RuntimeError("redis down")):
            trade = self._buy_stock(quantity=10, ask=100, bid=99)
        self.assertEqual(trade.status, Trade.STATUS_FILLED)

    # --- persisted-snapshot mark-to-market (the scoreboard/competitor-list
    #     13-14s N+1 fix: display reads TickerSnapshot, never live Steady) ---

    def test_account_value_reads_persisted_snapshot_never_calls_steady_live(self):
        """Two open positions on the same ticker (e.g. left over from
        separate lots), or a hundred across many tickers, cost zero live
        Steady calls to mark-to-market once account_value() reads off
        TickerSnapshot -- this is the actual fix for the endpoints that
        used to take 13-14s (one live round trip per open Position, every
        page load). Positions are created directly (not via submit_trade)
        so setup itself doesn't warm the snapshot via a fill."""
        Position.objects.create(
            competitor=self.competitor, instrument_type=Position.INSTRUMENT_STOCK,
            ticker="DUP", quantity=5, avg_cost=Decimal("100"),
        )
        Position.objects.create(
            competitor=self.competitor, instrument_type=Position.INSTRUMENT_STOCK,
            ticker="DUP", quantity=3, avg_cost=Decimal("100"),
        )
        valuation.save_quote_snapshot("DUP", _quote(ask=110, bid=108, last=109))
        with patch("companies.services.steady.get_quote") as mock_get_quote:
            value = valuation.account_value(self.competitor)
        mock_get_quote.assert_not_called()
        self.assertEqual(value, self.competitor.cash_balance + Decimal("109") * 8)

    def test_mark_position_detail_falls_back_to_avg_cost_when_no_snapshot_yet(self):
        """A ticker that's never been fetched (brand new to the app, no
        fill yet, Beat hasn't ticked yet) marks at avg_cost rather than
        raising or blocking -- same graceful-degradation shape the old
        live-call fallback had, just for a missing row instead of a
        failed request."""
        position = Position.objects.create(
            competitor=self.competitor, instrument_type=Position.INSTRUMENT_STOCK,
            ticker="NEVERFETCHED", quantity=4, avg_cost=Decimal("42.50"),
        )
        with patch("companies.services.steady.get_quote") as mock_get_quote:
            price, value, priced_at = valuation.mark_position_detail(position)
        mock_get_quote.assert_not_called()
        self.assertEqual(price, Decimal("42.50"))
        self.assertEqual(value, Decimal("170.00"))
        self.assertIsNone(priced_at)

    def test_mark_position_detail_marks_an_expired_option_at_zero_not_avg_cost(self):
        """avg_cost is what was paid at open -- economically meaningless
        for a contract that has stopped trading. Before this, an expired
        long option with no live chain snapshot kept counting as its full
        avg_cost toward account value indefinitely (until the nightly
        settlement task got to it), inflating both the scoreboard and the
        60%/30% entry caps for every trade after it."""
        position = Position.objects.create(
            competitor=self.competitor, instrument_type=Position.INSTRUMENT_CALL, ticker="TEST",
            strike=Decimal("150"), expiration=date(2020, 1, 17), quantity=2, is_short=False,
            avg_cost=Decimal("14"),
        )
        price, value, priced_at = valuation.mark_position_detail(position)
        self.assertEqual(price, Decimal("0"))
        self.assertEqual(value, Decimal("0"))
        self.assertIsNone(priced_at)

    def test_submit_trade_persists_a_ticker_snapshot_from_the_fill(self):
        """A fill already pays for a live Steady round trip for pricing --
        execution.py reuses that same payload to warm TickerSnapshot, so a
        brand-new position shows a fresh mark immediately instead of
        avg_cost until the next 10-minute Beat tick."""
        self._buy_stock(ticker="FRESH", quantity=5, ask=100, bid=99)
        snapshot = TickerSnapshot.objects.get(ticker="FRESH", data_type="quote")
        self.assertEqual(snapshot.raw_json["ask"], 100)
        self.assertEqual(snapshot.raw_json["bid"], 99)

    def test_persisted_snapshot_does_not_affect_trade_fill_pricing(self):
        """A persisted display snapshot must never leak into an actual
        fill -- a trade submitted after a stale-ish snapshot still prices
        off a fresh live quote at execution time."""
        self._buy_stock(ticker="LIVE", quantity=5, ask=100, bid=99)
        valuation.save_quote_snapshot("LIVE", _quote(ask=100, bid=99))  # display snapshot @ $100

        with patch("companies.services.steady.get_quote", return_value=_quote(ask=150, bid=148)):
            trade = submit_trade(self.competitor, "LIVE", Trade.SIDE_BUY, Position.INSTRUMENT_STOCK, 1, "more")
        self.assertEqual(trade.fill_price, Decimal("150"))  # live ask, not the $100 snapshot

    def test_mark_position_detail_returns_the_snapshots_fetched_at(self):
        """priced_at should reflect the backing TickerSnapshot's fetched_at
        when a live mark is actually used -- the "how stale is this"
        signal CompetitorDetailView surfaces as prices_as_of."""
        with patch("companies.services.steady.get_quote", return_value=_quote(ask=100, bid=99)):
            trade = self._buy_stock(ticker="TIMED", quantity=1, ask=100, bid=99)
        position = Position.objects.get(competitor=self.competitor, ticker="TIMED")
        price, value, priced_at = valuation.mark_position_detail(position)
        snapshot = TickerSnapshot.objects.get(ticker="TIMED", data_type="quote")
        self.assertEqual(priced_at, snapshot.fetched_at)

    # --- target/dead price levels (Position.target_price/dead_price) --
    #     purely informational, never enforced by execution.py itself ---

    def test_buy_records_target_and_dead_price_on_a_new_position(self):
        with patch("companies.services.steady.get_quote", return_value=_quote(ask=100, bid=99)):
            submit_trade(
                self.competitor, "TGT", Trade.SIDE_BUY, Position.INSTRUMENT_STOCK, 10, "thesis",
                target_price=Decimal("120"), dead_price=Decimal("90"),
            )
        position = Position.objects.get(competitor=self.competitor, ticker="TGT")
        self.assertEqual(position.target_price, Decimal("120"))
        self.assertEqual(position.dead_price, Decimal("90"))

    def test_adding_to_a_position_without_targets_leaves_existing_ones_alone(self):
        with patch("companies.services.steady.get_quote", return_value=_quote(ask=100, bid=99)):
            submit_trade(
                self.competitor, "TGT2", Trade.SIDE_BUY, Position.INSTRUMENT_STOCK, 10, "thesis",
                target_price=Decimal("120"), dead_price=Decimal("90"),
            )
        with patch("companies.services.steady.get_quote", return_value=_quote(ask=105, bid=104)):
            submit_trade(self.competitor, "TGT2", Trade.SIDE_BUY, Position.INSTRUMENT_STOCK, 5, "adding more")
        position = Position.objects.get(competitor=self.competitor, ticker="TGT2")
        self.assertEqual(position.quantity, 15)
        self.assertEqual(position.target_price, Decimal("120"))  # untouched by the second, target-less buy
        self.assertEqual(position.dead_price, Decimal("90"))

    def test_adding_to_a_position_can_revise_targets(self):
        with patch("companies.services.steady.get_quote", return_value=_quote(ask=100, bid=99)):
            submit_trade(
                self.competitor, "TGT3", Trade.SIDE_BUY, Position.INSTRUMENT_STOCK, 10, "thesis",
                target_price=Decimal("120"), dead_price=Decimal("90"),
            )
        with patch("companies.services.steady.get_quote", return_value=_quote(ask=105, bid=104)):
            submit_trade(
                self.competitor, "TGT3", Trade.SIDE_BUY, Position.INSTRUMENT_STOCK, 5, "raising targets",
                target_price=Decimal("130"),
            )
        position = Position.objects.get(competitor=self.competitor, ticker="TGT3")
        self.assertEqual(position.target_price, Decimal("130"))  # revised
        self.assertEqual(position.dead_price, Decimal("90"))  # left alone (not passed this time)


class AfterHoursQueuingTests(TestCase):
    """Competition.restrict_after_hours_trading -- simulates a broker with
    no extended-hours trading (e.g. Fidelity): a trade submitted while the
    market's closed queues as Trade.STATUS_PENDING instead of filling,
    resolved later by league.tasks.process_pending_trades once the market
    reopens. Both execution.py and tasks.py import the SAME market_hours
    module object, so patching league.services.market_hours.is_market_open
    once covers submit_trade's queuing decision AND process_pending_trades'
    own gate."""

    def setUp(self):
        self.competition = Competition.objects.create(
            name="After Hours League", starting_capital=Decimal("5000"),
            start_date=date(2026, 8, 12), end_date=date(2026, 11, 4),
            restrict_after_hours_trading=True,
        )
        self.competitor = Competitor.objects.create(
            competition=self.competition, name="Tester", kind=Competitor.KIND_AI,
            cash_balance=self.competition.starting_capital,
        )

    def test_queues_when_restricted_and_market_closed(self):
        with patch("league.services.market_hours.is_market_open", return_value=False), \
             patch("companies.services.steady.get_quote", return_value=_quote(ask=100, bid=99)) as mock_get_quote:
            trade = submit_trade(
                self.competitor, "TEST", Trade.SIDE_BUY, Position.INSTRUMENT_STOCK, 10, "after hours buy",
            )
        self.assertEqual(trade.status, Trade.STATUS_PENDING)
        self.assertIsNone(trade.fill_price)
        mock_get_quote.assert_not_called()  # _fill_price is only ever reached inside _execute_trade
        self.competitor.refresh_from_db()
        self.assertEqual(self.competitor.cash_balance, self.competition.starting_capital)
        self.assertFalse(Position.objects.filter(competitor=self.competitor, ticker="TEST").exists())

    def test_fills_immediately_when_restricted_but_market_open(self):
        with patch("league.services.market_hours.is_market_open", return_value=True), \
             patch("companies.services.steady.get_quote", return_value=_quote(ask=100, bid=99)):
            trade = submit_trade(
                self.competitor, "TEST", Trade.SIDE_BUY, Position.INSTRUMENT_STOCK, 10, "market hours buy",
            )
        self.assertEqual(trade.status, Trade.STATUS_FILLED)
        self.assertEqual(trade.fill_price, Decimal("100"))

    def test_fills_immediately_when_not_restricted_even_if_market_closed(self):
        self.competition.restrict_after_hours_trading = False
        self.competition.save(update_fields=["restrict_after_hours_trading"])
        with patch("league.services.market_hours.is_market_open", return_value=False), \
             patch("companies.services.steady.get_quote", return_value=_quote(ask=100, bid=99)):
            trade = submit_trade(
                self.competitor, "TEST", Trade.SIDE_BUY, Position.INSTRUMENT_STOCK, 10, "unrestricted buy",
            )
        self.assertEqual(trade.status, Trade.STATUS_FILLED)

    def test_two_pending_trades_same_competitor_dont_both_spend_stale_cash(self):
        """Two orders queued before the open (STATUS_PENDING) for the SAME
        competitor, both resolved in the same process_pending_trades()
        drain: process_pending_trades loads its whole batch via one
        select_related("competitor__...") query, so each Trade arrives at
        _execute_trade holding a SEPARATE competitor object, both stamped
        with cash_balance as of that single shared query -- before either
        fill has touched the DB. Without a fresh per-trade re-read, the
        second trade's cash check validates against pre-fill cash and its
        save() clobbers the first trade's deduction (a lost update): both
        fill using the SAME starting cash, one of them effectively free.
        30 shares @ $100 = $3000 notional, exactly the 60% single-position
        cap on $5000 starting capital, so both pass the cap check
        individually; combined ($6000) exceeds the $5000 cash actually
        available, so the second must re-validate against what the first
        left behind and be rejected -- insufficient cash, or the position
        cap once the first fill's Position marks account value down (bid
        vs. the $100 paid), either is a legitimate re-validation outcome
        -- not silently fill against the original $5000."""
        with patch("league.services.market_hours.is_market_open", return_value=False), \
             patch("companies.services.steady.get_quote", return_value=_quote(ask=100, bid=99)):
            first = submit_trade(
                self.competitor, "TEST", Trade.SIDE_BUY, Position.INSTRUMENT_STOCK, 30, "queued buy 1",
            )
            second = submit_trade(
                self.competitor, "TEST", Trade.SIDE_BUY, Position.INSTRUMENT_STOCK, 30, "queued buy 2",
            )
        with patch("league.services.market_hours.is_market_open", return_value=True), \
             patch("companies.services.steady.get_quote", return_value=_quote(ask=100, bid=99)):
            process_pending_trades()
        first.refresh_from_db()
        second.refresh_from_db()
        self.assertEqual(first.status, Trade.STATUS_FILLED)
        self.assertEqual(second.status, Trade.STATUS_REJECTED)
        self.assertIn(
            second.rejection_code, (Trade.REJECTION_INSUFFICIENT_CASH, Trade.REJECTION_POSITION_CAP),
        )
        self.competitor.refresh_from_db()
        self.assertEqual(self.competitor.cash_balance, Decimal("2000"))
        position = Position.objects.get(competitor=self.competitor, ticker="TEST")
        self.assertEqual(position.quantity, 30)

    def test_static_validation_failure_never_queues(self):
        """A thesis-less trade is rejected outright regardless of market
        hours -- static validation runs before the queuing decision."""
        with patch("league.services.market_hours.is_market_open", return_value=False):
            trade = submit_trade(self.competitor, "TEST", Trade.SIDE_BUY, Position.INSTRUMENT_STOCK, 10, "")
        self.assertEqual(trade.status, Trade.STATUS_REJECTED)

    def test_process_pending_trades_noop_when_market_closed(self):
        with patch("league.services.market_hours.is_market_open", return_value=False):
            pending = submit_trade(self.competitor, "TEST", Trade.SIDE_BUY, Position.INSTRUMENT_STOCK, 10, "queued")
            process_pending_trades()
        pending.refresh_from_db()
        self.assertEqual(pending.status, Trade.STATUS_PENDING)

    def test_process_pending_trades_resolves_when_market_open(self):
        with patch("league.services.market_hours.is_market_open", return_value=False):
            pending = submit_trade(self.competitor, "TEST", Trade.SIDE_BUY, Position.INSTRUMENT_STOCK, 10, "queued")
        with patch("league.services.market_hours.is_market_open", return_value=True), \
             patch("companies.services.steady.get_quote", return_value=_quote(ask=100, bid=99)):
            process_pending_trades()
        pending.refresh_from_db()
        self.assertEqual(pending.status, Trade.STATUS_FILLED)
        self.assertEqual(pending.fill_price, Decimal("100"))
        self.competitor.refresh_from_db()
        self.assertEqual(self.competitor.cash_balance, Decimal("4000"))
        position = Position.objects.get(competitor=self.competitor, ticker="TEST")
        self.assertEqual(position.quantity, 10)

    def test_process_pending_trades_rejects_if_revalidation_fails(self):
        """A pending BUY that no longer fits available cash by the time the
        queue drains is genuinely re-validated at resolution, not rubber-
        stamped from whatever was true at submission. A large existing
        Position (marked at avg_cost -- no live quote needed, see
        valuation.mark_position_detail's fallback) keeps account_value --
        and so the position-size cap -- comfortably satisfied, isolating
        this down to specifically the cash check."""
        Position.objects.create(
            competitor=self.competitor, instrument_type=Position.INSTRUMENT_STOCK,
            ticker="OTHER", quantity=100, avg_cost=Decimal("100"),
        )
        with patch("league.services.market_hours.is_market_open", return_value=False):
            pending = submit_trade(self.competitor, "TEST", Trade.SIDE_BUY, Position.INSTRUMENT_STOCK, 10, "queued")
        self.competitor.cash_balance = Decimal("1")
        self.competitor.save(update_fields=["cash_balance"])
        with patch("league.services.market_hours.is_market_open", return_value=True), \
             patch("companies.services.steady.get_quote", return_value=_quote(ask=100, bid=99)):
            process_pending_trades()
        pending.refresh_from_db()
        self.assertEqual(pending.status, Trade.STATUS_REJECTED)
        self.assertEqual(pending.rejection_code, Trade.REJECTION_INSUFFICIENT_CASH)

    def test_sell_all_positions_force_immediate_bypasses_queue(self):
        with patch("league.services.market_hours.is_market_open", return_value=True), \
             patch("companies.services.steady.get_quote", return_value=_quote(ask=100, bid=99)):
            submit_trade(self.competitor, "TEST", Trade.SIDE_BUY, Position.INSTRUMENT_STOCK, 10, "open position")
        out = StringIO()
        with patch("league.services.market_hours.is_market_open", return_value=False), \
             patch("companies.services.steady.get_quote", return_value=_quote(ask=105, bid=102)):
            call_command("sell_all_positions", "--competitor", str(self.competitor.id), stdout=out)
        self.assertFalse(Position.objects.filter(competitor=self.competitor, ticker="TEST").exists())
        closing_trade = Trade.objects.filter(competitor=self.competitor, side=Trade.SIDE_SELL).latest("created_at")
        self.assertEqual(closing_trade.status, Trade.STATUS_FILLED)

    def test_cancel_pending_trade_cancels_without_touching_cash_or_position(self):
        with patch("league.services.market_hours.is_market_open", return_value=False):
            pending = submit_trade(self.competitor, "TEST", Trade.SIDE_BUY, Position.INSTRUMENT_STOCK, 10, "queued")
        cancelled = execution.cancel_pending_trade(pending)
        self.assertEqual(cancelled.status, Trade.STATUS_CANCELLED)
        self.competitor.refresh_from_db()
        self.assertEqual(self.competitor.cash_balance, self.competition.starting_capital)
        self.assertFalse(Position.objects.filter(competitor=self.competitor, ticker="TEST").exists())

    def test_cancel_pending_trade_raises_when_already_filled(self):
        with patch("league.services.market_hours.is_market_open", return_value=True), \
             patch("companies.services.steady.get_quote", return_value=_quote(ask=100, bid=99)):
            trade = submit_trade(self.competitor, "TEST", Trade.SIDE_BUY, Position.INSTRUMENT_STOCK, 10, "filled")
        self.assertEqual(trade.status, Trade.STATUS_FILLED)
        with self.assertRaises(execution.TradeAlreadyResolved):
            execution.cancel_pending_trade(trade)

    def test_execute_trade_backs_off_if_cancelled_out_from_under_it(self):
        """Simulates the race process_pending_trades can hit against a
        concurrent cancel: `stale_copy` stands in for the Trade instance
        process_pending_trades already fetched (still PENDING) before
        another request cancelled the same row out from under it --
        _execute_trade's fresh-read guard must back off instead of
        overwriting that cancellation with a fill."""
        with patch("league.services.market_hours.is_market_open", return_value=False):
            pending = submit_trade(self.competitor, "TEST", Trade.SIDE_BUY, Position.INSTRUMENT_STOCK, 10, "queued")
        stale_copy = Trade.objects.get(id=pending.id)
        execution.cancel_pending_trade(pending)
        with patch("companies.services.steady.get_quote", return_value=_quote(ask=100, bid=99)):
            result = execution.resolve_pending_trade(stale_copy)
        self.assertEqual(result.status, Trade.STATUS_CANCELLED)
        self.competitor.refresh_from_db()
        self.assertEqual(self.competitor.cash_balance, self.competition.starting_capital)


class MarketHoursTests(TestCase):
    def test_is_market_open_during_session_on_a_weekday(self):
        ts = datetime.combine(date(2026, 8, 13), time(10, 0), tzinfo=EASTERN)  # Thursday
        self.assertTrue(market_hours.is_market_open(ts))

    def test_is_market_open_false_outside_session_on_a_weekday(self):
        ts = datetime.combine(date(2026, 8, 13), time(20, 0), tzinfo=EASTERN)
        self.assertFalse(market_hours.is_market_open(ts))

    def test_is_market_open_false_on_a_weekend(self):
        ts = datetime.combine(date(2026, 8, 15), time(12, 0), tzinfo=EASTERN)  # Saturday
        self.assertFalse(market_hours.is_market_open(ts))


class SettleExpiredOptionsTests(TestCase):
    """execution.settle_expired_position -- the only thing in this app that
    resolves a call/put (long or short) past its own expiration (see that
    function's docstring). Every case here calls it directly, not through
    league.tasks.settle_expired_options, since the task is just "fetch a
    closing price per ticker, then call this" -- the settlement logic
    itself is what needs the coverage."""

    def setUp(self):
        self.competition = Competition.objects.create(
            name="Test League", starting_capital=Decimal("5000"),
            max_position_pct=Decimal("0.60"), max_index_pct=Decimal("0.30"),
            start_date=date(2026, 8, 12), end_date=date(2026, 11, 4),
        )
        self.competitor = Competitor.objects.create(
            competition=self.competition, name="Tester", kind=Competitor.KIND_AI,
            cash_balance=self.competition.starting_capital,
        )

    def _buy_stock(self, quantity=100, ask=10, bid=9.9):
        with patch("companies.services.steady.get_quote", return_value=_quote(ask, bid)):
            return submit_trade(
                self.competitor, "TEST", Trade.SIDE_BUY, Position.INSTRUMENT_STOCK, quantity, "test thesis",
            )

    def _sell_covered_call(self, quantity=1, ask=5, bid=4.5, strike=150):
        with patch("companies.services.steady.get_options_chain", return_value=_chain(calls=[_contract(strike, ask, bid)])):
            return submit_trade(
                self.competitor, "TEST", Trade.SIDE_SELL_TO_OPEN, Position.INSTRUMENT_CALL, quantity,
                "covered call", strike=Decimal(str(strike)), expiration=EXPIRATION,
            )

    def _sell_cash_covered_put(self, quantity=1, ask=2, bid=1.5, strike=40):
        with patch("companies.services.steady.get_options_chain", return_value=_chain(puts=[_contract(strike, ask, bid, "Put")])):
            return submit_trade(
                self.competitor, "TEST", Trade.SIDE_SELL_TO_OPEN, Position.INSTRUMENT_PUT, quantity,
                "cash-covered put", strike=Decimal(str(strike)), expiration=EXPIRATION,
            )

    def test_itm_call_assigned_delivers_shares_at_strike(self):
        self._buy_stock(quantity=100, ask=10, bid=9.9)  # cash: 5000 - 1000 = 4000
        self._sell_covered_call(quantity=1, ask=5, bid=4.5, strike=150)  # cash: 4000 + 450 = 4450
        short_call = Position.objects.get(competitor=self.competitor, instrument_type=Position.INSTRUMENT_CALL)

        trades = execution.settle_expired_position(short_call, closing_price=Decimal("160"))

        self.assertEqual(len(trades), 2)
        self.assertFalse(Position.objects.filter(id=short_call.id).exists())
        self.assertFalse(Position.objects.filter(competitor=self.competitor, instrument_type=Position.INSTRUMENT_STOCK).exists())
        self.competitor.refresh_from_db()
        # 4450 + 150*100 (shares delivered at strike, not the $160 market price) = 19450
        self.assertEqual(self.competitor.cash_balance, Decimal("19450"))
        option_trade = next(t for t in trades if t.instrument_type == Position.INSTRUMENT_CALL)
        self.assertEqual(option_trade.side, Trade.SIDE_BUY_TO_CLOSE)
        self.assertEqual(option_trade.fill_price, Decimal("0"))
        stock_trade = next(t for t in trades if t.instrument_type == Position.INSTRUMENT_STOCK)
        self.assertEqual(stock_trade.side, Trade.SIDE_SELL)
        self.assertEqual(stock_trade.fill_price, Decimal("150"))

    def test_otm_call_expires_worthless_leaves_shares_untouched(self):
        self._buy_stock(quantity=100, ask=10, bid=9.9)
        self._sell_covered_call(quantity=1, ask=5, bid=4.5, strike=150)
        short_call = Position.objects.get(competitor=self.competitor, instrument_type=Position.INSTRUMENT_CALL)
        cash_before = self.competitor.cash_balance

        trades = execution.settle_expired_position(short_call, closing_price=Decimal("140"))

        self.assertEqual(len(trades), 1)
        self.assertEqual(trades[0].side, Trade.SIDE_BUY_TO_CLOSE)
        self.assertEqual(trades[0].fill_price, Decimal("0"))
        self.assertFalse(Position.objects.filter(id=short_call.id).exists())
        stock_position = Position.objects.get(competitor=self.competitor, instrument_type=Position.INSTRUMENT_STOCK)
        self.assertEqual(stock_position.quantity, 100)  # untouched
        self.competitor.refresh_from_db()
        self.assertEqual(self.competitor.cash_balance, cash_before)  # closing at $0 doesn't move cash further

    def test_call_exactly_at_strike_expires_worthless(self):
        # Pins the convention: exactly-at-strike is OTM (expires worthless),
        # not assigned, for both calls and puts (plain >/< against strike,
        # not >=/<=).
        self._buy_stock(quantity=100, ask=10, bid=9.9)
        self._sell_covered_call(quantity=1, ask=5, bid=4.5, strike=150)
        short_call = Position.objects.get(competitor=self.competitor, instrument_type=Position.INSTRUMENT_CALL)

        trades = execution.settle_expired_position(short_call, closing_price=Decimal("150"))

        self.assertEqual(len(trades), 1)
        stock_position = Position.objects.get(competitor=self.competitor, instrument_type=Position.INSTRUMENT_STOCK)
        self.assertEqual(stock_position.quantity, 100)

    def test_itm_put_assigned_buys_shares_at_strike(self):
        self._sell_cash_covered_put(quantity=1, ask=2, bid=1.5, strike=40)  # cash: 5000 + 150 = 5150
        short_put = Position.objects.get(competitor=self.competitor, instrument_type=Position.INSTRUMENT_PUT)

        trades = execution.settle_expired_position(short_put, closing_price=Decimal("35"))

        self.assertEqual(len(trades), 2)
        self.assertFalse(Position.objects.filter(id=short_put.id).exists())
        stock_position = Position.objects.get(competitor=self.competitor, instrument_type=Position.INSTRUMENT_STOCK)
        self.assertEqual(stock_position.quantity, 100)
        self.assertEqual(stock_position.avg_cost, Decimal("40"))
        self.assertFalse(stock_position.is_short)
        self.competitor.refresh_from_db()
        # 5150 - 40*100 (shares put to us at strike) = 1150
        self.assertEqual(self.competitor.cash_balance, Decimal("1150"))
        stock_trade = next(t for t in trades if t.instrument_type == Position.INSTRUMENT_STOCK)
        self.assertEqual(stock_trade.side, Trade.SIDE_BUY)
        self.assertEqual(stock_trade.fill_price, Decimal("40"))

    def test_otm_put_expires_worthless_no_shares_created(self):
        self._sell_cash_covered_put(quantity=1, ask=2, bid=1.5, strike=40)
        short_put = Position.objects.get(competitor=self.competitor, instrument_type=Position.INSTRUMENT_PUT)
        cash_before = self.competitor.cash_balance

        trades = execution.settle_expired_position(short_put, closing_price=Decimal("45"))

        self.assertEqual(len(trades), 1)
        self.assertFalse(Position.objects.filter(competitor=self.competitor, instrument_type=Position.INSTRUMENT_STOCK).exists())
        self.competitor.refresh_from_db()
        self.assertEqual(self.competitor.cash_balance, cash_before)

    def test_uses_covers_fk_not_by_ticker_lookup_for_call_assignment(self):
        # Two separate stock lots for the same ticker (Position's own
        # docstring: "there should only ever be one... but summing is
        # defensive" -- constructed directly here, since submit_trade's
        # own BUY path always merges into an existing lot for the same
        # ticker via _find_position, so this shape can't arise through
        # submit_trade itself). The short call's `covers` FK is set
        # explicitly to `other_lot`, NOT the larger `covered_lot` -- if
        # settlement used a fresh by-ticker lookup instead of following
        # `covers`, Position's default ordering (-opened_at) would resolve
        # to `covered_lot` (created second) and deduct from the wrong row.
        other_lot = Position.objects.create(
            competitor=self.competitor, instrument_type=Position.INSTRUMENT_STOCK, ticker="TEST",
            quantity=100, is_short=False, avg_cost=Decimal("8"),
        )
        covered_lot = Position.objects.create(
            competitor=self.competitor, instrument_type=Position.INSTRUMENT_STOCK, ticker="TEST",
            quantity=50, is_short=False, avg_cost=Decimal("12"),
        )
        short_call = Position.objects.create(
            competitor=self.competitor, instrument_type=Position.INSTRUMENT_CALL, ticker="TEST",
            strike=Decimal("150"), expiration=EXPIRATION, quantity=1, is_short=True,
            avg_cost=Decimal("4.5"), covers=other_lot,
        )

        execution.settle_expired_position(short_call, closing_price=Decimal("160"))

        self.assertFalse(Position.objects.filter(id=other_lot.id).exists())  # fully delivered
        covered_lot.refresh_from_db()
        self.assertEqual(covered_lot.quantity, 50)  # untouched

    # --- long options: cash-settled to intrinsic value, never exercised
    # into a stock position (added alongside submit_trade's own
    # option_expired rejection -- before this, a long call/put that
    # expired simply stayed open forever, since nothing else in the app
    # ever touched it and it could no longer be closed manually either) ---

    def _buy_call(self, quantity=2, ask=14, bid=13.5, strike=220, expiration=None):
        with patch(
            "companies.services.steady.get_options_chain",
            return_value=_chain(calls=[_contract(strike, ask, bid)]),
        ):
            return submit_trade(
                self.competitor, "TEST", Trade.SIDE_BUY, Position.INSTRUMENT_CALL, quantity,
                "long call thesis", strike=Decimal(str(strike)), expiration=expiration or EXPIRATION,
            )

    def _buy_put(self, quantity=1, ask=2, bid=1.5, strike=40, expiration=None):
        with patch(
            "companies.services.steady.get_options_chain",
            return_value=_chain(puts=[_contract(strike, ask, bid, "Put")]),
        ):
            return submit_trade(
                self.competitor, "TEST", Trade.SIDE_BUY, Position.INSTRUMENT_PUT, quantity,
                "long put thesis", strike=Decimal(str(strike)), expiration=expiration or EXPIRATION,
            )

    def test_itm_long_call_cash_settles_to_intrinsic_value_not_exercised(self):
        self._buy_call(quantity=2, ask=14, bid=13.5, strike=220)  # cash: 5000 - 14*2*100 = 2200
        long_call = Position.objects.get(competitor=self.competitor, instrument_type=Position.INSTRUMENT_CALL)

        trades = execution.settle_expired_position(long_call, closing_price=Decimal("235"))

        self.assertEqual(len(trades), 1)
        self.assertFalse(Position.objects.filter(id=long_call.id).exists())
        # Never exercised into shares -- no stock position should exist.
        self.assertFalse(Position.objects.filter(competitor=self.competitor, instrument_type=Position.INSTRUMENT_STOCK).exists())
        self.competitor.refresh_from_db()
        # 2200 + intrinsic(235-220=15) * 2 * 100 = 2200 + 3000 = 5200
        self.assertEqual(self.competitor.cash_balance, Decimal("5200"))
        trade = trades[0]
        self.assertEqual(trade.side, Trade.SIDE_SELL)
        self.assertEqual(trade.fill_price, Decimal("15"))
        self.assertIn("cash-settled", trade.thesis)

    def test_otm_long_call_expires_worthless_no_cash_change(self):
        self._buy_call(quantity=2, ask=14, bid=13.5, strike=220)
        long_call = Position.objects.get(competitor=self.competitor, instrument_type=Position.INSTRUMENT_CALL)
        cash_before = self.competitor.cash_balance

        trades = execution.settle_expired_position(long_call, closing_price=Decimal("210"))

        self.assertEqual(len(trades), 1)
        self.assertEqual(trades[0].fill_price, Decimal("0"))
        self.assertFalse(Position.objects.filter(id=long_call.id).exists())
        self.competitor.refresh_from_db()
        self.assertEqual(self.competitor.cash_balance, cash_before)

    def test_itm_long_put_cash_settles_to_intrinsic_value_not_exercised(self):
        """The important case: a naive "exercise it" implementation would
        need to SELL shares at strike -- creating a naked short stock
        position if the competitor doesn't already hold the underlying,
        which this league disallows entirely. Cash-settling sidesteps
        that; confirm no stock position (long OR short) is ever created."""
        self._buy_put(quantity=1, ask=2, bid=1.5, strike=40)  # cash: 5000 - 2*100 = 4800
        long_put = Position.objects.get(competitor=self.competitor, instrument_type=Position.INSTRUMENT_PUT)

        trades = execution.settle_expired_position(long_put, closing_price=Decimal("32"))

        self.assertFalse(Position.objects.filter(id=long_put.id).exists())
        self.assertFalse(Position.objects.filter(competitor=self.competitor, instrument_type=Position.INSTRUMENT_STOCK).exists())
        self.competitor.refresh_from_db()
        # 4800 + intrinsic(40-32=8) * 1 * 100 = 5600
        self.assertEqual(self.competitor.cash_balance, Decimal("5600"))
        self.assertEqual(trades[0].fill_price, Decimal("8"))

    def test_otm_long_put_expires_worthless(self):
        self._buy_put(quantity=1, ask=2, bid=1.5, strike=40)
        long_put = Position.objects.get(competitor=self.competitor, instrument_type=Position.INSTRUMENT_PUT)
        cash_before = self.competitor.cash_balance

        trades = execution.settle_expired_position(long_put, closing_price=Decimal("45"))

        self.assertEqual(trades[0].fill_price, Decimal("0"))
        self.competitor.refresh_from_db()
        self.assertEqual(self.competitor.cash_balance, cash_before)

    def test_long_call_exactly_at_strike_expires_worthless(self):
        # Same convention as the short side: exactly-at-strike is OTM.
        self._buy_call(quantity=1, ask=14, bid=13.5, strike=220)
        long_call = Position.objects.get(competitor=self.competitor, instrument_type=Position.INSTRUMENT_CALL)

        trades = execution.settle_expired_position(long_call, closing_price=Decimal("220"))

        self.assertEqual(trades[0].fill_price, Decimal("0"))


class SettleExpiredOptionsTaskTests(TestCase):
    """league.tasks.settle_expired_options -- the Beat wrapper: batches one
    closing-price fetch per distinct ticker, then hands each expired short
    position to execution.settle_expired_position. Settlement correctness
    itself is SettleExpiredOptionsTests' job; this covers the batching and
    the self-healing "no price yet, leave it for next run" behavior."""

    def setUp(self):
        self.competition = Competition.objects.create(
            name="Test League", starting_capital=Decimal("5000"),
            max_position_pct=Decimal("0.60"), max_index_pct=Decimal("0.30"),
            start_date=date(2026, 8, 12), end_date=date(2026, 11, 4),
        )
        self.competitor = Competitor.objects.create(
            competition=self.competition, name="Tester", kind=Competitor.KIND_AI,
            cash_balance=self.competition.starting_capital,
        )

    # Already in the past relative to any realistic test-run date (unlike
    # module-level EXPIRATION, which other tests rely on being an open,
    # still-tradeable contract) -- settle_expired_options' own
    # expiration__lte=today filter needs a real past date to pick these up.
    PAST_EXPIRATION = date(2020, 1, 17)

    def _open_covered_call(self, ticker="TEST", strike=150, quantity=1, premium=Decimal("4.5")):
        """Constructs the short call Position directly rather than through
        submit_trade -- representing a position that was legitimately
        opened BEFORE its expiration passed. submit_trade itself now
        refuses to open (or close) anything already expired (see
        execution.py's option_expired check), so it can no longer be used
        to set up this fixture's own PAST_EXPIRATION contracts."""
        with patch("companies.services.steady.get_quote", return_value=_quote(ask=10, bid=9.9)):
            submit_trade(self.competitor, ticker, Trade.SIDE_BUY, Position.INSTRUMENT_STOCK, 100, "thesis")
        stock = Position.objects.get(competitor=self.competitor, ticker=ticker, instrument_type=Position.INSTRUMENT_STOCK)
        short_call = Position.objects.create(
            competitor=self.competitor, instrument_type=Position.INSTRUMENT_CALL, ticker=ticker,
            strike=Decimal(str(strike)), expiration=self.PAST_EXPIRATION, quantity=quantity,
            is_short=True, avg_cost=premium, covers=stock,
        )
        self.competitor.cash_balance += premium * quantity * 100
        self.competitor.save(update_fields=["cash_balance"])
        return short_call

    def test_fetches_one_quote_per_distinct_ticker(self):
        self._open_covered_call(ticker="AAA", strike=150)
        self._open_covered_call(ticker="AAA", strike=160, quantity=1)  # same ticker, different position
        self._open_covered_call(ticker="BBB", strike=150)

        with patch("companies.services.pricing.get_quote", return_value=_quote(ask=170, bid=169, last=170)) as mock_quote:
            settle_expired_options()

        called_tickers = sorted(call.args[0] for call in mock_quote.call_args_list)
        self.assertEqual(called_tickers, ["AAA", "BBB"])
        self.assertFalse(Position.objects.filter(competitor=self.competitor, instrument_type=Position.INSTRUMENT_CALL).exists())

    def test_no_quote_leaves_position_for_next_run(self):
        short_call = self._open_covered_call(ticker="TEST", strike=150)
        with patch("companies.services.pricing.get_quote", return_value=None):
            settle_expired_options()
        self.assertTrue(Position.objects.filter(id=short_call.id).exists())

    def test_no_open_positions_is_a_noop(self):
        settle_expired_options()  # must not raise with nothing to settle

    def test_also_settles_an_expired_long_option_not_just_short(self):
        """_expiring_option_positions used to filter is_short=True only --
        a long call/put past expiration was invisible to this task
        entirely and stayed open forever. Constructed directly (not via
        submit_trade) for the same PAST_EXPIRATION reason as
        _open_covered_call."""
        long_call = Position.objects.create(
            competitor=self.competitor, instrument_type=Position.INSTRUMENT_CALL, ticker="TEST",
            strike=Decimal("150"), expiration=self.PAST_EXPIRATION, quantity=1, is_short=False,
            avg_cost=Decimal("5"),
        )
        with patch("companies.services.pricing.get_quote", return_value=_quote(ask=170, bid=169, last=170)):
            settle_expired_options()
        self.assertFalse(Position.objects.filter(id=long_call.id).exists())
        self.competitor.refresh_from_db()
        # ITM at 170 vs strike 150: cash-settled intrinsic 20 * 1 * 100 = 2000, plus starting 5000.
        self.assertEqual(self.competitor.cash_balance, Decimal("7000"))


class QuoteRefreshTests(TestCase):
    """valuation.refresh_quote/refresh_chain/refresh_quotes_for_positions --
    the write side that keeps TickerSnapshot warm (league.tasks.
    refresh_open_position_quotes on a Beat tick, or a manual refresh),
    and league.tasks.refresh_open_position_quotes' market-hours gating."""

    def setUp(self):
        self.competition = Competition.objects.create(
            name="Refresh Test League", starting_capital=Decimal("5000"),
            start_date=date(2026, 8, 12), end_date=date(2026, 11, 4),
        )
        self.competitor = Competitor.objects.create(
            competition=self.competition, name="Refresher", kind=Competitor.KIND_AI,
            cash_balance=Decimal("5000"),
        )

    def test_refresh_quote_persists_a_successful_live_fetch(self):
        with patch("companies.services.steady.get_quote", return_value=_quote(ask=51, bid=49, last=50)):
            quote = valuation.refresh_quote("NEW")
        self.assertEqual(quote["last_price"], 50)
        snapshot = TickerSnapshot.objects.get(ticker="NEW", data_type="quote")
        self.assertEqual(snapshot.raw_json["last_price"], 50)

    def test_refresh_quote_leaves_no_snapshot_on_failure(self):
        with patch("companies.services.steady.get_quote", return_value=None):
            quote = valuation.refresh_quote("MISSING")
        self.assertIsNone(quote)
        self.assertFalse(TickerSnapshot.objects.filter(ticker="MISSING").exists())

    def test_refresh_chain_persists_under_a_per_expiration_key(self):
        with patch("companies.services.steady.get_options_chain", return_value=_chain(calls=[_contract(150, 5, 4.5)])):
            valuation.refresh_chain("TEST", "2026-12-19")
        snapshot = TickerSnapshot.objects.get(ticker="TEST", data_type="chain:2026-12-19")
        self.assertEqual(snapshot.raw_json["calls"][0]["strike_price"], 150)

    def test_refresh_quotes_for_positions_covers_every_distinct_ticker_and_contract(self):
        """Two stock positions sharing a ticker -> one quote call for that
        ticker; a covered call adds one options-chain call for its own
        (ticker, expiration) -- not one call per Position."""
        Position.objects.create(
            competitor=self.competitor, instrument_type=Position.INSTRUMENT_STOCK,
            ticker="AAA", quantity=10, avg_cost=Decimal("100"),
        )
        Position.objects.create(
            competitor=self.competitor, instrument_type=Position.INSTRUMENT_STOCK,
            ticker="AAA", quantity=5, avg_cost=Decimal("100"),
        )
        Position.objects.create(
            competitor=self.competitor, instrument_type=Position.INSTRUMENT_CALL,
            ticker="AAA", strike=Decimal("120"), expiration=EXPIRATION,
            quantity=1, is_short=True, avg_cost=Decimal("2"),
        )
        with patch("companies.services.steady.get_quote", return_value=_quote(ask=110, bid=108)) as mock_quote, \
             patch("companies.services.steady.get_options_chain", return_value=_chain(calls=[_contract(120, 3, 2.5)])) as mock_chain:
            valuation.refresh_quotes_for_positions(Position.objects.filter(competitor=self.competitor))
        self.assertEqual(mock_quote.call_count, 1)
        self.assertEqual(mock_chain.call_count, 1)
        self.assertTrue(TickerSnapshot.objects.filter(ticker="AAA", data_type="quote").exists())
        self.assertTrue(TickerSnapshot.objects.filter(ticker="AAA", data_type=f"chain:{EXPIRATION.isoformat()}").exists())

    def test_refresh_quotes_for_positions_one_bad_ticker_does_not_block_the_rest(self):
        Position.objects.create(
            competitor=self.competitor, instrument_type=Position.INSTRUMENT_STOCK,
            ticker="BAD", quantity=1, avg_cost=Decimal("10"),
        )
        Position.objects.create(
            competitor=self.competitor, instrument_type=Position.INSTRUMENT_STOCK,
            ticker="GOOD", quantity=1, avg_cost=Decimal("10"),
        )

        def _side_effect(ticker, asset_class="STOCKS"):
            if ticker == "BAD":
                raise RuntimeError("Steady is down")
            return _quote(ask=21, bid=19, last=20)

        with patch("companies.services.steady.get_quote", side_effect=_side_effect):
            valuation.refresh_quotes_for_positions(Position.objects.filter(competitor=self.competitor))
        self.assertFalse(TickerSnapshot.objects.filter(ticker="BAD").exists())
        self.assertTrue(TickerSnapshot.objects.filter(ticker="GOOD").exists())

    def _et(self, hour, minute, weekday_date=date(2026, 8, 13)):
        """2026-08-13 is a Thursday -- a plain weekday to anchor the
        in-hours cases; weekend cases pass an explicit Saturday/Sunday
        date instead."""
        return datetime.combine(weekday_date, time(hour, minute), tzinfo=EASTERN)

    def test_beat_task_refreshes_during_market_hours_on_a_weekday(self):
        Position.objects.create(
            competitor=self.competitor, instrument_type=Position.INSTRUMENT_STOCK,
            ticker="INHOURS", quantity=1, avg_cost=Decimal("10"),
        )
        with patch("league.tasks.timezone.now", return_value=self._et(14, 0)), \
             patch("league.tasks.valuation.refresh_quotes_for_positions") as mock_refresh:
            refresh_open_position_quotes()
        mock_refresh.assert_called_once()
        refreshed_tickers = {p.ticker for p in mock_refresh.call_args[0][0]}
        self.assertIn("INHOURS", refreshed_tickers)

    def test_beat_task_noops_before_market_open(self):
        with patch("league.tasks.timezone.now", return_value=self._et(8, 0)), \
             patch("league.tasks.valuation.refresh_quotes_for_positions") as mock_refresh:
            refresh_open_position_quotes()
        mock_refresh.assert_not_called()

    def test_beat_task_noops_after_market_close(self):
        with patch("league.tasks.timezone.now", return_value=self._et(17, 0)), \
             patch("league.tasks.valuation.refresh_quotes_for_positions") as mock_refresh:
            refresh_open_position_quotes()
        mock_refresh.assert_not_called()

    def test_beat_task_noops_on_a_weekend(self):
        saturday = date(2026, 8, 15)
        with patch("league.tasks.timezone.now", return_value=self._et(14, 0, weekday_date=saturday)), \
             patch("league.tasks.valuation.refresh_quotes_for_positions") as mock_refresh:
            refresh_open_position_quotes()
        mock_refresh.assert_not_called()

    # --- manual-refresh tasks: refresh + broadcast over the league group ---

    def test_refresh_competitor_quotes_task_broadcasts_the_updated_row(self):
        Position.objects.create(
            competitor=self.competitor, instrument_type=Position.INSTRUMENT_STOCK,
            ticker="BCAST", quantity=1, avg_cost=Decimal("10"),
        )
        with patch("companies.services.steady.get_quote", return_value=_quote(ask=21, bid=19, last=20)), \
             patch("realtime.utils.broadcast_to_group") as mock_broadcast:
            refresh_competitor_quotes(self.competitor.id)

        self.assertTrue(TickerSnapshot.objects.filter(ticker="BCAST", data_type="quote").exists())
        mock_broadcast.assert_called_once()
        group, message_type = mock_broadcast.call_args[0]
        self.assertEqual(group, f"league_{self.competition.id}")
        self.assertEqual(message_type, "league.competitor_refreshed")
        self.assertEqual(mock_broadcast.call_args[1]["competitor"]["id"], self.competitor.id)

    def test_refresh_competitor_quotes_task_skips_a_deleted_competitor(self):
        with patch("realtime.utils.broadcast_to_group") as mock_broadcast:
            refresh_competitor_quotes(999999)
        mock_broadcast.assert_not_called()

    def test_refresh_competition_quotes_task_broadcasts_the_scoreboard(self):
        with patch("realtime.utils.broadcast_to_group") as mock_broadcast:
            refresh_competition_quotes(self.competition.id)
        mock_broadcast.assert_called_once()
        group, message_type = mock_broadcast.call_args[0]
        self.assertEqual(group, f"league_{self.competition.id}")
        self.assertEqual(message_type, "league.scoreboard_refreshed")
        self.assertEqual(mock_broadcast.call_args[1]["competition"], self.competition.name)

    def test_refresh_competition_quotes_task_skips_a_deleted_competition(self):
        with patch("realtime.utils.broadcast_to_group") as mock_broadcast:
            refresh_competition_quotes(999999)
        mock_broadcast.assert_not_called()


class PauseTests(TestCase):
    """Competitor.is_paused -- an operator kill switch for an AI
    competitor's own LLM-driven wake-up loop (the actual API cost),
    checked at every trigger point so pausing is airtight."""

    def setUp(self):
        self.competition = Competition.objects.create(
            name="Pause Test League", starting_capital=Decimal("5000"),
            start_date=date(2026, 8, 12), end_date=date(2026, 11, 4),
        )

    def test_run_agent_wakeup_cancels_without_running_for_a_paused_competitor(self):
        competitor = Competitor.objects.create(
            competition=self.competition, name="Paused", kind=Competitor.KIND_AI,
            assistant_model=AssistantModel.get_default(), cash_balance=Decimal("5000"), is_paused=True,
        )
        wakeup = AgentWakeup.objects.create(competitor=competitor, run_at=timezone.now(), reason="test")
        with patch("league.tasks._run_wakeup") as mock_run_wakeup:
            run_agent_wakeup(wakeup.id)
        mock_run_wakeup.assert_not_called()
        wakeup.refresh_from_db()
        self.assertEqual(wakeup.status, AgentWakeup.STATUS_CANCELLED)
        self.assertIsNotNone(wakeup.completed_at)

    def test_run_agent_wakeup_runs_normally_for_an_unpaused_competitor(self):
        competitor = Competitor.objects.create(
            competition=self.competition, name="Active", kind=Competitor.KIND_AI,
            assistant_model=AssistantModel.get_default(), cash_balance=Decimal("5000"),
        )
        wakeup = AgentWakeup.objects.create(competitor=competitor, run_at=timezone.now(), reason="test")
        # A COMPLETED turn runs _ensure_future_wakeup_scheduled, which (with
        # nothing self-scheduled, the normal case for a mocked _run_wakeup)
        # calls run_agent_wakeup.apply_async for real -- mocked here too,
        # same as every other test exercising this path, purely so the
        # test doesn't attempt a real broker connection as a side effect
        # of logic this test isn't actually checking.
        with patch("league.tasks._run_wakeup") as mock_run_wakeup, \
             patch("league.tasks.run_agent_wakeup.apply_async"):
            run_agent_wakeup(wakeup.id)
        mock_run_wakeup.assert_called_once()
        wakeup.refresh_from_db()
        self.assertEqual(wakeup.status, AgentWakeup.STATUS_COMPLETED)

    def test_run_agent_wakeup_pushes_wakeup_started_before_running(self):
        """league.wakeup_started (docs/websockets.md) is the "now
        thinking" signal a frontend wake-up button waits on instead of
        just the bare 202 from CompetitorWakeupsView.post -- must fire
        once the pause/budget/status guards have all passed but before
        the (potentially slow) LLM call itself, so patching _run_wakeup to
        assert call ordering via a shared list is how this pins that
        without a real LLM call."""
        user = User.objects.create_user(email="wakeup-started@example.com", password="testpass123")
        competitor = Competitor.objects.create(
            competition=self.competition, name="Watched", kind=Competitor.KIND_AI, user=user,
            assistant_model=AssistantModel.get_default(), cash_balance=Decimal("5000"),
        )
        wakeup = AgentWakeup.objects.create(competitor=competitor, run_at=timezone.now(), reason="test")

        calls = []
        with patch("league.tasks._run_wakeup", side_effect=lambda *a, **k: calls.append("ran")) as mock_run_wakeup, \
             patch("league.tasks.run_agent_wakeup.apply_async"), \
             patch("league.tasks.send_to_user", side_effect=lambda *a, **k: calls.append("started")) as mock_send:
            run_agent_wakeup(wakeup.id)

        mock_run_wakeup.assert_called_once()
        mock_send.assert_called_once_with(
            user.id, "league.wakeup_started", competitor_id=competitor.id, wakeup_id=wakeup.id,
        )
        self.assertEqual(calls, ["started", "ran"])

    def test_run_agent_wakeup_skips_wakeup_started_push_for_a_userless_competitor(self):
        """An AI competitor with no owning user (Competitor.user is
        nullable -- see that field's docstring) has nowhere to push
        league.wakeup_started to, same reasoning as the terminal
        notify() calls' own `if not competitor.user_id: return` guard."""
        competitor = Competitor.objects.create(
            competition=self.competition, name="Ownerless", kind=Competitor.KIND_AI,
            assistant_model=AssistantModel.get_default(), cash_balance=Decimal("5000"),
        )
        wakeup = AgentWakeup.objects.create(competitor=competitor, run_at=timezone.now(), reason="test")
        with patch("league.tasks._run_wakeup"), \
             patch("league.tasks.run_agent_wakeup.apply_async"), \
             patch("league.tasks.send_to_user") as mock_send:
            run_agent_wakeup(wakeup.id)
        mock_send.assert_not_called()

    def test_dispatch_forced_checkins_skips_paused_competitors(self):
        ForcedCheckin.objects.create(
            competition=self.competition, hour=8, minute=0, label="8am news scan",
            prompt="Read overnight news.", inject_news=True,
        )
        paused = Competitor.objects.create(
            competition=self.competition, name="Paused", kind=Competitor.KIND_AI,
            assistant_model=AssistantModel.get_default(), cash_balance=Decimal("5000"), is_paused=True,
        )
        active = Competitor.objects.create(
            competition=self.competition, name="Active", kind=Competitor.KIND_AI,
            assistant_model=AssistantModel.get_default(), cash_balance=Decimal("5000"),
        )
        # 2026-08-13 is a Thursday inside the competition's date range;
        # 8:00am ET matches the ForcedCheckin row above.
        eight_am_et = datetime.combine(date(2026, 8, 13), time(8, 0), tzinfo=EASTERN)
        with patch("league.tasks.timezone.now", return_value=eight_am_et), \
             patch("league.tasks.run_agent_wakeup.delay") as mock_delay:
            dispatch_forced_checkins()
        mock_delay.assert_called_once()
        self.assertFalse(AgentWakeup.objects.filter(competitor=paused).exists())
        self.assertTrue(AgentWakeup.objects.filter(competitor=active).exists())

    def test_dispatch_forced_checkins_ignores_other_competitions(self):
        """A competition with NO ForcedCheckin rows configured (e.g. one
        created directly, bypassing the auto-seeded default) gets no
        forced wake-ups at all -- this is deliberate, not a gap: the new
        floor is opt-in data on Competition, not a global constant."""
        Competitor.objects.create(
            competition=self.competition, name="NoCheckins", kind=Competitor.KIND_AI,
            assistant_model=AssistantModel.get_default(), cash_balance=Decimal("5000"),
        )
        eight_am_et = datetime.combine(date(2026, 8, 13), time(8, 0), tzinfo=EASTERN)
        with patch("league.tasks.timezone.now", return_value=eight_am_et), \
             patch("league.tasks.run_agent_wakeup.delay") as mock_delay:
            dispatch_forced_checkins()
        mock_delay.assert_not_called()


class BudgetTests(TestCase):
    """Competitor.total_budget_usd (hard cap, enforced in run_agent_wakeup)
    and .weekly_budget_usd (advisory only, never enforced) -- see
    league.services.budget for the aggregation/projection math."""

    def setUp(self):
        self.competition = Competition.objects.create(
            name="Budget Test League", starting_capital=Decimal("5000"),
            start_date=date(2026, 8, 12), end_date=date(2026, 11, 4),
        )

    def _make_turn(self, competitor, cost, created_at=None):
        turn = AgentTurn.objects.create(
            competitor=competitor, trigger=AgentTurn.TRIGGER_SCHEDULED, cost_usd=cost,
        )
        if created_at is not None:
            # created_at is auto_now_add -- only a queryset .update() (which
            # skips auto_now_add processing) can back-date it, same trick
            # used elsewhere in this file for created_at-dependent fixtures.
            AgentTurn.objects.filter(id=turn.id).update(created_at=created_at)
            turn.refresh_from_db()
        return turn

    def test_run_agent_wakeup_cancels_and_pauses_once_over_total_budget(self):
        competitor = Competitor.objects.create(
            competition=self.competition, name="OverBudget", kind=Competitor.KIND_AI,
            assistant_model=AssistantModel.get_default(), cash_balance=Decimal("5000"),
            total_budget_usd=Decimal("1.00"),
        )
        self._make_turn(competitor, Decimal("1.50"))
        wakeup = AgentWakeup.objects.create(competitor=competitor, run_at=timezone.now(), reason="test")
        with patch("league.tasks._run_wakeup") as mock_run_wakeup:
            run_agent_wakeup(wakeup.id)
        mock_run_wakeup.assert_not_called()
        wakeup.refresh_from_db()
        competitor.refresh_from_db()
        self.assertEqual(wakeup.status, AgentWakeup.STATUS_CANCELLED)
        self.assertTrue(competitor.is_paused)

    def test_run_agent_wakeup_runs_normally_under_total_budget(self):
        competitor = Competitor.objects.create(
            competition=self.competition, name="UnderBudget", kind=Competitor.KIND_AI,
            assistant_model=AssistantModel.get_default(), cash_balance=Decimal("5000"),
            total_budget_usd=Decimal("100.00"),
        )
        self._make_turn(competitor, Decimal("1.50"))
        wakeup = AgentWakeup.objects.create(competitor=competitor, run_at=timezone.now(), reason="test")
        with patch("league.tasks._run_wakeup") as mock_run_wakeup, \
             patch("league.tasks.run_agent_wakeup.apply_async"):
            run_agent_wakeup(wakeup.id)
        mock_run_wakeup.assert_called_once()
        wakeup.refresh_from_db()
        competitor.refresh_from_db()
        self.assertEqual(wakeup.status, AgentWakeup.STATUS_COMPLETED)
        self.assertFalse(competitor.is_paused)

    def test_run_agent_wakeup_ignores_unset_total_budget(self):
        """No total_budget_usd at all -- must run normally, not be treated
        as an implicit $0 cap."""
        competitor = Competitor.objects.create(
            competition=self.competition, name="NoCap", kind=Competitor.KIND_AI,
            assistant_model=AssistantModel.get_default(), cash_balance=Decimal("5000"),
        )
        self._make_turn(competitor, Decimal("50.00"))
        wakeup = AgentWakeup.objects.create(competitor=competitor, run_at=timezone.now(), reason="test")
        with patch("league.tasks._run_wakeup") as mock_run_wakeup, \
             patch("league.tasks.run_agent_wakeup.apply_async"):
            run_agent_wakeup(wakeup.id)
        mock_run_wakeup.assert_called_once()

    def test_et_week_bounds_anchors_to_monday(self):
        thursday = datetime.combine(date(2026, 8, 13), time(15, 0), tzinfo=EASTERN)  # 2026-08-13 is a Thursday
        start, end = wakeup_spacing.et_week_bounds(thursday)
        self.assertEqual(start, datetime.combine(date(2026, 8, 10), time.min, tzinfo=EASTERN))
        self.assertEqual(end, datetime.combine(date(2026, 8, 17), time.min, tzinfo=EASTERN))

        monday_just_after_midnight = datetime.combine(date(2026, 8, 10), time(0, 30), tzinfo=EASTERN)
        start2, _ = wakeup_spacing.et_week_bounds(monday_just_after_midnight)
        self.assertEqual(start2, datetime.combine(date(2026, 8, 10), time.min, tzinfo=EASTERN))

    def test_snapshot_projections_none_without_budgets_or_turns(self):
        competitor = Competitor.objects.create(
            competition=self.competition, name="NoBudget", kind=Competitor.KIND_AI,
            assistant_model=AssistantModel.get_default(), cash_balance=Decimal("5000"),
        )
        snap = budget.snapshot(competitor)
        self.assertIsNone(snap.total_budget)
        self.assertIsNone(snap.total_remaining)
        self.assertIsNone(snap.weekly_budget)
        self.assertIsNone(snap.projected_week_total)
        self.assertIsNone(snap.projected_runway_days)
        self.assertIsNone(snap.avg_cost_per_turn)
        self.assertIsNone(snap.remaining_calls_estimate)
        self.assertEqual(snap.total_spent, Decimal("0"))

    def test_snapshot_computes_spend_and_projections_with_budgets_set(self):
        competitor = Competitor.objects.create(
            competition=self.competition, name="Tracked", kind=Competitor.KIND_AI,
            assistant_model=AssistantModel.get_default(), cash_balance=Decimal("5000"),
            total_budget_usd=Decimal("10.00"), weekly_budget_usd=Decimal("5.00"),
        )
        now = datetime.combine(date(2026, 8, 18), time(12, 0), tzinfo=EASTERN)  # Tuesday
        this_monday = datetime.combine(date(2026, 8, 17), time(9, 0), tzinfo=EASTERN)
        last_week = datetime.combine(date(2026, 8, 10), time(9, 0), tzinfo=EASTERN)
        self._make_turn(competitor, Decimal("1.00"), created_at=last_week)
        self._make_turn(competitor, Decimal("2.00"), created_at=this_monday)

        snap = budget.snapshot(competitor, now=now)

        self.assertEqual(snap.total_spent, Decimal("3.00"))
        self.assertEqual(snap.total_remaining, Decimal("7.00"))
        self.assertEqual(snap.week_spent, Decimal("2.00"))  # last_week's turn excluded
        self.assertEqual(snap.avg_cost_per_turn, Decimal("1.50"))
        self.assertIsNotNone(snap.remaining_calls_estimate)
        self.assertIsNotNone(snap.projected_week_total)
        self.assertIsNotNone(snap.projected_runway_days)


class SafetyNetWakeupTests(TestCase):
    """_ensure_future_wakeup_scheduled -- the backstop for a turn that
    exhausted LEAGUE_MAX_TOOL_ITERATIONS before the agent ever reached its
    own schedule_wakeup call (league/tasks.py's SAFETY_NET_WAKEUP_DELAY
    docstring has the full incident this exists for)."""

    def setUp(self):
        self.competition = Competition.objects.create(
            name="Safety Net Test League", starting_capital=Decimal("5000"),
            start_date=date(2026, 8, 12), end_date=date(2026, 11, 4),
        )
        self.competitor = Competitor.objects.create(
            competition=self.competition, name="AI", kind=Competitor.KIND_AI,
            assistant_model=AssistantModel.get_default(), cash_balance=Decimal("5000"),
        )

    def test_completed_turn_with_nothing_scheduled_gets_a_safety_net_wakeup(self):
        wakeup = AgentWakeup.objects.create(competitor=self.competitor, run_at=timezone.now(), reason="test")
        with patch("league.tasks._run_wakeup"), patch("league.tasks.run_agent_wakeup.apply_async") as mock_apply_async:
            run_agent_wakeup(wakeup.id)
        # The original wakeup plus exactly one new safety-net row.
        pending = AgentWakeup.objects.filter(competitor=self.competitor, status=AgentWakeup.STATUS_SCHEDULED)
        self.assertEqual(pending.count(), 1)
        safety_net = pending.first()
        self.assertIn("safety-net", safety_net.reason.lower())
        mock_apply_async.assert_called_once_with(args=[safety_net.id], eta=safety_net.run_at)

    def test_completed_turn_with_something_already_scheduled_gets_no_safety_net(self):
        wakeup = AgentWakeup.objects.create(competitor=self.competitor, run_at=timezone.now(), reason="test")
        AgentWakeup.objects.create(
            competitor=self.competitor, run_at=timezone.now() + timedelta(hours=2),
            reason="agent's own schedule_wakeup call", status=AgentWakeup.STATUS_SCHEDULED,
        )
        with patch("league.tasks._run_wakeup"), patch("league.tasks.run_agent_wakeup.apply_async") as mock_apply_async:
            run_agent_wakeup(wakeup.id)
        mock_apply_async.assert_not_called()
        self.assertEqual(
            AgentWakeup.objects.filter(competitor=self.competitor, status=AgentWakeup.STATUS_SCHEDULED).count(), 1,
        )

    def test_transient_failed_turn_gets_a_safety_net(self):
        """A plain/network-shaped error (not a recognized terminal
        provider error, see provider_errors.is_terminal) is retryable --
        it gets the same safety net a budget-exhausted COMPLETED turn
        does, just like league.tasks._ensure_future_wakeup_scheduled's
        docstring says."""
        wakeup = AgentWakeup.objects.create(competitor=self.competitor, run_at=timezone.now(), reason="test")
        with patch("league.tasks._run_wakeup", side_effect=RuntimeError("boom")), \
             patch("league.tasks.run_agent_wakeup.apply_async") as mock_apply_async:
            run_agent_wakeup(wakeup.id)
        wakeup.refresh_from_db()
        self.assertEqual(wakeup.status, AgentWakeup.STATUS_FAILED)
        self.assertIn("RuntimeError", wakeup.error)
        mock_apply_async.assert_called_once()
        self.competitor.refresh_from_db()
        self.assertFalse(self.competitor.is_paused)

    def test_terminal_failed_turn_pauses_competitor_and_gets_no_safety_net(self):
        """A terminal provider error (dead API key, exhausted account --
        provider_errors.is_terminal) pauses the competitor instead of
        retrying, since retrying the exact same call would just fail the
        exact same way -- the 2026-08-25/26 incident this exists for."""
        wakeup = AgentWakeup.objects.create(competitor=self.competitor, run_at=timezone.now(), reason="test")
        with patch("league.tasks._run_wakeup", side_effect=_anthropic_auth_error()), \
             patch("league.tasks.run_agent_wakeup.apply_async") as mock_apply_async:
            run_agent_wakeup(wakeup.id)
        wakeup.refresh_from_db()
        self.assertEqual(wakeup.status, AgentWakeup.STATUS_FAILED)
        self.assertIn("Anthropic 401", wakeup.error)
        mock_apply_async.assert_not_called()
        self.competitor.refresh_from_db()
        self.assertTrue(self.competitor.is_paused)

    def test_repeated_non_terminal_failures_pause_the_competitor(self):
        """CONSECUTIVE_FAILURE_PAUSE_THRESHOLD straight non-terminal
        failures (a misconfigured API key, a Gemini quota exhaustion --
        neither recognized as TERMINAL by provider_errors) must stop
        retrying instead of hammering SAFETY_NET_WAKEUP_DELAY forever --
        the exact unbounded-loop gap code review caught before this
        shipped."""
        from .tasks import CONSECUTIVE_FAILURE_PAUSE_THRESHOLD
        now = timezone.now()
        for i in range(CONSECUTIVE_FAILURE_PAUSE_THRESHOLD - 1):
            AgentWakeup.objects.create(
                competitor=self.competitor, run_at=now - timedelta(minutes=i + 1),
                reason="an earlier attempt", status=AgentWakeup.STATUS_FAILED,
                completed_at=now - timedelta(minutes=i + 1),
            )
        wakeup = AgentWakeup.objects.create(competitor=self.competitor, run_at=now, reason="test")
        with patch("league.tasks._run_wakeup", side_effect=RuntimeError("still broken")), \
             patch("league.tasks.run_agent_wakeup.apply_async") as mock_apply_async:
            run_agent_wakeup(wakeup.id)
        mock_apply_async.assert_not_called()
        self.competitor.refresh_from_db()
        self.assertTrue(self.competitor.is_paused)

    def test_a_completed_turn_resets_the_failure_streak(self):
        from .tasks import CONSECUTIVE_FAILURE_PAUSE_THRESHOLD
        now = timezone.now()
        AgentWakeup.objects.create(
            competitor=self.competitor, run_at=now - timedelta(minutes=10),
            reason="an old failure", status=AgentWakeup.STATUS_FAILED, completed_at=now - timedelta(minutes=10),
        )
        AgentWakeup.objects.create(
            competitor=self.competitor, run_at=now - timedelta(minutes=5),
            reason="it recovered", status=AgentWakeup.STATUS_COMPLETED, completed_at=now - timedelta(minutes=5),
        )
        wakeup = AgentWakeup.objects.create(competitor=self.competitor, run_at=now, reason="test")
        with patch("league.tasks._run_wakeup", side_effect=RuntimeError("broken again")), \
             patch("league.tasks.run_agent_wakeup.apply_async") as mock_apply_async:
            run_agent_wakeup(wakeup.id)
        # Only 1 failure since the reset (threshold is
        # CONSECUTIVE_FAILURE_PAUSE_THRESHOLD) -- still treated as transient.
        self.assertGreaterEqual(CONSECUTIVE_FAILURE_PAUSE_THRESHOLD, 2)
        mock_apply_async.assert_called_once()
        self.competitor.refresh_from_db()
        self.assertFalse(self.competitor.is_paused)


class WakeupCapCountingTests(TestCase):
    """LEAGUE_MAX_WAKEUPS_PER_DAY is meant to budget only the agent's OWN
    schedule_wakeup calls (league.services.wakeup_spacing.
    wakeups_today_count's docstring has the 2026-08-25 incident this
    replaced: counting ForcedCheckin/safety-net rows too let 17
    consecutive safety-net wake-ups burn through the cap in one
    evening)."""

    def setUp(self):
        self.competition = Competition.objects.create(
            name="Cap Test League", starting_capital=Decimal("5000"),
            start_date=date(2026, 8, 12), end_date=date(2026, 11, 4),
        )
        self.competitor = Competitor.objects.create(
            competition=self.competition, name="AI", kind=Competitor.KIND_AI,
            assistant_model=AssistantModel.get_default(), cash_balance=Decimal("5000"),
        )

    def test_forced_and_safety_net_rows_are_excluded_from_the_count(self):
        from .services.wakeup_spacing import wakeups_today_count
        now = timezone.now()
        AgentWakeup.objects.create(
            competitor=self.competitor, run_at=now, reason="[Forced: 8am news scan] Read the news.",
        )
        AgentWakeup.objects.create(
            competitor=self.competitor, run_at=now,
            reason="Automatic safety-net check-in: your last turn ended without any future wake-up scheduled.",
        )
        AgentWakeup.objects.create(competitor=self.competitor, run_at=now, reason="the agent's own pick")
        self.assertEqual(wakeups_today_count(self.competitor, now), 1)

    def test_schedule_wakeup_rejects_a_past_run_at(self):
        """Without this, a miscomputed time (wrong day, wrong offset)
        gets apply_async(eta=<already past>) and fires almost
        immediately -- a burst of turns right now, not the check-in the
        agent actually meant to schedule."""
        from .tools import build_league_tools
        schedule_wakeup = {t.name: t for t in build_league_tools(self.competitor)}["schedule_wakeup"]
        past = (timezone.now() - timedelta(hours=1)).isoformat()
        with patch("league.tasks.run_agent_wakeup.apply_async") as mock_apply_async:
            result = schedule_wakeup.call({"run_at": past, "reason": "oops, wrong day"})
        self.assertIn("already in the past", result)
        mock_apply_async.assert_not_called()
        self.assertFalse(AgentWakeup.objects.filter(competitor=self.competitor).exists())

    def test_schedule_wakeup_tool_is_not_blocked_by_exempt_rows(self):
        from .tools import LEAGUE_MAX_WAKEUPS_PER_DAY, build_league_tools
        now = timezone.now()
        for _ in range(LEAGUE_MAX_WAKEUPS_PER_DAY):
            AgentWakeup.objects.create(
                competitor=self.competitor, run_at=now, reason="[Forced: 8am news scan] Read the news.",
            )
        schedule_wakeup = {t.name: t for t in build_league_tools(self.competitor)}["schedule_wakeup"]
        with patch("league.tasks.run_agent_wakeup.apply_async"):
            result = schedule_wakeup.call({
                "run_at": (now + timedelta(hours=1)).isoformat(), "reason": "own scheduling, cap should still allow this",
            })
        self.assertIn("Scheduled", result)

    def test_schedule_wakeup_tool_is_blocked_once_own_rows_hit_the_cap(self):
        from .tools import LEAGUE_MAX_WAKEUPS_PER_DAY, build_league_tools
        now = timezone.now()
        for _ in range(LEAGUE_MAX_WAKEUPS_PER_DAY):
            AgentWakeup.objects.create(competitor=self.competitor, run_at=now, reason="an earlier own pick")
        schedule_wakeup = {t.name: t for t in build_league_tools(self.competitor)}["schedule_wakeup"]
        with patch("league.tasks.run_agent_wakeup.apply_async") as mock_apply_async:
            result = schedule_wakeup.call({"run_at": (now + timedelta(hours=1)).isoformat(), "reason": "one too many"})
        self.assertIn("Not scheduled", result)
        mock_apply_async.assert_not_called()

    def test_safety_net_after_cap_exhaustion_is_timed_for_the_reset_not_15_minutes(self):
        """The closed loop from 2026-08-25: cap-refused -> nothing
        scheduled -> safety net fires 15 min later -> refused again. Once
        the competitor has already used its full OWN-row budget today,
        the safety net must skip straight to just after the ET-day
        reset."""
        from .tools import LEAGUE_MAX_WAKEUPS_PER_DAY
        now = timezone.now()
        for _ in range(LEAGUE_MAX_WAKEUPS_PER_DAY):
            AgentWakeup.objects.create(competitor=self.competitor, run_at=now, reason="an earlier own pick")
        wakeup = AgentWakeup.objects.create(competitor=self.competitor, run_at=now, reason="test")
        with patch("league.tasks._run_wakeup"), \
             patch("league.tasks.run_agent_wakeup.apply_async") as mock_apply_async:
            run_agent_wakeup(wakeup.id)
        mock_apply_async.assert_called_once()
        _args, kwargs = mock_apply_async.call_args
        safety_net_id = kwargs["args"][0]
        safety_net = AgentWakeup.objects.get(id=safety_net_id)
        self.assertIn("cap resets", safety_net.reason)
        # Timed for after today's ET midnight, not the usual ~15 minutes
        # out -- compared by ET calendar date (not a fixed hour threshold
        # against real timezone.now()) so this doesn't flake when the
        # suite happens to run late in the ET evening.
        self.assertGreater(safety_net.run_at.astimezone(EASTERN).date(), now.astimezone(EASTERN).date())


class ForcedCheckinCatchUpTests(TestCase):
    """dispatch_forced_checkins used to match a ForcedCheckin's (hour,
    minute) for EXACT equality against the instant the task happened to
    run -- a row whose minute wasn't a multiple of 5 (Beat's own tick
    interval) could never fire at all, and a tick running even ~60s late
    silently skipped the day's slot with no catch-up. It now fires any
    not-yet-fired-today slot whose time has passed (see that function's
    docstring)."""

    def setUp(self):
        self.competition = Competition.objects.create(
            name="Catch-Up League", starting_capital=Decimal("5000"),
            start_date=date(2026, 8, 12), end_date=date(2026, 11, 4),
        )
        self.competitor = Competitor.objects.create(
            competition=self.competition, name="AI", kind=Competitor.KIND_AI,
            assistant_model=AssistantModel.get_default(), cash_balance=Decimal("5000"),
        )

    def test_minute_not_a_multiple_of_five_still_fires(self):
        """hour=8, minute=7 -- Beat's crontab(minute="*/5") never ticks at
        :07, so the old exact-match version could never fire this row."""
        ForcedCheckin.objects.create(
            competition=self.competition, hour=8, minute=7, label="odd-minute scan", prompt="Read the news.",
        )
        tick_at = datetime.combine(date(2026, 8, 13), time(8, 10), tzinfo=EASTERN)
        with patch("league.tasks.timezone.now", return_value=tick_at), \
             patch("league.tasks.run_agent_wakeup.delay") as mock_delay:
            dispatch_forced_checkins()
        mock_delay.assert_called_once()

    def test_a_late_tick_still_fires_the_slot_once(self):
        """The 8:00 tick never happens (simulating a worker backlog); the
        next tick at 8:05 must still fire it, and firing again at 8:10
        must NOT create a duplicate for the same competitor/day."""
        ForcedCheckin.objects.create(
            competition=self.competition, hour=8, minute=0, label="8am scan", prompt="Read the news.",
        )
        first_tick = datetime.combine(date(2026, 8, 13), time(8, 5), tzinfo=EASTERN)
        with patch("league.tasks.timezone.now", return_value=first_tick), \
             patch("league.tasks.run_agent_wakeup.delay") as mock_delay:
            dispatch_forced_checkins()
        mock_delay.assert_called_once()

        second_tick = datetime.combine(date(2026, 8, 13), time(8, 10), tzinfo=EASTERN)
        with patch("league.tasks.timezone.now", return_value=second_tick), \
             patch("league.tasks.run_agent_wakeup.delay") as mock_delay_again:
            dispatch_forced_checkins()
        mock_delay_again.assert_not_called()

    def test_does_not_fire_a_slot_whose_time_has_not_yet_arrived(self):
        ForcedCheckin.objects.create(
            competition=self.competition, hour=15, minute=15, label="pre-close", prompt="Wrap up.",
        )
        early = datetime.combine(date(2026, 8, 13), time(9, 0), tzinfo=EASTERN)
        with patch("league.tasks.timezone.now", return_value=early), \
             patch("league.tasks.run_agent_wakeup.delay") as mock_delay:
            dispatch_forced_checkins()
        mock_delay.assert_not_called()

    def test_fires_again_the_next_trading_day(self):
        ForcedCheckin.objects.create(
            competition=self.competition, hour=8, minute=0, label="8am scan", prompt="Read the news.",
        )
        day_one = datetime.combine(date(2026, 8, 13), time(8, 0), tzinfo=EASTERN)
        with patch("league.tasks.timezone.now", return_value=day_one), \
             patch("league.tasks.run_agent_wakeup.delay") as mock_delay_one:
            dispatch_forced_checkins()
        mock_delay_one.assert_called_once()

        day_two = datetime.combine(date(2026, 8, 14), time(8, 0), tzinfo=EASTERN)
        with patch("league.tasks.timezone.now", return_value=day_two), \
             patch("league.tasks.run_agent_wakeup.delay") as mock_delay_two:
            dispatch_forced_checkins()
        mock_delay_two.assert_called_once()

    def test_does_not_catch_up_a_slot_missed_beyond_the_catch_up_window(self):
        """Bounded catch-up (added after code review): a slot missed by
        more than CHECKIN_CATCH_UP_WINDOW is not fired hours later mixed
        in with whatever's actually due now -- it waits for tomorrow."""
        from .tasks import CHECKIN_CATCH_UP_WINDOW
        ForcedCheckin.objects.create(
            competition=self.competition, hour=8, minute=0, label="8am scan", prompt="Read the news.",
        )
        too_late = datetime.combine(date(2026, 8, 13), time(8, 0), tzinfo=EASTERN) + CHECKIN_CATCH_UP_WINDOW + timedelta(minutes=1)
        with patch("league.tasks.timezone.now", return_value=too_late), \
             patch("league.tasks.run_agent_wakeup.delay") as mock_delay:
            dispatch_forced_checkins()
        mock_delay.assert_not_called()

    def test_still_catches_up_a_slot_missed_within_the_catch_up_window(self):
        from .tasks import CHECKIN_CATCH_UP_WINDOW
        ForcedCheckin.objects.create(
            competition=self.competition, hour=8, minute=0, label="8am scan", prompt="Read the news.",
        )
        just_in_time = datetime.combine(date(2026, 8, 13), time(8, 0), tzinfo=EASTERN) + CHECKIN_CATCH_UP_WINDOW - timedelta(minutes=1)
        with patch("league.tasks.timezone.now", return_value=just_in_time), \
             patch("league.tasks.run_agent_wakeup.delay") as mock_delay:
            dispatch_forced_checkins()
        mock_delay.assert_called_once()

    def test_a_new_forced_checkin_does_not_immediately_fire_todays_already_past_slot(self):
        """Creating a ForcedCheckin at 2pm for an 8am slot shouldn't fire
        that slot the instant the next tick runs -- it's covered by the
        same catch-up window bound as a slot that was actually due and
        missed, since either way the row didn't fire by the time the
        window closed."""
        ForcedCheckin.objects.create(
            competition=self.competition, hour=8, minute=0, label="8am scan", prompt="Read the news.",
        )
        created_at = datetime.combine(date(2026, 8, 13), time(14, 0), tzinfo=EASTERN)  # 6h past the 8am slot
        with patch("league.tasks.timezone.now", return_value=created_at), \
             patch("league.tasks.run_agent_wakeup.delay") as mock_delay:
            dispatch_forced_checkins()
        mock_delay.assert_not_called()


class OverdueWakeupSweepTests(TestCase):
    """sweep_overdue_wakeups -- recovers an AgentWakeup whose
    apply_async(eta=...) task the Celery/Redis broker silently dropped
    (confirmed happening in production: wakeup #192 during the
    2026-08-25/26 investigation, see that function's docstring)."""

    def setUp(self):
        self.competition = Competition.objects.create(
            name="Sweep Test League", starting_capital=Decimal("5000"),
            start_date=date(2026, 8, 12), end_date=date(2026, 11, 4),
        )
        self.competitor = Competitor.objects.create(
            competition=self.competition, name="AI", kind=Competitor.KIND_AI,
            assistant_model=AssistantModel.get_default(), cash_balance=Decimal("5000"),
        )

    def test_redispatches_a_wakeup_overdue_past_the_grace_window(self):
        from .tasks import OVERDUE_SWEEP_GRACE, sweep_overdue_wakeups
        overdue = AgentWakeup.objects.create(
            competitor=self.competitor, run_at=timezone.now() - OVERDUE_SWEEP_GRACE - timedelta(minutes=1),
            reason="lost wake-up", status=AgentWakeup.STATUS_SCHEDULED,
        )
        with patch("league.tasks.run_agent_wakeup.delay") as mock_delay:
            sweep_overdue_wakeups()
        mock_delay.assert_called_once_with(overdue.id)

    def test_does_not_touch_a_wakeup_still_inside_the_grace_window(self):
        """Merely running a little late (worker backlog) isn't "lost" --
        touching it here would race its own apply_async(eta=...) firing
        on time and risk a double-run."""
        from .tasks import sweep_overdue_wakeups
        AgentWakeup.objects.create(
            competitor=self.competitor, run_at=timezone.now() - timedelta(minutes=2),
            reason="running a bit late", status=AgentWakeup.STATUS_SCHEDULED,
        )
        with patch("league.tasks.run_agent_wakeup.delay") as mock_delay:
            sweep_overdue_wakeups()
        mock_delay.assert_not_called()

    def test_ignores_a_wakeup_that_is_not_still_scheduled(self):
        from .tasks import OVERDUE_SWEEP_GRACE, sweep_overdue_wakeups
        AgentWakeup.objects.create(
            competitor=self.competitor, run_at=timezone.now() - OVERDUE_SWEEP_GRACE - timedelta(minutes=1),
            reason="already handled", status=AgentWakeup.STATUS_COMPLETED,
        )
        with patch("league.tasks.run_agent_wakeup.delay") as mock_delay:
            sweep_overdue_wakeups()
        mock_delay.assert_not_called()

    def test_cancels_instead_of_redispatching_a_very_stale_wakeup(self):
        """Past MAX_SWEEP_AGE, re-firing means acting on a genuinely stale
        prompt (and, after a long-dead sweep task or extended outage,
        dumping a whole backlog of them into the LLM at once) -- cancel
        instead."""
        from .tasks import MAX_SWEEP_AGE, sweep_overdue_wakeups
        stale = AgentWakeup.objects.create(
            competitor=self.competitor, run_at=timezone.now() - MAX_SWEEP_AGE - timedelta(minutes=1),
            reason="ancient wake-up", status=AgentWakeup.STATUS_SCHEDULED,
        )
        with patch("league.tasks.run_agent_wakeup.delay") as mock_delay:
            sweep_overdue_wakeups()
        mock_delay.assert_not_called()
        stale.refresh_from_db()
        self.assertEqual(stale.status, AgentWakeup.STATUS_CANCELLED)


class DailyBriefSharingTests(TestCase):
    """DailyBrief exists to turn N competitors' worth of Steady calls into 1
    per news_slot per day (league/models.py's DailyBrief docstring) --
    these confirm that actually happens, not just that inject_news still
    threads a non-empty string through."""

    def setUp(self):
        self.competition_a = Competition.objects.create(
            name="Brief League A", starting_capital=Decimal("5000"),
            start_date=date(2026, 8, 12), end_date=date(2026, 11, 4),
        )
        self.competition_b = Competition.objects.create(
            name="Brief League B", starting_capital=Decimal("5000"),
            start_date=date(2026, 8, 12), end_date=date(2026, 11, 4),
        )
        for competition in (self.competition_a, self.competition_b):
            ForcedCheckin.objects.create(
                competition=competition, hour=8, minute=0, label="8am news scan",
                prompt="Read overnight news.", inject_news=True, news_slot="morning",
            )
            Competitor.objects.create(
                competition=competition, name="AI", kind=Competitor.KIND_AI,
                assistant_model=AssistantModel.get_default(), cash_balance=Decimal("5000"),
            )

    @override_settings(STEADY_API_TOKEN="test-token")
    def test_shared_news_slot_across_competitions_fetches_headlines_once(self):
        """Two competitions, each with one competitor, both using
        news_slot='morning' -- get_news should be called once total for
        this tick, not once per competitor. STEADY_API_TOKEN must be
        configured here (S-01's own gate) or _headlines_block short-
        circuits before ever calling the mocked get_news below."""
        eight_am_et = datetime.combine(date(2026, 8, 13), time(8, 0), tzinfo=EASTERN)
        with patch("league.tasks.timezone.now", return_value=eight_am_et), \
             patch("league.tasks.steady.get_news", return_value=[{"title": "Fed holds rates"}]) as mock_get_news, \
             patch("league.tasks.pricing.get_quote", return_value=None), \
             patch("league.tasks.run_agent_wakeup.delay") as mock_delay:
            dispatch_forced_checkins()
        mock_get_news.assert_called_once()
        self.assertEqual(mock_delay.call_count, 2)
        self.assertEqual(DailyBrief.objects.filter(date=date(2026, 8, 13), slot="morning").count(), 1)
        # Both competitors received the same generated brief text.
        news_contents = {call.args[2] for call in mock_delay.call_args_list}
        self.assertEqual(len(news_contents), 1)
        self.assertIn("Fed holds rates", news_contents.pop())


class ForcedCheckinNoInjectNewsTests(TestCase):
    """Deliberately its own TestCase, not a method on DailyBriefSharingTests
    -- that class's setUp creates two OTHER competitions with an 8am
    inject_news=True ForcedCheckin, and dispatch_forced_checkins now fires
    any not-yet-fired-today slot whose time has already passed (not just
    an exact hour==now/minute==now match, see that function's docstring),
    so at a mocked 3:15pm "now" those unrelated 8am rows would legitimately
    fire too and call get_news -- correct behavior for them, but it would
    make this test's assert_not_called() fail for a reason that has
    nothing to do with what this test actually checks."""

    def test_forced_checkin_without_inject_news_gets_no_brief(self):
        competition = Competition.objects.create(
            name="No Brief League", starting_capital=Decimal("5000"),
            start_date=date(2026, 8, 12), end_date=date(2026, 11, 4),
        )
        ForcedCheckin.objects.create(
            competition=competition, hour=15, minute=15, label="pre-close",
            prompt="Decide if you want to adjust anything before the close.",
        )
        Competitor.objects.create(
            competition=competition, name="AI", kind=Competitor.KIND_AI,
            assistant_model=AssistantModel.get_default(), cash_balance=Decimal("5000"),
        )
        pre_close_et = datetime.combine(date(2026, 8, 13), time(15, 15), tzinfo=EASTERN)
        with patch("league.tasks.timezone.now", return_value=pre_close_et), \
             patch("league.tasks.steady.get_news") as mock_get_news, \
             patch("league.tasks.run_agent_wakeup.delay") as mock_delay:
            dispatch_forced_checkins()
        mock_get_news.assert_not_called()
        mock_delay.assert_called_once_with(mock_delay.call_args.args[0], AgentTurn.TRIGGER_FORCED, "")
        self.assertFalse(DailyBrief.objects.exists())


class DailyBriefMarketSnapshotProviderTests(TestCase):
    """_market_snapshot_block used to call companies.services.steady.get_quote
    directly, bypassing pricing.py -- the one module every other live-quote
    caller (valuation.py, execution.py, both tool surfaces) goes through so
    PRICING_PROVIDER actually controls where a quote comes from. That meant
    a Tradier-only deployment (STEADY_API_TOKEN unset) silently got an
    empty "Broad market" section in every daily brief, with no error
    anywhere -- exactly the single-token operator pricing.py's own
    docstring says this project is meant to support. These tests pin the
    fix at the pricing.py boundary rather than the provider one, so they'd
    still pass if a third provider were added to pricing.py later."""

    @override_settings(PRICING_PROVIDER="tradier")
    def test_tradier_provider_is_actually_used_for_the_snapshot(self):
        with patch("companies.services.steady.get_quote") as mock_steady_quote, \
             patch("companies.services.tradier.get_quote", return_value=None) as mock_tradier_quote:
            _market_snapshot_block()
        mock_tradier_quote.assert_called()
        mock_steady_quote.assert_not_called()

    @override_settings(PRICING_PROVIDER="steady")
    def test_steady_provider_is_actually_used_for_the_snapshot(self):
        with patch("companies.services.steady.get_quote", return_value=None) as mock_steady_quote, \
             patch("companies.services.tradier.get_quote") as mock_tradier_quote:
            _market_snapshot_block()
        mock_steady_quote.assert_called()
        mock_tradier_quote.assert_not_called()

    @override_settings(PRICING_PROVIDER="tradier")
    def test_tradier_quotes_populate_the_snapshot(self):
        """Confirms this isn't just "the right function gets called" --
        Tradier's raw shape (pricing.py's own _tradier_quote_to_steady_shape
        normalization) actually produces a usable line, not silently empty
        output from a shape mismatch."""
        raw = {"symbol": "SPY", "last": 550.0, "change_percentage": 1.25}
        with patch("companies.services.tradier.get_quote", return_value=raw):
            block = _market_snapshot_block()
        self.assertIn("SPY: 550.0 (+1.25%)", block)


class DailyBriefHeadlinesGatingTests(TestCase):
    """_headlines_block has no Tradier equivalent (get_news is Steady-only,
    per pricing.py's own module docstring), so a Tradier-only deployment
    with no STEADY_API_TOKEN should say so plainly rather than reusing the
    generic "couldn't fetch" message a transient failure gets -- those are
    different problems (misconfiguration vs. a flaky call) and shouldn't
    look identical to whoever reads the brief."""

    @override_settings(STEADY_API_TOKEN="")
    def test_unconfigured_steady_gets_its_own_message_not_a_network_call(self):
        with patch("companies.services.steady.get_news") as mock_get_news:
            block = _headlines_block()
        mock_get_news.assert_not_called()
        self.assertIn("STEADY_API_TOKEN isn't configured", block)

    @override_settings(STEADY_API_TOKEN="test-token")
    def test_configured_but_failing_gets_the_generic_message(self):
        with patch("companies.services.steady.get_news", side_effect=RuntimeError("Steady is down")):
            block = _headlines_block()
        self.assertIn("Couldn't fetch headlines automatically", block)
        self.assertNotIn("isn't configured", block)


class SellAllPositionsCommandTests(TestCase):
    """The `sell_all_positions` management command -- flattens holdings by
    submitting real closing Trades (never touches Position/cash_balance
    directly), for "I'm just testing, stop the noise" cleanup."""

    def setUp(self):
        self.competition = Competition.objects.create(
            name="Flatten Test League", starting_capital=Decimal("5000"),
            start_date=date(2026, 8, 12), end_date=date(2026, 11, 4),
        )
        self.competitor = Competitor.objects.create(
            competition=self.competition, name="Flattener", kind=Competitor.KIND_AI,
            cash_balance=Decimal("5000"),
        )

    def _call(self, *args):
        out = StringIO()
        call_command("sell_all_positions", *args, stdout=out, stderr=out)
        return out.getvalue()

    def test_closes_a_long_stock_position(self):
        Position.objects.create(
            competitor=self.competitor, instrument_type=Position.INSTRUMENT_STOCK,
            ticker="AAA", quantity=10, avg_cost=Decimal("100"),
        )
        with patch("companies.services.steady.get_quote", return_value=_quote(ask=105, bid=100)):
            self._call("--competitor", str(self.competitor.id))
        self.assertFalse(Position.objects.filter(competitor=self.competitor, ticker="AAA").exists())
        trade = Trade.objects.get(competitor=self.competitor, ticker="AAA")
        self.assertEqual(trade.side, Trade.SIDE_SELL)
        self.assertEqual(trade.status, Trade.STATUS_FILLED)
        self.assertEqual(trade.fill_price, Decimal("100"))  # bid, matches SELL fill convention

    def test_closes_covered_call_before_the_stock_it_covers(self):
        """Closing the stock first would be rejected (still backing an
        open covered call) -- the command must close the short call leg
        first regardless of Position.objects ordering."""
        stock = Position.objects.create(
            competitor=self.competitor, instrument_type=Position.INSTRUMENT_STOCK,
            ticker="BBB", quantity=100, avg_cost=Decimal("50"),
        )
        Position.objects.create(
            competitor=self.competitor, instrument_type=Position.INSTRUMENT_CALL,
            ticker="BBB", strike=Decimal("60"), expiration=EXPIRATION,
            quantity=1, is_short=True, avg_cost=Decimal("2"), covers=stock,
        )
        with patch("companies.services.steady.get_quote", return_value=_quote(ask=55, bid=52)), \
             patch("companies.services.steady.get_options_chain", return_value=_chain(calls=[_contract(60, 3, 2.5)])):
            output = self._call("--competitor", str(self.competitor.id))
        self.assertNotIn("Failed to close", output)
        self.assertFalse(Position.objects.filter(competitor=self.competitor).exists())
        self.assertEqual(
            Trade.objects.get(competitor=self.competitor, instrument_type=Position.INSTRUMENT_CALL).side,
            Trade.SIDE_BUY_TO_CLOSE,
        )
        self.assertEqual(
            Trade.objects.get(competitor=self.competitor, instrument_type=Position.INSTRUMENT_STOCK).side,
            Trade.SIDE_SELL,
        )

    def test_dry_run_closes_nothing(self):
        Position.objects.create(
            competitor=self.competitor, instrument_type=Position.INSTRUMENT_STOCK,
            ticker="CCC", quantity=5, avg_cost=Decimal("20"),
        )
        output = self._call("--competitor", str(self.competitor.id), "--dry-run")
        self.assertIn("would sell", output)
        self.assertTrue(Position.objects.filter(competitor=self.competitor, ticker="CCC").exists())
        self.assertFalse(Trade.objects.filter(competitor=self.competitor).exists())

    def test_competition_scope_closes_every_competitor(self):
        other = Competitor.objects.create(
            competition=self.competition, name="Other", kind=Competitor.KIND_AI, cash_balance=Decimal("5000"),
        )
        Position.objects.create(
            competitor=self.competitor, instrument_type=Position.INSTRUMENT_STOCK,
            ticker="DDD", quantity=1, avg_cost=Decimal("10"),
        )
        Position.objects.create(
            competitor=other, instrument_type=Position.INSTRUMENT_STOCK,
            ticker="EEE", quantity=1, avg_cost=Decimal("10"),
        )
        with patch("companies.services.steady.get_quote", return_value=_quote(ask=11, bid=9)):
            self._call("--competition", str(self.competition.id))
        self.assertFalse(Position.objects.filter(competitor__competition=self.competition).exists())

    def test_no_scope_flag_errors(self):
        with self.assertRaises(Exception):
            call_command("sell_all_positions")

    def test_unknown_competitor_errors(self):
        from django.core.management.base import CommandError
        with self.assertRaises(CommandError):
            call_command("sell_all_positions", "--competitor", "999999")

    # --- an already-expired option can't go through submit_trade at all
    # (league.services.execution's option_expired check), so this command
    # must route those through execution.settle_expired_position instead --
    # see the module docstring's "flatten everything now" reasoning. ---

    PAST_EXPIRATION = date(2020, 1, 17)

    def test_settles_an_already_expired_covered_call_then_frees_the_shares_to_sell(self):
        """Before this fix: the call can't close (submit_trade refuses an
        expired contract outright) and the shares can't sell either
        (still "covering" an open call as far as _covered_call_available
        is concerned) -- permanently stuck with no path out. Must settle
        the call first (via execution.settle_expired_position, which frees
        the collateral) so the stock can then close normally in the same
        run, same as any other flatten-everything pass."""
        stock = Position.objects.create(
            competitor=self.competitor, instrument_type=Position.INSTRUMENT_STOCK,
            ticker="FFF", quantity=100, avg_cost=Decimal("50"),
        )
        Position.objects.create(
            competitor=self.competitor, instrument_type=Position.INSTRUMENT_CALL,
            ticker="FFF", strike=Decimal("60"), expiration=self.PAST_EXPIRATION,
            quantity=1, is_short=True, avg_cost=Decimal("2"), covers=stock,
        )
        with patch("companies.services.pricing.get_quote", return_value=_quote(ask=55, bid=52, last=53)):
            output = self._call("--competitor", str(self.competitor.id))
        self.assertNotIn("Failed", output)
        self.assertIn("Settled", output)
        self.assertFalse(Position.objects.filter(competitor=self.competitor).exists())
        self.assertEqual(
            Trade.objects.get(competitor=self.competitor, instrument_type=Position.INSTRUMENT_STOCK).side,
            Trade.SIDE_SELL,
        )

    def test_dry_run_reports_an_expired_option_would_settle_not_close(self):
        Position.objects.create(
            competitor=self.competitor, instrument_type=Position.INSTRUMENT_PUT,
            ticker="GGG", strike=Decimal("40"), expiration=self.PAST_EXPIRATION,
            quantity=1, is_short=True, avg_cost=Decimal("3"),
        )
        output = self._call("--competitor", str(self.competitor.id), "--dry-run")
        self.assertIn("would settle (expired)", output)
        self.assertTrue(Position.objects.filter(competitor=self.competitor, ticker="GGG").exists())
        self.assertFalse(Trade.objects.filter(competitor=self.competitor).exists())

    def test_settle_reports_failure_without_a_quote_and_leaves_the_position(self):
        """The scenario this whole path exists for: a quote that's been
        missing long enough that the nightly settle_expired_options batch
        has never resolved it either. Must fail loudly, not silently
        pretend to close it."""
        Position.objects.create(
            competitor=self.competitor, instrument_type=Position.INSTRUMENT_PUT,
            ticker="HHH", strike=Decimal("40"), expiration=self.PAST_EXPIRATION,
            quantity=1, is_short=True, avg_cost=Decimal("3"),
        )
        with patch("companies.services.pricing.get_quote", return_value=None):
            output = self._call("--competitor", str(self.competitor.id))
        self.assertIn("Failed to settle", output)
        self.assertTrue(Position.objects.filter(competitor=self.competitor, ticker="HHH").exists())


class SetPriceTargetsToolTests(TestCase):
    """The AI-facing set_price_targets tool (league/tools.py) -- revises
    target_price/dead_price on an already-open position without a trade,
    the AI-side equivalent of PositionPriceTargetsView (ApiTests)."""

    def setUp(self):
        self.competition = Competition.objects.create(
            name="Price Targets Tool Test", starting_capital=Decimal("5000"),
            start_date=date(2026, 8, 12), end_date=date(2026, 11, 4),
        )
        self.competitor = Competitor.objects.create(
            competition=self.competition, name="Targeter", kind=Competitor.KIND_AI,
            cash_balance=Decimal("5000"),
        )

    def _tool(self):
        from .tools import build_league_tools
        tools = build_league_tools(self.competitor)
        return {t.name: t for t in tools}["set_price_targets"]

    def test_updates_an_existing_stock_position(self):
        Position.objects.create(
            competitor=self.competitor, instrument_type=Position.INSTRUMENT_STOCK,
            ticker="AAA", quantity=10, avg_cost=Decimal("100"),
        )
        result = self._tool().call({"ticker": "AAA", "instrument_type": "stock", "target_price": "120", "dead_price": "90"})
        self.assertIn("Updated AAA", result)
        position = Position.objects.get(competitor=self.competitor, ticker="AAA")
        self.assertEqual(position.target_price, Decimal("120"))
        self.assertEqual(position.dead_price, Decimal("90"))

    def test_updates_an_existing_option_position_by_strike_and_expiration(self):
        Position.objects.create(
            competitor=self.competitor, instrument_type=Position.INSTRUMENT_CALL,
            ticker="BBB", strike=Decimal("150"), expiration=EXPIRATION, quantity=1, avg_cost=Decimal("5"),
        )
        result = self._tool().call({
            "ticker": "BBB", "instrument_type": "call", "strike": "150",
            "expiration": EXPIRATION.isoformat(), "target_price": "10",
        })
        self.assertIn("Updated BBB", result)
        position = Position.objects.get(competitor=self.competitor, ticker="BBB")
        self.assertEqual(position.target_price, Decimal("10"))

    def test_errors_with_no_matching_position(self):
        with self.assertRaises(ToolError):
            self._tool().call({"ticker": "NOPE", "instrument_type": "stock", "target_price": "10"})

    def test_errors_when_neither_target_nor_dead_given(self):
        Position.objects.create(
            competitor=self.competitor, instrument_type=Position.INSTRUMENT_STOCK,
            ticker="AAA", quantity=10, avg_cost=Decimal("100"),
        )
        with self.assertRaises(ToolError):
            self._tool().call({"ticker": "AAA", "instrument_type": "stock"})

    def test_errors_when_option_missing_strike_or_expiration(self):
        with self.assertRaises(ToolError):
            self._tool().call({"ticker": "BBB", "instrument_type": "call", "target_price": "10"})

    def test_does_not_touch_dead_price_when_only_target_given(self):
        Position.objects.create(
            competitor=self.competitor, instrument_type=Position.INSTRUMENT_STOCK,
            ticker="AAA", quantity=10, avg_cost=Decimal("100"), dead_price=Decimal("80"),
        )
        self._tool().call({"ticker": "AAA", "instrument_type": "stock", "target_price": "120"})
        position = Position.objects.get(competitor=self.competitor, ticker="AAA")
        self.assertEqual(position.target_price, Decimal("120"))
        self.assertEqual(position.dead_price, Decimal("80"))


class CancelTradeToolTests(TestCase):
    """The AI-facing cancel_trade tool (league/tools.py) -- the AI-side
    equivalent of CompetitorTradeCancelView (ApiTests), for pulling back a
    still-STATUS_PENDING order queued because Competition.
    restrict_after_hours_trading was set and the market was closed at
    submission."""

    def setUp(self):
        self.competition = Competition.objects.create(
            name="Cancel Tool Test", starting_capital=Decimal("5000"),
            start_date=date(2026, 8, 12), end_date=date(2026, 11, 4),
            restrict_after_hours_trading=True,
        )
        self.competitor = Competitor.objects.create(
            competition=self.competition, name="Canceller", kind=Competitor.KIND_AI,
            cash_balance=Decimal("5000"),
        )
        self.other_competitor = Competitor.objects.create(
            competition=self.competition, name="Other", kind=Competitor.KIND_AI,
            cash_balance=Decimal("5000"),
        )

    def _tool(self, competitor=None):
        from .tools import build_league_tools
        tools = build_league_tools(competitor or self.competitor)
        return {t.name: t for t in tools}["cancel_trade"]

    def test_cancels_own_pending_trade(self):
        with patch("league.services.market_hours.is_market_open", return_value=False):
            pending = submit_trade(self.competitor, "TEST", Trade.SIDE_BUY, Position.INSTRUMENT_STOCK, 10, "queued")
        result = self._tool().call({"trade_id": pending.id})
        self.assertIn("CANCELLED", result)
        pending.refresh_from_db()
        self.assertEqual(pending.status, Trade.STATUS_CANCELLED)

    def test_errors_on_another_competitors_trade(self):
        with patch("league.services.market_hours.is_market_open", return_value=False):
            pending = submit_trade(self.other_competitor, "TEST", Trade.SIDE_BUY, Position.INSTRUMENT_STOCK, 10, "queued")
        with self.assertRaises(ToolError):
            self._tool().call({"trade_id": pending.id})
        pending.refresh_from_db()
        self.assertEqual(pending.status, Trade.STATUS_PENDING)  # untouched

    def test_errors_on_unknown_trade_id(self):
        with self.assertRaises(ToolError):
            self._tool().call({"trade_id": 999999})

    def test_errors_on_already_filled_trade(self):
        with patch("league.services.market_hours.is_market_open", return_value=True), \
             patch("companies.services.steady.get_quote", return_value=_quote(ask=100, bid=99)):
            trade = submit_trade(self.competitor, "TEST", Trade.SIDE_BUY, Position.INSTRUMENT_STOCK, 10, "filled")
        with self.assertRaises(ToolError):
            self._tool().call({"trade_id": trade.id})


class GetStockQuotesToolTests(TestCase):
    """The AI-facing get_stock_quotes batch tool (league/tools.py) -- lets
    a competitor check its whole watchlist in one tool-call round instead
    of one get_stock_quote call per ticker, see that tool's own docstring
    for why (LEAGUE_MAX_TOOL_ITERATIONS budget)."""

    def setUp(self):
        self.competition = Competition.objects.create(
            name="Batch Quote Tool Test", starting_capital=Decimal("5000"),
            start_date=date(2026, 8, 12), end_date=date(2026, 11, 4),
        )
        self.competitor = Competitor.objects.create(
            competition=self.competition, name="Watcher", kind=Competitor.KIND_AI,
            cash_balance=Decimal("5000"),
        )

    def _tool(self):
        from .tools import build_league_tools
        tools = build_league_tools(self.competitor)
        return {t.name: t for t in tools}["get_stock_quotes"]

    def test_returns_a_quote_per_ticker(self):
        quotes = {"AAA": _quote(ask=105, bid=100), "BBB": _quote(ask=55, bid=50)}
        with patch("companies.services.steady.get_quote", side_effect=lambda ticker, asset_class: quotes[ticker]):
            result = json.loads(self._tool().call({"tickers": ["AAA", "BBB"]}))
        self.assertEqual(set(result.keys()), {"AAA", "BBB"})
        self.assertEqual(result["AAA"]["ask"], 105)
        self.assertEqual(result["BBB"]["ask"], 55)

    def test_one_bad_ticker_does_not_block_the_rest(self):
        def _side_effect(ticker, asset_class):
            if ticker == "GOOD":
                return _quote(ask=105, bid=100)
            raise RuntimeError("Steady is down")

        with patch("companies.services.steady.get_quote", side_effect=_side_effect):
            result = json.loads(self._tool().call({"tickers": ["GOOD", "BAD"]}))
        self.assertEqual(result["GOOD"]["ask"], 105)
        self.assertIn("Error", result["BAD"])

    def test_unconfigured_steady_reports_per_ticker_not_a_hard_error(self):
        with patch("companies.services.steady.get_quote", return_value=None):
            result = json.loads(self._tool().call({"tickers": ["AAA"]}))
        self.assertIn("isn't configured", result["AAA"])

    def test_empty_tickers_raises(self):
        with self.assertRaises(ToolError):
            self._tool().call({"tickers": []})

    def test_too_many_tickers_raises(self):
        with self.assertRaises(ToolError):
            self._tool().call({"tickers": [f"T{i}" for i in range(21)]})

    def test_invalid_asset_class_raises_without_calling_steady(self):
        with patch("companies.services.steady.get_quote") as mock_get_quote:
            with self.assertRaises(ToolError):
                self._tool().call({"tickers": ["AAA"], "asset_class": "BONDS"})
        mock_get_quote.assert_not_called()


class GetCompetitionStandingsToolTests(TestCase):
    """The AI-facing get_competition_standings tool (league/tools.py) --
    gives every competitor equal access to rank/return info, the same
    numbers ScoreboardView (league/views.py) computes for the public
    scoreboard, so a strategy that should play differently when ahead vs.
    behind has something to check besides its own isolated P/L."""

    def setUp(self):
        self.competition = Competition.objects.create(
            name="Standings Tool Test", starting_capital=Decimal("5000"),
            start_date=date(2026, 8, 12), end_date=date(2026, 11, 4),
        )
        self.leader = Competitor.objects.create(
            competition=self.competition, name="Leader", kind=Competitor.KIND_AI, cash_balance=Decimal("6000"),
        )
        self.caller = Competitor.objects.create(
            competition=self.competition, name="Caller", kind=Competitor.KIND_AI, cash_balance=Decimal("5000"),
        )
        self.trailing = Competitor.objects.create(
            competition=self.competition, name="Trailing", kind=Competitor.KIND_AI, cash_balance=Decimal("4000"),
        )

    def _tool(self):
        from .tools import build_league_tools
        tools = build_league_tools(self.caller)
        return {t.name: t for t in tools}["get_competition_standings"]

    def test_ranks_by_total_return_best_first(self):
        result = json.loads(self._tool().call({}))
        names_in_order = [row["name"] for row in result["standings"]]
        self.assertEqual(names_in_order, ["Leader", "Caller", "Trailing"])
        self.assertEqual([row["rank"] for row in result["standings"]], [1, 2, 3])

    def test_your_rank_and_is_you_flag_point_at_the_calling_competitor(self):
        result = json.loads(self._tool().call({}))
        self.assertEqual(result["your_rank"], 2)
        caller_rows = [row for row in result["standings"] if row["is_you"]]
        self.assertEqual(len(caller_rows), 1)
        self.assertEqual(caller_rows[0]["name"], "Caller")
        self.assertFalse(any(row["is_you"] for row in result["standings"] if row["name"] != "Caller"))

    def test_competitor_count_matches_the_competition(self):
        result = json.loads(self._tool().call({}))
        self.assertEqual(result["competitor_count"], 3)

    def test_total_return_pct_computed_against_starting_capital(self):
        result = json.loads(self._tool().call({}))
        leader_row = next(row for row in result["standings"] if row["name"] == "Leader")
        # 6000 cash, no positions: (6000 - 5000) / 5000 = 0.20
        self.assertEqual(Decimal(leader_row["total_return_pct"]), Decimal("0.20"))

    def test_does_not_expose_other_competitors_positions(self):
        result = json.loads(self._tool().call({}))
        for row in result["standings"]:
            self.assertNotIn("positions", row)
            self.assertNotIn("cash_balance", row)


def _reddit_post(id_="abc123", subreddit="wallstreetbets", title="Test post", selftext=""):
    return {
        "id": id_, "title": title, "selftext": selftext, "author": "someone",
        "subreddit": subreddit, "score": 100, "upvote_ratio": 0.9,
        "num_comments": 10, "created_utc": 1755000000, "permalink": f"/r/{subreddit}/comments/{id_}/",
        "url": "https://reddit.com",
    }


@override_settings(STEADY_API_TOKEN="test-token")
class GetRedditSentimentToolTests(TestCase):
    """The AI-facing get_reddit_sentiment tool (league/tools.py) -- raw
    Reddit content for one ticker, no computed sentiment score (see that
    tool's own docstring for why: SteadyAPI's subreddit filter on this
    endpoint is confirmed live not to work, so results are read/judged by
    the model itself, not pre-scoped or pre-digested here)."""

    def setUp(self):
        self.competition = Competition.objects.create(
            name="Reddit Sentiment Tool Test", starting_capital=Decimal("5000"),
            start_date=date(2026, 8, 12), end_date=date(2026, 11, 4),
        )
        self.competitor = Competitor.objects.create(
            competition=self.competition, name="Scanner", kind=Competitor.KIND_AI,
            cash_balance=Decimal("5000"),
        )

    def _tool(self):
        from .tools import build_league_tools
        tools = build_league_tools(self.competitor)
        return {t.name: t for t in tools}["get_reddit_sentiment"]

    def test_returns_posts_from_search(self):
        posts = [_reddit_post(id_="p1"), _reddit_post(id_="p2")]
        with patch("companies.services.steady.get_reddit_search", return_value=posts) as mock_search:
            result = json.loads(self._tool().call({"ticker": "SLS"}))
        self.assertEqual(len(result), 2)
        mock_search.assert_called_once_with("SLS")

    def test_caps_to_top_ten(self):
        posts = [_reddit_post(id_=str(i)) for i in range(25)]
        with patch("companies.services.steady.get_reddit_search", return_value=posts):
            result = json.loads(self._tool().call({"ticker": "SLS"}))
        self.assertEqual(len(result), 10)

    def test_no_results_returns_a_message_not_an_empty_list(self):
        with patch("companies.services.steady.get_reddit_search", return_value=[]):
            result = self._tool().call({"ticker": "ZZZZZ"})
        self.assertIn("No Reddit posts found", result)

    def test_unconfigured_steady_raises_tool_error(self):
        with patch("companies.services.steady.get_reddit_search", return_value=None):
            with self.assertRaises(ToolError):
                self._tool().call({"ticker": "SLS"})


@override_settings(STEADY_API_TOKEN="test-token", OPENAI_API_KEY="test-key")
class ScanSubredditForTickersToolTests(TestCase):
    """The AI-facing scan_subreddit_for_tickers tool (league/tools.py) --
    the discovery half of the Reddit feature: fetch a subreddit's current
    posts (genuinely subreddit-scoped, unlike get_reddit_sentiment's
    search) and extract which tickers are actually being discussed via
    reddit_tickers.extract_tickers, not a keyword/cashtag scan."""

    def setUp(self):
        self.competition = Competition.objects.create(
            name="Scan Subreddit Tool Test", starting_capital=Decimal("5000"),
            start_date=date(2026, 8, 12), end_date=date(2026, 11, 4),
        )
        self.competitor = Competitor.objects.create(
            competition=self.competition, name="Scanner", kind=Competitor.KIND_AI,
            cash_balance=Decimal("5000"),
        )

    def _tool(self):
        from .tools import build_league_tools
        tools = build_league_tools(self.competitor)
        return {t.name: t for t in tools}["scan_subreddit_for_tickers"]

    def test_returns_extracted_tickers(self):
        posts = [_reddit_post(id_="p1"), _reddit_post(id_="p2")]
        with patch("companies.services.steady.get_reddit_posts", return_value=posts) as mock_posts, \
             patch("companies.services.reddit_tickers.extract_tickers", return_value=["MRNA", "SLS"]) as mock_extract:
            result = json.loads(self._tool().call({"subreddit": "wallstreetbets"}))
        self.assertEqual(result, {"tickers": ["MRNA", "SLS"], "posts_scanned": 2})
        mock_posts.assert_called_once_with("wallstreetbets", sort_type="hot")
        mock_extract.assert_called_once_with(posts)

    def test_no_posts_returns_a_message_without_calling_extraction(self):
        with patch("companies.services.steady.get_reddit_posts", return_value=[]), \
             patch("companies.services.reddit_tickers.extract_tickers") as mock_extract:
            result = self._tool().call({"subreddit": "emptysubforsure"})
        self.assertIn("No posts found", result)
        mock_extract.assert_not_called()

    def test_unconfigured_steady_raises_tool_error(self):
        with patch("companies.services.steady.get_reddit_posts", return_value=None):
            with self.assertRaises(ToolError):
                self._tool().call({"subreddit": "wallstreetbets"})

    def test_unconfigured_openai_raises_tool_error(self):
        posts = [_reddit_post()]
        with patch("companies.services.steady.get_reddit_posts", return_value=posts), \
             patch("companies.services.reddit_tickers.extract_tickers", return_value=None):
            with self.assertRaises(ToolError):
                self._tool().call({"subreddit": "wallstreetbets"})

    def test_invalid_sort_type_raises(self):
        # Real get_reddit_posts (not mocked) does this validation itself,
        # before ever calling http.get -- STEADY_API_TOKEN just needs to
        # be non-empty to get past that earlier check first.
        with override_settings(STEADY_API_TOKEN="test-token"):
            with self.assertRaises(ToolError):
                self._tool().call({"subreddit": "wallstreetbets", "sort_type": "bogus"})


@override_settings(BRAVE_API_KEY="test-brave-key")
class SearchWebToolTests(TestCase):
    """The AI-facing search_web tool (league/tools.py) -- same tool as
    assistant/tools.py's version, mirrored here for parity. Brave
    Search-backed generic open-web search, separate from whichever
    provider's own native web_search server tool is also attached
    (league/tasks.py, llm_openai.py, llm_gemini.py)."""

    def setUp(self):
        self.competition = Competition.objects.create(
            name="Search Web Tool Test", starting_capital=Decimal("5000"),
            start_date=date(2026, 8, 12), end_date=date(2026, 11, 4),
        )
        self.competitor = Competitor.objects.create(
            competition=self.competition, name="Searcher", kind=Competitor.KIND_AI,
            cash_balance=Decimal("5000"),
        )

    def _tool(self):
        from .tools import build_league_tools
        tools = build_league_tools(self.competitor)
        return {t.name: t for t in tools}["search_web"]

    def test_returns_results(self):
        results = [{"title": "Rocket Lab", "url": "https://example.com", "description": "..."}]
        with patch("companies.services.brave_search.search_web", return_value=results) as mock_search:
            result = json.loads(self._tool().call({"query": "Rocket Lab Neutron"}))
        self.assertEqual(result, results)
        mock_search.assert_called_once_with("Rocket Lab Neutron")

    def test_no_results_returns_a_message_not_an_empty_list(self):
        with patch("companies.services.brave_search.search_web", return_value=[]):
            result = self._tool().call({"query": "zzzznoresultszzzz"})
        self.assertIn("No web results found", result)

    def test_unconfigured_raises_tool_error(self):
        with patch("companies.services.brave_search.search_web", return_value=None):
            with self.assertRaises(ToolError):
                self._tool().call({"query": "anything"})


@override_settings(STEADY_API_TOKEN="test-token", OPENAI_API_KEY="test-key", BRAVE_API_KEY="test-brave-key")
class ToolCapabilityGatingTests(TestCase):
    """build_league_tools() filters out provider-exclusive tools whose
    capability isn't configured, rather than registering them and letting
    every call raise ToolError (assistant/tools.py's build_tools() mirrors
    the same gate -- see its own ToolCapabilityGatingTests). Covers the
    tools not already pinned by their own class above
    (GetIntradayCandlesToolTests already covers get_intraday_candles).

    Class-level tokens set so "fully configured" is this class's actual
    default, matching the first test below -- individual tests override
    a single one back to "" to test that specific absence, relying on
    override_settings correctly layering per-test on top of this."""

    def setUp(self):
        self.competition = Competition.objects.create(
            name="Capability Gating Test", starting_capital=Decimal("5000"),
            start_date=date(2026, 8, 12), end_date=date(2026, 11, 4),
        )
        self.competitor = Competitor.objects.create(
            competition=self.competition, name="Gated", kind=Competitor.KIND_AI,
            cash_balance=Decimal("5000"),
        )

    def _tool_names(self):
        from .tools import build_league_tools
        return {t.name for t in build_league_tools(self.competitor)}

    def test_all_gated_tools_present_when_fully_configured(self):
        names = self._tool_names()
        for name in ("get_market_news", "get_reddit_sentiment", "scan_subreddit_for_tickers", "search_web"):
            self.assertIn(name, names)

    @override_settings(BRAVE_API_KEY="")
    def test_search_web_absent_without_brave_key(self):
        self.assertNotIn("search_web", self._tool_names())

    @override_settings(STEADY_API_TOKEN="")
    def test_market_news_absent_without_steady_token(self):
        self.assertNotIn("get_market_news", self._tool_names())

    @override_settings(STEADY_API_TOKEN="")
    def test_reddit_sentiment_absent_without_steady_token(self):
        self.assertNotIn("get_reddit_sentiment", self._tool_names())

    @override_settings(STEADY_API_TOKEN="")
    def test_scan_subreddit_absent_without_steady_token(self):
        self.assertNotIn("scan_subreddit_for_tickers", self._tool_names())

    @override_settings(OPENAI_API_KEY="")
    def test_scan_subreddit_absent_without_openai_key_even_with_steady_configured(self):
        """Needs Steady (the fetch) AND OpenAI (ticker extraction) -- missing
        either one fails on every call, not just some inputs, so either one
        missing should be enough to drop it from the list."""
        self.assertNotIn("scan_subreddit_for_tickers", self._tool_names())


class ListFilingsToolTests(TestCase):
    """The AI-facing list_filings tool (league/tools.py) -- a trimmed copy
    of assistant/tools.py's version with the TrackedCompany-based
    "ingested" flag dropped: that tracks a human USER's dashboard state
    (search_research_clips/Weaviate ingestion), neither of which an AI
    competitor has (Competitor.user is null for AI competitors -- they
    aren't a login), so this just lists what EDGAR has."""

    def setUp(self):
        self.competition = Competition.objects.create(
            name="Filings Tool Test", starting_capital=Decimal("5000"),
            start_date=date(2026, 8, 12), end_date=date(2026, 11, 4),
        )
        self.competitor = Competitor.objects.create(
            competition=self.competition, name="Researcher", kind=Competitor.KIND_AI,
            cash_balance=Decimal("5000"),
        )

    def _tool(self):
        from .tools import build_league_tools
        tools = build_league_tools(self.competitor)
        return {t.name: t for t in tools}["list_filings"]

    def test_returns_filing_rows_without_an_ingested_flag(self):
        company = Company.objects.create(ticker="RKLB", cik=1819994)
        filings = [
            {
                "form": "10-K", "filing_date": "2026-02-20", "report_date": "2025-12-31",
                "accession_number": "0001-26-000001", "url": "https://example.com/10k",
            },
        ]
        with patch("league.tools.resolve_company", return_value=company), \
             patch("companies.services.sec_edgar.get_submissions", return_value={}), \
             patch("companies.services.sec_edgar.list_filings", return_value=filings):
            result = json.loads(self._tool().call({"ticker": "rklb"}))
        self.assertEqual(result, [
            {"form": "10-K", "filed": "2026-02-20", "period": "2025-12-31",
             "accession_number": "0001-26-000001", "url": "https://example.com/10k"},
        ])
        self.assertNotIn("ingested", result[0])

    def test_unknown_ticker_raises_tool_error(self):
        with patch("league.tools.resolve_company", side_effect=ValueError("No company found for ZZZZZ.")):
            with self.assertRaises(ToolError):
                self._tool().call({"ticker": "ZZZZZ"})

    def test_no_filings_returns_a_message(self):
        company = Company.objects.create(ticker="RKLB", cik=1819994)
        with patch("league.tools.resolve_company", return_value=company), \
             patch("companies.services.sec_edgar.get_submissions", return_value={}), \
             patch("companies.services.sec_edgar.list_filings", return_value=[]):
            result = self._tool().call({"ticker": "RKLB"})
        self.assertIn("No 10-K/10-Q filings found", result)


class GetCompanyReportToolTests(TestCase):
    """The AI-facing get_company_report tool (league/tools.py) -- identical
    to assistant/tools.py's version (no user-scoping involved, Company is
    shared across every user/competitor)."""

    def setUp(self):
        self.competition = Competition.objects.create(
            name="Report Tool Test", starting_capital=Decimal("5000"),
            start_date=date(2026, 8, 12), end_date=date(2026, 11, 4),
        )
        self.competitor = Competitor.objects.create(
            competition=self.competition, name="Researcher", kind=Competitor.KIND_AI,
            cash_balance=Decimal("5000"),
        )

    def _tool(self):
        from .tools import build_league_tools
        tools = build_league_tools(self.competitor)
        return {t.name: t for t in tools}["get_company_report"]

    def test_returns_the_cached_report(self):
        Company.objects.create(ticker="RKLB", cik=1819994)
        with patch("league.tools.build_report", return_value={"ticker": "RKLB", "market_cap": 1000}):
            result = json.loads(self._tool().call({"ticker": "rklb"}))
        self.assertEqual(result, {"ticker": "RKLB", "market_cap": 1000})

    def test_unscreened_ticker_raises_tool_error(self):
        with self.assertRaises(ToolError):
            self._tool().call({"ticker": "ZZZZZ"})


class SearchWikipediaToolTests(TestCase):
    """The AI-facing search_wikipedia tool (league/tools.py) -- identical
    to assistant/tools.py's version, a live/stateless search with nothing
    persisted or indexed."""

    def setUp(self):
        self.competition = Competition.objects.create(
            name="Wikipedia Tool Test", starting_capital=Decimal("5000"),
            start_date=date(2026, 8, 12), end_date=date(2026, 11, 4),
        )
        self.competitor = Competitor.objects.create(
            competition=self.competition, name="Researcher", kind=Competitor.KIND_AI,
            cash_balance=Decimal("5000"),
        )

    def _tool(self):
        from .tools import build_league_tools
        tools = build_league_tools(self.competitor)
        return {t.name: t for t in tools}["search_wikipedia"]

    def test_returns_search_results(self):
        pages = [{"title": "Rocket Lab", "snippet": "Rocket Lab USA, Inc. is..."}]
        with patch("companies.services.wikipedia_client.search_pages", return_value=pages) as mock_search:
            result = json.loads(self._tool().call({"query": "Rocket Lab Neutron"}))
        self.assertEqual(result, pages)
        mock_search.assert_called_once_with("Rocket Lab Neutron", limit=5)

    def test_no_results_returns_a_message(self):
        with patch("companies.services.wikipedia_client.search_pages", return_value=[]):
            result = self._tool().call({"query": "asdfghjklqwerty"})
        self.assertIn("No Wikipedia pages found", result)


class GetPriceHistoryToolTests(TestCase):
    """The AI-facing get_price_history tool (league/tools.py) -- last N
    daily OHLCV bars, the raw series get_technicals' ATR/SMA/high_50d are
    computed from. Real day-shaping/clamping logic lives in
    companies.services.technicals.get_price_history (companies/tests.py's
    GetPriceHistoryTests) -- this covers the tool's own error/cap
    handling on top of that."""

    def setUp(self):
        self.competition = Competition.objects.create(
            name="Price History Tool Test", starting_capital=Decimal("5000"),
            start_date=date(2026, 8, 12), end_date=date(2026, 11, 4),
        )
        self.competitor = Competitor.objects.create(
            competition=self.competition, name="Researcher", kind=Competitor.KIND_AI,
            cash_balance=Decimal("5000"),
        )

    def _tool(self):
        from .tools import build_league_tools
        tools = build_league_tools(self.competitor)
        return {t.name: t for t in tools}["get_price_history"]

    def test_returns_bars(self):
        bars = [{"date": "2026-08-20", "open": 100, "high": 102, "low": 99, "close": 101, "volume": 500}]
        with patch("companies.services.technicals.get_price_history", return_value=bars) as mock_history:
            result = json.loads(self._tool().call({"ticker": "rklb", "days": 10}))
        self.assertEqual(result, bars)
        mock_history.assert_called_once_with("rklb", days=10)

    def test_no_history_returns_a_message(self):
        with patch("companies.services.technicals.get_price_history", return_value=[]):
            result = self._tool().call({"ticker": "ZZZZZ"})
        self.assertIn("No price history found", result)

    def test_unavailable_provider_raises_tool_error(self):
        with patch("companies.services.technicals.get_price_history", return_value=None):
            with self.assertRaises(ToolError):
                self._tool().call({"ticker": "RKLB"})

    def test_days_over_cap_raises_without_calling_the_service(self):
        from .tools import _PRICE_HISTORY_TOOL_MAX_DAYS
        with patch("companies.services.technicals.get_price_history") as mock_history:
            with self.assertRaises(ToolError):
                self._tool().call({"ticker": "RKLB", "days": _PRICE_HISTORY_TOOL_MAX_DAYS + 1})
        mock_history.assert_not_called()

    def test_days_below_one_raises(self):
        with self.assertRaises(ToolError):
            self._tool().call({"ticker": "RKLB", "days": 0})


@override_settings(PRICING_PROVIDER="tradier", TRADIER_API_TOKEN="test-token")
class GetIntradayCandlesToolTests(TestCase):
    """The AI-facing get_intraday_candles tool (league/tools.py) -- 1/5/
    15-minute bars for the CURRENT session, via
    companies.services.tradier.get_intraday (itself unverified against a
    live Tradier response -- see that function's own docstring). Returns
    an envelope ({"bars": [...], "truncated": ...}), not a bare list, so a
    1min call that silently dropped most of the session is distinguishable
    from one that returned everything."""

    def setUp(self):
        self.competition = Competition.objects.create(
            name="Intraday Tool Test", starting_capital=Decimal("5000"),
            start_date=date(2026, 8, 12), end_date=date(2026, 11, 4),
        )
        self.competitor = Competitor.objects.create(
            competition=self.competition, name="Researcher", kind=Competitor.KIND_AI,
            cash_balance=Decimal("5000"),
        )

    def _tools(self):
        from .tools import build_league_tools
        return build_league_tools(self.competitor)

    def _tool(self):
        return {t.name: t for t in self._tools()}["get_intraday_candles"]

    def test_returns_bars_in_an_envelope(self):
        bars = [{"time": "2026-08-20T09:30:00", "open": 100, "high": 101, "low": 99.5, "close": 100.5, "volume": 1000}]
        with patch("companies.services.tradier.get_intraday", return_value=bars) as mock_intraday:
            result = json.loads(self._tool().call({"ticker": "rklb", "interval": "1min"}))
        self.assertEqual(result["bars"], bars)
        self.assertEqual(result["bars_returned"], 1)
        self.assertEqual(result["total_bars_this_session"], 1)
        self.assertFalse(result["truncated"])
        mock_intraday.assert_called_once_with("rklb", interval="1min")

    def test_no_bars_yet_returns_a_message(self):
        with patch("companies.services.tradier.get_intraday", return_value=[]):
            result = self._tool().call({"ticker": "RKLB"})
        self.assertIn("No intraday bars found", result)

    def test_unconfigured_tradier_raises_tool_error(self):
        with patch("companies.services.tradier.get_intraday", return_value=None):
            with self.assertRaises(ToolError):
                self._tool().call({"ticker": "RKLB"})

    def test_invalid_interval_raises_without_calling_tradier(self):
        with patch("companies.services.tradier.get_intraday") as mock_intraday:
            with self.assertRaises(ToolError):
                self._tool().call({"ticker": "RKLB", "interval": "1hour"})
        mock_intraday.assert_not_called()

    @override_settings(PRICING_PROVIDER="steady")
    def test_non_tradier_provider_is_not_even_registered(self):
        """build_league_tools() now filters this out entirely on a non-
        Tradier deployment (assistant/tools.py's build_tools() mirrors the
        same gate) -- offering a tool that would raise ToolError on every
        single call wastes a turn and system-prompt space for no benefit."""
        self.assertNotIn("get_intraday_candles", {t.name for t in self._tools()})

    @override_settings(TRADIER_API_TOKEN="")
    def test_tradier_provider_with_no_token_is_not_even_registered(self):
        self.assertNotIn("get_intraday_candles", {t.name for t in self._tools()})

    def test_truncates_to_the_most_recent_cap_bars_and_flags_it(self):
        from .tools import _INTRADAY_MAX_BARS
        bars = [{"time": f"{i:04d}", "close": i} for i in range(_INTRADAY_MAX_BARS + 10)]
        with patch("companies.services.tradier.get_intraday", return_value=bars):
            result = json.loads(self._tool().call({"ticker": "RKLB", "interval": "1min"}))
        self.assertEqual(len(result["bars"]), _INTRADAY_MAX_BARS)
        self.assertEqual(result["bars"][-1], bars[-1])  # most recent bars kept, not the earliest
        self.assertEqual(result["bars_returned"], _INTRADAY_MAX_BARS)
        self.assertEqual(result["total_bars_this_session"], _INTRADAY_MAX_BARS + 10)
        self.assertTrue(result["truncated"])


class TickerMemoryToolTests(TestCase):
    """The AI-facing list_ticker_memories/get_ticker_memories/
    set_ticker_memories tools (league/tools.py) -- per-ticker persistent
    research notes, AgentTickerMemory, deliberately separate from
    AgentScratchpad: pull-based (not auto-injected into every wake-up
    prompt) so it can scale across many tickers, one row per
    (competitor, ticker), overwritten wholesale on each set -- never
    appended."""

    def setUp(self):
        self.competition = Competition.objects.create(
            name="Ticker Memory Tool Test", starting_capital=Decimal("5000"),
            start_date=date(2026, 8, 12), end_date=date(2026, 11, 4),
        )
        self.competitor = Competitor.objects.create(
            competition=self.competition, name="Researcher", kind=Competitor.KIND_AI,
            cash_balance=Decimal("5000"),
        )

    def _tools(self):
        from .tools import build_league_tools
        return {t.name: t for t in build_league_tools(self.competitor)}

    def test_list_is_empty_with_nothing_saved(self):
        result = self._tools()["list_ticker_memories"].call({})
        self.assertIn("No ticker research notes saved yet", result)

    def test_set_then_list_then_get_round_trips(self):
        tools = self._tools()
        save = tools["set_ticker_memories"].call({"entries": [
            {"ticker": "aapl", "content": "Strong services growth, watching WWDC."},
            {"ticker": "tsla", "content": "Deliveries thesis intact."},
        ]})
        self.assertIn("AAPL", save)
        self.assertIn("TSLA", save)

        listed = json.loads(tools["list_ticker_memories"].call({}))
        self.assertEqual({row["ticker"] for row in listed}, {"AAPL", "TSLA"})
        self.assertEqual(listed[0]["length"], len("Strong services growth, watching WWDC."))

        pulled = json.loads(tools["get_ticker_memories"].call({"tickers": ["AAPL", "MSFT"]}))
        by_ticker = {row["ticker"]: row for row in pulled}
        self.assertEqual(by_ticker["AAPL"]["content"], "Strong services growth, watching WWDC.")
        self.assertIsNone(by_ticker["MSFT"]["content"])
        self.assertIsNone(by_ticker["MSFT"]["updated_at"])

    def test_set_overwrites_not_appends(self):
        tools = self._tools()
        tools["set_ticker_memories"].call({"entries": [{"ticker": "AAPL", "content": "First note."}]})
        tools["set_ticker_memories"].call({"entries": [{"ticker": "AAPL", "content": "Replacement note."}]})
        self.assertEqual(AgentTickerMemory.objects.filter(competitor=self.competitor, ticker="AAPL").count(), 1)
        pulled = json.loads(tools["get_ticker_memories"].call({"tickers": ["AAPL"]}))
        self.assertEqual(pulled[0]["content"], "Replacement note.")

    def test_set_is_scoped_per_competitor(self):
        other = Competitor.objects.create(
            competition=self.competition, name="Other", kind=Competitor.KIND_AI, cash_balance=Decimal("5000"),
        )
        from .tools import build_league_tools
        other_tools = {t.name: t for t in build_league_tools(other)}
        other_tools["set_ticker_memories"].call({"entries": [{"ticker": "AAPL", "content": "Other's note."}]})

        result = self._tools()["list_ticker_memories"].call({})
        self.assertIn("No ticker research notes saved yet", result)

    def test_set_empty_entries_raises(self):
        with self.assertRaises(ToolError):
            self._tools()["set_ticker_memories"].call({"entries": []})

    def test_set_missing_ticker_key_raises(self):
        with self.assertRaises(ToolError):
            self._tools()["set_ticker_memories"].call({"entries": [{"content": "no ticker here"}]})

    def test_get_empty_tickers_raises(self):
        with self.assertRaises(ToolError):
            self._tools()["get_ticker_memories"].call({"tickers": []})

    def test_get_over_batch_cap_raises(self):
        from .tools import _TICKER_MEMORY_BATCH_MAX
        with self.assertRaises(ToolError):
            self._tools()["get_ticker_memories"].call({"tickers": [f"T{i}" for i in range(_TICKER_MEMORY_BATCH_MAX + 1)]})

    def test_set_over_batch_cap_raises(self):
        from .tools import _TICKER_MEMORY_BATCH_MAX
        entries = [{"ticker": f"T{i}", "content": "note"} for i in range(_TICKER_MEMORY_BATCH_MAX + 1)]
        with self.assertRaises(ToolError):
            self._tools()["set_ticker_memories"].call({"entries": entries})


class ApiTests(TestCase):
    """Scoreboard/competitor/trade-submission endpoints (league/views.py) --
    the read side plus the human-competitor POST path, which reuses
    execution.submit_trade() directly (full parity with the AI submit_trade
    tool, no kind-specific code path)."""

    def setUp(self):
        self.competition = Competition.objects.create(
            name="API Test League", starting_capital=Decimal("5000"),
            start_date=date(2026, 8, 12), end_date=date(2026, 11, 4), index_tickers=["SPY"],
        )
        self.user = User.objects.create_user(email="apitest@example.com", password="testpass123")
        self.human = Competitor.objects.create(
            competition=self.competition, name="Human", kind=Competitor.KIND_HUMAN,
            user=self.user, cash_balance=Decimal("5000"),
        )
        self.ai = Competitor.objects.create(
            competition=self.competition, name="AI", kind=Competitor.KIND_AI, cash_balance=Decimal("5000"),
        )
        # This app authenticates via JWT (rest_framework_simplejwt), not
        # Django sessions -- force_login() sets a session cookie DRF's
        # JWTAuthentication never looks at, so requests would 401 despite
        # a "logged in" test client. A real access token on the
        # Authorization header is what actually satisfies it.
        access_token = RefreshToken.for_user(self.user).access_token
        self.client.defaults["HTTP_AUTHORIZATION"] = f"Bearer {access_token}"

    def test_scoreboard_returns_sorted_competitors(self):
        WeeklyMark.objects.create(competitor=self.human, week_ending=date(2026, 8, 15), account_value=Decimal("5200"))
        WeeklyMark.objects.create(competitor=self.human, week_ending=date(2026, 8, 22), account_value=Decimal("4900"))
        response = self.client.get(f"/api/league/competitions/{self.competition.id}/scoreboard/")
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(len(body["competitors"]), 2)
        human_row = next(r for r in body["competitors"] if r["name"] == "Human")
        self.assertEqual(human_row["account_value"], "5000.00")  # live value, no positions
        # peak 5200 -> trough 4900 = 5.77% drawdown
        self.assertAlmostEqual(float(human_row["max_drawdown_pct"]), 300 / 5200, places=4)

    def test_scoreboard_404_for_unknown_competition(self):
        response = self.client.get("/api/league/competitions/999999/scoreboard/")
        self.assertEqual(response.status_code, 404)

    def test_competitor_detail_includes_positions(self):
        position = Position.objects.create(
            competitor=self.human, instrument_type=Position.INSTRUMENT_STOCK,
            ticker="TEST", quantity=10, avg_cost=Decimal("100"),
        )
        valuation.save_quote_snapshot("TEST", _quote(ask=105, bid=100))
        response = self.client.get(f"/api/league/competitors/{self.human.id}/")
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(len(body["positions"]), 1)
        self.assertEqual(body["positions"][0]["ticker"], "TEST")
        # Regression: _position_row omitted "id" entirely until a frontend
        # building the price-targets UI needed it to construct
        # POST .../positions/{id}/price-targets/ and had nothing to use.
        self.assertEqual(body["positions"][0]["id"], position.id)

    def test_competitor_detail_never_calls_steady_live(self):
        """The actual fix for the 13-14s scoreboard/competitor-detail load
        times: this endpoint used to issue one live Steady call per open
        Position (worse, previously TWICE per position -- see git history),
        every single request. Reading TickerSnapshot instead means GET here
        makes zero live calls, however many positions are open, snapshot
        present or not (falls back to avg_cost -- see valuation.py)."""
        Position.objects.create(
            competitor=self.human, instrument_type=Position.INSTRUMENT_STOCK,
            ticker="AAA", quantity=10, avg_cost=Decimal("100"),
        )
        Position.objects.create(
            competitor=self.human, instrument_type=Position.INSTRUMENT_STOCK,
            ticker="BBB", quantity=5, avg_cost=Decimal("50"),
        )
        valuation.save_quote_snapshot("AAA", _quote(ask=105, bid=100))
        # BBB deliberately left with no snapshot -- falls back to avg_cost.
        with patch("companies.services.steady.get_quote") as mock_get_quote:
            response = self.client.get(f"/api/league/competitors/{self.human.id}/")
        self.assertEqual(response.status_code, 200)
        mock_get_quote.assert_not_called()

    def test_competitor_detail_includes_target_dead_price_and_priced_at(self):
        Position.objects.create(
            competitor=self.human, instrument_type=Position.INSTRUMENT_STOCK,
            ticker="TGT", quantity=10, avg_cost=Decimal("100"),
            target_price=Decimal("120"), dead_price=Decimal("90"),
        )
        valuation.save_quote_snapshot("TGT", _quote(ask=105, bid=100))
        response = self.client.get(f"/api/league/competitors/{self.human.id}/")
        self.assertEqual(response.status_code, 200)
        row = response.json()["positions"][0]
        self.assertEqual(row["target_price"], "120.0000")
        self.assertEqual(row["dead_price"], "90.0000")
        self.assertIsNotNone(row["priced_at"])

    def test_competitor_detail_omits_target_dead_price_when_unset(self):
        Position.objects.create(
            competitor=self.human, instrument_type=Position.INSTRUMENT_STOCK,
            ticker="NOTGT", quantity=10, avg_cost=Decimal("100"),
        )
        response = self.client.get(f"/api/league/competitors/{self.human.id}/")
        row = response.json()["positions"][0]
        self.assertIsNone(row["target_price"])
        self.assertIsNone(row["dead_price"])
        self.assertIsNone(row["priced_at"])  # no snapshot -> avg_cost fallback -> no priced_at either

    def test_competitor_detail_prices_as_of_is_the_stalest_snapshot(self):
        """prices_as_of should be the OLDEST priced_at among shown
        positions, not the newest -- it describes the least-fresh number
        actually contributing to the page, not the best case."""
        Position.objects.create(
            competitor=self.human, instrument_type=Position.INSTRUMENT_STOCK,
            ticker="OLD", quantity=1, avg_cost=Decimal("10"),
        )
        Position.objects.create(
            competitor=self.human, instrument_type=Position.INSTRUMENT_STOCK,
            ticker="NEW", quantity=1, avg_cost=Decimal("10"),
        )
        valuation.save_quote_snapshot("OLD", _quote(ask=11, bid=9))
        old_fetched_at = TickerSnapshot.objects.get(ticker="OLD").fetched_at
        TickerSnapshot.objects.filter(ticker="OLD").update(fetched_at=old_fetched_at - timedelta(minutes=20))
        valuation.save_quote_snapshot("NEW", _quote(ask=11, bid=9))

        response = self.client.get(f"/api/league/competitors/{self.human.id}/")
        body = response.json()
        old_row = next(r for r in body["positions"] if r["ticker"] == "OLD")
        self.assertEqual(body["prices_as_of"], old_row["priced_at"])

    def test_competitor_detail_prices_as_of_null_when_nothing_priced(self):
        Position.objects.create(
            competitor=self.human, instrument_type=Position.INSTRUMENT_STOCK,
            ticker="UNPRICED", quantity=1, avg_cost=Decimal("10"),
        )
        response = self.client.get(f"/api/league/competitors/{self.human.id}/")
        self.assertIsNone(response.json()["prices_as_of"])

    def test_competitor_detail_includes_model_provider_and_persona_for_ai(self):
        ai = Competitor.objects.create(
            competition=self.competition, name="Persona AI", kind=Competitor.KIND_AI,
            assistant_model=AssistantModel.get_default(), system_prompt="Be careful.",
            cash_balance=Decimal("5000"),
        )
        response = self.client.get(f"/api/league/competitors/{ai.id}/")
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["assistant_model"], AssistantModel.get_default().model_id)
        self.assertEqual(body["provider"], AssistantModel.PROVIDER_ANTHROPIC)
        self.assertEqual(body["system_prompt"], "Be careful.")

    def test_competitor_detail_omits_model_fields_for_human(self):
        response = self.client.get(f"/api/league/competitors/{self.human.id}/")
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertNotIn("assistant_model", body)
        self.assertNotIn("provider", body)
        self.assertNotIn("system_prompt", body)

    def test_human_can_submit_own_trade(self):
        with patch("companies.services.steady.get_quote", return_value=_quote(ask=100, bid=99)):
            response = self.client.post(f"/api/league/competitors/{self.human.id}/trades/", {
                "ticker": "TEST", "side": "buy", "instrument_type": "stock", "quantity": 5, "thesis": "api test",
            })
        self.assertEqual(response.status_code, 201)
        self.assertEqual(response.json()["status"], Trade.STATUS_FILLED)

    def test_human_trade_submission_returns_201_when_queued_after_hours(self):
        self.competition.restrict_after_hours_trading = True
        self.competition.save(update_fields=["restrict_after_hours_trading"])
        with patch("league.services.market_hours.is_market_open", return_value=False), \
             patch("companies.services.steady.get_quote", return_value=_quote(ask=100, bid=99)):
            response = self.client.post(f"/api/league/competitors/{self.human.id}/trades/", {
                "ticker": "TEST", "side": "buy", "instrument_type": "stock", "quantity": 5, "thesis": "queued test",
            })
        # PENDING is an accepted order, not a failure -- 201, not 400.
        self.assertEqual(response.status_code, 201)
        self.assertEqual(response.json()["status"], Trade.STATUS_PENDING)

    def test_human_can_cancel_own_pending_trade(self):
        self.competition.restrict_after_hours_trading = True
        self.competition.save(update_fields=["restrict_after_hours_trading"])
        with patch("league.services.market_hours.is_market_open", return_value=False), \
             patch("companies.services.steady.get_quote", return_value=_quote(ask=100, bid=99)):
            create_response = self.client.post(f"/api/league/competitors/{self.human.id}/trades/", {
                "ticker": "TEST", "side": "buy", "instrument_type": "stock", "quantity": 5, "thesis": "queued test",
            })
        trade_id = create_response.json()["id"]
        cancel_response = self.client.post(f"/api/league/competitors/{self.human.id}/trades/{trade_id}/cancel/")
        self.assertEqual(cancel_response.status_code, 200)
        self.assertEqual(cancel_response.json()["status"], Trade.STATUS_CANCELLED)

    def test_cancelling_an_already_filled_trade_returns_409_with_its_real_outcome(self):
        with patch("companies.services.steady.get_quote", return_value=_quote(ask=100, bid=99)):
            create_response = self.client.post(f"/api/league/competitors/{self.human.id}/trades/", {
                "ticker": "TEST", "side": "buy", "instrument_type": "stock", "quantity": 5, "thesis": "filled test",
            })
        trade_id = create_response.json()["id"]
        cancel_response = self.client.post(f"/api/league/competitors/{self.human.id}/trades/{trade_id}/cancel/")
        self.assertEqual(cancel_response.status_code, 409)
        body = cancel_response.json()
        # The trade's real, current outcome comes back in the 409 body --
        # not a bare error -- so the frontend can render it without a
        # second fetch.
        self.assertEqual(body["status"], Trade.STATUS_FILLED)
        self.assertEqual(body["fill_price"], "100.0000")

    def test_ai_competitors_trade_cannot_be_cancelled_via_human_endpoint(self):
        """Same restriction as CompetitorTradesView.post -- an AI only ever
        acts through its own cancel_trade tool, never this HTTP endpoint,
        even for the logged-in user's own AI competitor."""
        self.competition.restrict_after_hours_trading = True
        self.competition.save(update_fields=["restrict_after_hours_trading"])
        with patch("league.services.market_hours.is_market_open", return_value=False):
            pending = submit_trade(self.ai, "TEST", Trade.SIDE_BUY, Position.INSTRUMENT_STOCK, 5, "ai's own order")
        response = self.client.post(f"/api/league/competitors/{self.ai.id}/trades/{pending.id}/cancel/")
        self.assertEqual(response.status_code, 403)

    def test_human_trade_can_set_target_and_dead_price(self):
        with patch("companies.services.steady.get_quote", return_value=_quote(ask=100, bid=99)):
            response = self.client.post(f"/api/league/competitors/{self.human.id}/trades/", {
                "ticker": "TEST", "side": "buy", "instrument_type": "stock", "quantity": 5, "thesis": "api test",
                "target_price": "120", "dead_price": "90",
            })
        self.assertEqual(response.status_code, 201)
        position = Position.objects.get(competitor=self.human, ticker="TEST")
        self.assertEqual(position.target_price, Decimal("120"))
        self.assertEqual(position.dead_price, Decimal("90"))

    def test_human_cannot_submit_trade_for_ai_competitor(self):
        response = self.client.post(f"/api/league/competitors/{self.ai.id}/trades/", {
            "ticker": "TEST", "side": "buy", "instrument_type": "stock", "quantity": 5, "thesis": "should be denied",
        })
        self.assertEqual(response.status_code, 403)
        self.assertFalse(Trade.objects.filter(competitor=self.ai).exists())

    def test_human_cannot_submit_trade_for_other_humans_competitor(self):
        other_user = User.objects.create_user(email="other@example.com", password="testpass123")
        other_human = Competitor.objects.create(
            competition=self.competition, name="Other Human", kind=Competitor.KIND_HUMAN,
            user=other_user, cash_balance=Decimal("5000"),
        )
        response = self.client.post(f"/api/league/competitors/{other_human.id}/trades/", {
            "ticker": "TEST", "side": "buy", "instrument_type": "stock", "quantity": 5, "thesis": "should be denied",
        })
        self.assertEqual(response.status_code, 403)

    def test_rejected_trade_via_api_returns_400_with_reason(self):
        with patch("companies.services.steady.get_quote", return_value=_quote(ask=100, bid=99)):
            response = self.client.post(f"/api/league/competitors/{self.human.id}/trades/", {
                "ticker": "TEST", "side": "buy", "instrument_type": "stock", "quantity": 31, "thesis": "over cap",
            })
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["status"], Trade.STATUS_REJECTED)

    def test_trade_history_lists_past_trades(self):
        with patch("companies.services.steady.get_quote", return_value=_quote(ask=100, bid=99)):
            self.client.post(f"/api/league/competitors/{self.human.id}/trades/", {
                "ticker": "TEST", "side": "buy", "instrument_type": "stock", "quantity": 5, "thesis": "history test",
            })
        response = self.client.get(f"/api/league/competitors/{self.human.id}/trades/")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(response.json()), 1)

    def test_missing_required_field_400s(self):
        response = self.client.post(f"/api/league/competitors/{self.human.id}/trades/", {
            "ticker": "TEST", "side": "buy", "instrument_type": "stock",
        })
        self.assertEqual(response.status_code, 400)

    # --- PositionPriceTargetsView: revise target/dead on an already-open
    #     position without a new trade ---

    def test_set_price_targets_updates_an_existing_position(self):
        position = Position.objects.create(
            competitor=self.human, instrument_type=Position.INSTRUMENT_STOCK,
            ticker="REV", quantity=10, avg_cost=Decimal("100"), target_price=Decimal("120"),
        )
        response = self.client.post(
            f"/api/league/competitors/{self.human.id}/positions/{position.id}/price-targets/",
            {"target_price": "130", "dead_price": "95"},
        )
        self.assertEqual(response.status_code, 200)
        position.refresh_from_db()
        self.assertEqual(position.target_price, Decimal("130"))
        self.assertEqual(position.dead_price, Decimal("95"))

    def test_set_price_targets_can_clear_a_level_with_a_blank_value(self):
        position = Position.objects.create(
            competitor=self.human, instrument_type=Position.INSTRUMENT_STOCK,
            ticker="CLR", quantity=10, avg_cost=Decimal("100"), target_price=Decimal("120"),
        )
        response = self.client.post(
            f"/api/league/competitors/{self.human.id}/positions/{position.id}/price-targets/",
            {"target_price": ""},
        )
        self.assertEqual(response.status_code, 200)
        position.refresh_from_db()
        self.assertIsNone(position.target_price)

    def test_set_price_targets_requires_at_least_one_field(self):
        position = Position.objects.create(
            competitor=self.human, instrument_type=Position.INSTRUMENT_STOCK,
            ticker="EMPTY", quantity=10, avg_cost=Decimal("100"),
        )
        response = self.client.post(
            f"/api/league/competitors/{self.human.id}/positions/{position.id}/price-targets/", {},
        )
        self.assertEqual(response.status_code, 400)

    def test_set_price_targets_404_for_position_belonging_to_another_competitor(self):
        position = Position.objects.create(
            competitor=self.ai, instrument_type=Position.INSTRUMENT_STOCK,
            ticker="NOTMINE", quantity=10, avg_cost=Decimal("100"),
        )
        response = self.client.post(
            f"/api/league/competitors/{self.human.id}/positions/{position.id}/price-targets/",
            {"target_price": "50"},
        )
        self.assertEqual(response.status_code, 404)

    def test_set_price_targets_403_for_someone_elses_competitor(self):
        other_user = User.objects.create_user(email="other2@example.com", password="testpass123")
        other_human = Competitor.objects.create(
            competition=self.competition, name="Other Human 2", kind=Competitor.KIND_HUMAN,
            user=other_user, cash_balance=Decimal("5000"),
        )
        position = Position.objects.create(
            competitor=other_human, instrument_type=Position.INSTRUMENT_STOCK,
            ticker="THEIRS", quantity=10, avg_cost=Decimal("100"),
        )
        response = self.client.post(
            f"/api/league/competitors/{other_human.id}/positions/{position.id}/price-targets/",
            {"target_price": "50"},
        )
        self.assertEqual(response.status_code, 403)

    def test_competition_trade_feed_spans_every_competitor_newest_first(self):
        with patch("companies.services.steady.get_quote", return_value=_quote(ask=100, bid=99)):
            submit_trade(self.human, "AAA", Trade.SIDE_BUY, "stock", 1, "first")
            submit_trade(self.ai, "BBB", Trade.SIDE_BUY, "stock", 1, "second")
        response = self.client.get(f"/api/league/competitions/{self.competition.id}/trades/")
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual([row["ticker"] for row in body], ["BBB", "AAA"])
        self.assertEqual(body[0]["competitor_name"], "AI")
        self.assertEqual(body[0]["competitor_kind"], Competitor.KIND_AI)
        self.assertEqual(body[1]["competitor_name"], "Human")

    def test_competition_trade_feed_filters_by_competitor_kind(self):
        with patch("companies.services.steady.get_quote", return_value=_quote(ask=100, bid=99)):
            submit_trade(self.human, "AAA", Trade.SIDE_BUY, "stock", 1, "human trade")
            submit_trade(self.ai, "BBB", Trade.SIDE_BUY, "stock", 1, "ai trade")
        response = self.client.get(f"/api/league/competitions/{self.competition.id}/trades/?competitor_kind=ai")
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(len(body), 1)
        self.assertEqual(body[0]["ticker"], "BBB")

    def test_competition_trade_feed_filters_by_date_range(self):
        with patch("companies.services.steady.get_quote", return_value=_quote(ask=100, bid=99)):
            old_trade = submit_trade(self.human, "OLD", Trade.SIDE_BUY, "stock", 1, "old")
            Trade.objects.filter(id=old_trade.id).update(created_at=timezone.now() - timedelta(days=10))
            submit_trade(self.human, "NEW", Trade.SIDE_BUY, "stock", 1, "new")

        since = (timezone.now() - timedelta(days=1)).date().isoformat()
        response = self.client.get(f"/api/league/competitions/{self.competition.id}/trades/?since={since}")
        self.assertEqual(response.status_code, 200)
        tickers = [row["ticker"] for row in response.json()]
        self.assertIn("NEW", tickers)
        self.assertNotIn("OLD", tickers)

    def test_competition_trade_feed_404_for_unknown_competition(self):
        response = self.client.get("/api/league/competitions/999999/trades/")
        self.assertEqual(response.status_code, 404)

    def test_competition_trade_feed_bad_date_400s(self):
        response = self.client.get(f"/api/league/competitions/{self.competition.id}/trades/?since=not-a-date")
        self.assertEqual(response.status_code, 400)


class ManagementApiTests(TestCase):
    """The setup/management endpoints (league/views.py): create
    competitions and competitors, trigger an AI wake-up on demand, and
    read back its reasoning (turns/scratchpad) -- the surface a UI needs
    to actually run the league, not just watch the scoreboard."""

    def setUp(self):
        self.user = User.objects.create_user(email="manager@example.com", password="testpass123")
        access_token = RefreshToken.for_user(self.user).access_token
        self.client.defaults["HTTP_AUTHORIZATION"] = f"Bearer {access_token}"

    def _create_competition(self):
        # content_type="application/json" matters here: a plain dict POST
        # defaults to multipart encoding, which flattens a list value
        # (index_tickers) down to its last item -- a real frontend sending
        # JSON (the normal case for this API) doesn't hit that.
        response = self.client.post(
            "/api/league/competitions/",
            data=json.dumps({
                "name": "Mgmt Test League", "start_date": "2026-08-12", "end_date": "2026-11-04",
                "index_tickers": ["SPY", "VOO"],
            }),
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 201, response.content)
        return response.json()

    def test_league_models_list_includes_every_provider(self):
        response = self.client.get("/api/league/models/")
        self.assertEqual(response.status_code, 200)
        by_id = {m["model_id"]: m for m in response.json()}
        self.assertIn("claude-sonnet-5", by_id)
        self.assertIn("gpt-5.6-sol", by_id)
        self.assertEqual(by_id["gpt-5.6-sol"]["provider"], "openai")
        self.assertEqual(by_id["claude-sonnet-5"]["provider"], "anthropic")

    def test_create_ai_competitor_with_openai_model(self):
        competition = self._create_competition()
        response = self.client.post(
            f"/api/league/competitions/{competition['id']}/competitors/",
            {"name": "GPT Competitor", "kind": "ai", "model": "gpt-5.6-sol"},
        )
        self.assertEqual(response.status_code, 201, response.content)
        self.assertEqual(response.json()["assistant_model"], "gpt-5.6-sol")

    def test_create_and_list_competitions(self):
        created = self._create_competition()
        self.assertEqual(created["starting_capital"], "5000")
        self.assertEqual(created["index_tickers"], ["SPY", "VOO"])
        self.assertEqual(created["competitor_count"], 0)

        response = self.client.get("/api/league/competitions/")
        self.assertEqual(response.status_code, 200)
        self.assertIn(created["id"], [c["id"] for c in response.json()])

    def test_create_competition_missing_dates_400s(self):
        response = self.client.post("/api/league/competitions/", {"name": "No Dates"})
        self.assertEqual(response.status_code, 400)

    def test_create_competition_non_numeric_setting_400s_with_a_readable_error(self):
        response = self.client.post(
            "/api/league/competitions/",
            data=json.dumps({
                "name": "Bad Capital", "start_date": "2026-08-12", "end_date": "2026-11-04",
                "starting_capital": "lots",
            }),
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 400)
        self.assertEqual(
            response.json(), ["starting_capital, max_position_pct, max_index_pct and cash_apy must be numbers."],
        )

    def test_create_ai_competitor_defaults_model_and_cash(self):
        competition = self._create_competition()
        response = self.client.post(
            f"/api/league/competitions/{competition['id']}/competitors/",
            {"name": "Claude Regular", "kind": "ai", "system_prompt": "Be careful."},
        )
        self.assertEqual(response.status_code, 201, response.content)
        body = response.json()
        self.assertEqual(body["cash_balance"], "5000.00")
        self.assertEqual(body["assistant_model"], AssistantModel.get_default().model_id)
        self.assertEqual(body["system_prompt"], "Be careful.")
        self.assertEqual(body["user_email"], self.user.email)

    def test_create_human_competitor_attaches_requester(self):
        competition = self._create_competition()
        response = self.client.post(
            f"/api/league/competitions/{competition['id']}/competitors/",
            {"name": "Me", "kind": "human"},
        )
        self.assertEqual(response.status_code, 201, response.content)
        self.assertEqual(response.json()["user_email"], self.user.email)
        self.assertNotIn("assistant_model", response.json())


    def test_duplicate_competitor_name_in_same_competition_rejected(self):
        competition = self._create_competition()
        self.client.post(
            f"/api/league/competitions/{competition['id']}/competitors/", {"name": "Dup", "kind": "ai"},
        )
        response = self.client.post(
            f"/api/league/competitions/{competition['id']}/competitors/", {"name": "Dup", "kind": "ai"},
        )
        self.assertEqual(response.status_code, 400)

    def test_invalid_kind_rejected(self):
        competition = self._create_competition()
        response = self.client.post(
            f"/api/league/competitions/{competition['id']}/competitors/", {"name": "Bad", "kind": "robot"},
        )
        self.assertEqual(response.status_code, 400)

    def test_trigger_wakeup_dispatches_task_and_returns_202(self):
        competition = Competition.objects.create(
            name="Wakeup Test", starting_capital=Decimal("5000"),
            start_date=date(2026, 8, 12), end_date=date(2026, 11, 4),
        )
        ai = Competitor.objects.create(
            competition=competition, name="AI", kind=Competitor.KIND_AI, user=self.user,
            assistant_model=AssistantModel.get_default(), cash_balance=Decimal("5000"),
        )
        with patch("league.views.run_agent_wakeup.delay") as mock_delay:
            response = self.client.post(f"/api/league/competitors/{ai.id}/wakeups/", {"reason": "look at AAPL"})
        self.assertEqual(response.status_code, 202)
        self.assertEqual(AgentWakeup.objects.filter(competitor=ai).count(), 1)
        wakeup = AgentWakeup.objects.get(competitor=ai)
        self.assertEqual(wakeup.reason, "look at AAPL")
        mock_delay.assert_called_once_with(wakeup.id, AgentTurn.TRIGGER_MANUAL)

    def test_trigger_wakeup_rejected_for_human_competitor(self):
        competition = Competition.objects.create(
            name="Wakeup Test 2", starting_capital=Decimal("5000"),
            start_date=date(2026, 8, 12), end_date=date(2026, 11, 4),
        )
        human = Competitor.objects.create(
            competition=competition, name="Human", kind=Competitor.KIND_HUMAN,
            user=self.user, cash_balance=Decimal("5000"),
        )
        with patch("league.views.run_agent_wakeup.delay") as mock_delay:
            response = self.client.post(f"/api/league/competitors/{human.id}/wakeups/", {"reason": "n/a"})
        self.assertEqual(response.status_code, 400)
        mock_delay.assert_not_called()

    def test_trigger_wakeup_rejected_for_paused_competitor(self):
        competition = Competition.objects.create(
            name="Wakeup Test 3", starting_capital=Decimal("5000"),
            start_date=date(2026, 8, 12), end_date=date(2026, 11, 4),
        )
        ai = Competitor.objects.create(
            competition=competition, name="Paused AI", kind=Competitor.KIND_AI, user=self.user,
            assistant_model=AssistantModel.get_default(), cash_balance=Decimal("5000"), is_paused=True,
        )
        with patch("league.views.run_agent_wakeup.delay") as mock_delay:
            response = self.client.post(f"/api/league/competitors/{ai.id}/wakeups/", {"reason": "n/a"})
        self.assertEqual(response.status_code, 400)
        mock_delay.assert_not_called()

    def test_competitor_refresh_dispatches_task_and_returns_202(self):
        competition = Competition.objects.create(
            name="Refresh Test", starting_capital=Decimal("5000"),
            start_date=date(2026, 8, 12), end_date=date(2026, 11, 4),
        )
        competitor = Competitor.objects.create(
            competition=competition, name="Refreshee", kind=Competitor.KIND_HUMAN,
            user=self.user, cash_balance=Decimal("5000"),
        )
        with patch("league.views.refresh_competitor_quotes.delay") as mock_delay:
            response = self.client.post(f"/api/league/competitors/{competitor.id}/refresh/")
        self.assertEqual(response.status_code, 202)
        self.assertEqual(response.json(), {"status": "queued", "competitor_id": competitor.id})
        mock_delay.assert_called_once_with(competitor.id)

    def test_competitor_refresh_404_for_unknown_competitor(self):
        response = self.client.post("/api/league/competitors/999999/refresh/")
        self.assertEqual(response.status_code, 404)

    def test_competition_refresh_dispatches_task_and_returns_202(self):
        competition = Competition.objects.create(
            name="Competition Refresh Test", starting_capital=Decimal("5000"),
            start_date=date(2026, 8, 12), end_date=date(2026, 11, 4),
        )
        with patch("league.views.refresh_competition_quotes.delay") as mock_delay:
            response = self.client.post(f"/api/league/competitions/{competition.id}/refresh/")
        self.assertEqual(response.status_code, 202)
        self.assertEqual(response.json(), {"status": "queued", "competition_id": competition.id})
        mock_delay.assert_called_once_with(competition.id)

    def test_competition_refresh_404_for_unknown_competition(self):
        response = self.client.post("/api/league/competitions/999999/refresh/")
        self.assertEqual(response.status_code, 404)

    def test_list_wakeups(self):
        competition = Competition.objects.create(
            name="Wakeup List Test", starting_capital=Decimal("5000"),
            start_date=date(2026, 8, 12), end_date=date(2026, 11, 4),
        )
        ai = Competitor.objects.create(
            competition=competition, name="AI", kind=Competitor.KIND_AI, user=self.user,
            assistant_model=AssistantModel.get_default(), cash_balance=Decimal("5000"),
        )
        AgentWakeup.objects.create(competitor=ai, run_at=timezone.now(), reason="r1", status=AgentWakeup.STATUS_COMPLETED)
        response = self.client.get(f"/api/league/competitors/{ai.id}/wakeups/")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(response.json()), 1)

    def test_next_wakeups_includes_forced_checkins_and_pending_self_scheduled(self):
        competition = Competition.objects.create(
            name="Next Wakeups Test", starting_capital=Decimal("5000"),
            start_date=date(2026, 8, 12), end_date=date(2026, 11, 4),
        )
        ForcedCheckin.objects.create(
            competition=competition, hour=8, minute=0, label="8am news scan",
            prompt="Read overnight news.", inject_news=True,
        )
        ForcedCheckin.objects.create(
            competition=competition, hour=15, minute=15, label="pre-close",
            prompt="Decide if you want to adjust anything before the close.",
        )
        ai = Competitor.objects.create(
            competition=competition, name="AI", kind=Competitor.KIND_AI, user=self.user,
            assistant_model=AssistantModel.get_default(), cash_balance=Decimal("5000"),
        )
        # One pending self-scheduled wake-up, one already-completed one --
        # only the pending one should show up.
        pending = AgentWakeup.objects.create(
            competitor=ai, run_at=timezone.now() + timedelta(days=2), reason="check earnings",
            status=AgentWakeup.STATUS_SCHEDULED,
        )
        AgentWakeup.objects.create(
            competitor=ai, run_at=timezone.now() - timedelta(days=1), reason="already ran",
            status=AgentWakeup.STATUS_COMPLETED,
        )

        response = self.client.get(f"/api/league/competitors/{ai.id}/wakeups/next/")
        self.assertEqual(response.status_code, 200)
        body = response.json()

        self.assertEqual(len(body["forced_checkins"]), 2)
        labels = {f["label"] for f in body["forced_checkins"]}
        self.assertEqual(labels, {"8am news scan", "pre-close"})
        # sorted soonest-first
        run_ats = [f["next_run_at"] for f in body["forced_checkins"]]
        self.assertEqual(run_ats, sorted(run_ats))

        self.assertEqual(len(body["self_scheduled"]), 1)
        self.assertEqual(body["self_scheduled"][0]["id"], pending.id)
        self.assertEqual(body["self_scheduled"][0]["reason"], "check earnings")

    def test_next_wakeups_empty_for_human_competitor(self):
        competition = Competition.objects.create(
            name="Next Wakeups Human Test", starting_capital=Decimal("5000"),
            start_date=date(2026, 8, 12), end_date=date(2026, 11, 4),
        )
        human = Competitor.objects.create(
            competition=competition, name="Human", kind=Competitor.KIND_HUMAN,
            user=self.user, cash_balance=Decimal("5000"),
        )
        response = self.client.get(f"/api/league/competitors/{human.id}/wakeups/next/")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"forced_checkins": [], "self_scheduled": []})

    def test_turns_list_and_detail(self):
        competition = Competition.objects.create(
            name="Turns Test", starting_capital=Decimal("5000"),
            start_date=date(2026, 8, 12), end_date=date(2026, 11, 4),
        )
        ai = Competitor.objects.create(
            competition=competition, name="AI", kind=Competitor.KIND_AI, user=self.user,
            assistant_model=AssistantModel.get_default(), cash_balance=Decimal("5000"),
        )
        turn = AgentTurn.objects.create(
            competitor=ai, trigger=AgentTurn.TRIGGER_MANUAL,
            transcript=[{"role": "user", "text": "hi"}], input_tokens=10, output_tokens=5,
            cost_usd=Decimal("0.001"), tool_calls=["get_stock_quote"],
        )

        list_response = self.client.get(f"/api/league/competitors/{ai.id}/turns/")
        self.assertEqual(list_response.status_code, 200)
        self.assertEqual(len(list_response.json()), 1)
        self.assertNotIn("transcript", list_response.json()[0])  # summary only

        detail_response = self.client.get(f"/api/league/competitors/{ai.id}/turns/{turn.id}/")
        self.assertEqual(detail_response.status_code, 200)
        self.assertEqual(detail_response.json()["transcript"], [{"role": "user", "text": "hi"}])

    def test_turn_detail_404_for_wrong_competitor(self):
        competition = Competition.objects.create(
            name="Turns Test 2", starting_capital=Decimal("5000"),
            start_date=date(2026, 8, 12), end_date=date(2026, 11, 4),
        )
        ai1 = Competitor.objects.create(
            competition=competition, name="AI1", kind=Competitor.KIND_AI, user=self.user,
            assistant_model=AssistantModel.get_default(), cash_balance=Decimal("5000"),
        )
        ai2 = Competitor.objects.create(
            competition=competition, name="AI2", kind=Competitor.KIND_AI, user=self.user,
            assistant_model=AssistantModel.get_default(), cash_balance=Decimal("5000"),
        )
        turn = AgentTurn.objects.create(competitor=ai1, trigger=AgentTurn.TRIGGER_MANUAL, transcript=[])
        response = self.client.get(f"/api/league/competitors/{ai2.id}/turns/{turn.id}/")
        self.assertEqual(response.status_code, 404)

    def test_scratchpad_empty_when_none_saved(self):
        competition = Competition.objects.create(
            name="Scratchpad Test", starting_capital=Decimal("5000"),
            start_date=date(2026, 8, 12), end_date=date(2026, 11, 4),
        )
        ai = Competitor.objects.create(
            competition=competition, name="AI", kind=Competitor.KIND_AI, user=self.user,
            assistant_model=AssistantModel.get_default(), cash_balance=Decimal("5000"),
        )
        response = self.client.get(f"/api/league/competitors/{ai.id}/scratchpad/")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["content"], "")

    def test_scratchpad_returns_saved_content(self):
        competition = Competition.objects.create(
            name="Scratchpad Test 2", starting_capital=Decimal("5000"),
            start_date=date(2026, 8, 12), end_date=date(2026, 11, 4),
        )
        ai = Competitor.objects.create(
            competition=competition, name="AI", kind=Competitor.KIND_AI, user=self.user,
            assistant_model=AssistantModel.get_default(), cash_balance=Decimal("5000"),
        )
        AgentScratchpad.objects.create(competitor=ai, content="Watching SPY.")
        response = self.client.get(f"/api/league/competitors/{ai.id}/scratchpad/")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["content"], "Watching SPY.")

    def test_ticker_memories_empty_when_none_saved(self):
        competition = Competition.objects.create(
            name="Ticker Memories Test", starting_capital=Decimal("5000"),
            start_date=date(2026, 8, 12), end_date=date(2026, 11, 4),
        )
        ai = Competitor.objects.create(
            competition=competition, name="AI", kind=Competitor.KIND_AI, user=self.user,
            assistant_model=AssistantModel.get_default(), cash_balance=Decimal("5000"),
        )
        response = self.client.get(f"/api/league/competitors/{ai.id}/ticker-memories/")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), [])

    def test_ticker_memories_returns_saved_notes_most_recent_first(self):
        competition = Competition.objects.create(
            name="Ticker Memories Test 2", starting_capital=Decimal("5000"),
            start_date=date(2026, 8, 12), end_date=date(2026, 11, 4),
        )
        ai = Competitor.objects.create(
            competition=competition, name="AI", kind=Competitor.KIND_AI, user=self.user,
            assistant_model=AssistantModel.get_default(), cash_balance=Decimal("5000"),
        )
        rklb = AgentTickerMemory.objects.create(competitor=ai, ticker="RKLB", content="Backlog thesis.")
        spy = AgentTickerMemory.objects.create(competitor=ai, ticker="SPY", content="Hedge candidate.")
        # Explicit, well-separated timestamps rather than relying on
        # auto_now's real-clock ordering across two objects.create() calls
        # microseconds apart -- .update() bypasses auto_now, so this is the
        # only way to set updated_at deterministically after the fact.
        AgentTickerMemory.objects.filter(id=rklb.id).update(updated_at=timezone.now() - timedelta(days=1))
        AgentTickerMemory.objects.filter(id=spy.id).update(updated_at=timezone.now())

        response = self.client.get(f"/api/league/competitors/{ai.id}/ticker-memories/")
        self.assertEqual(response.status_code, 200)
        rows = response.json()
        self.assertEqual({r["ticker"] for r in rows}, {"RKLB", "SPY"})
        self.assertEqual(next(r for r in rows if r["ticker"] == "RKLB")["content"], "Backlog thesis.")
        self.assertEqual(rows[0]["ticker"], "SPY")  # most recently updated first

    def test_ticker_memories_404_for_unknown_competitor(self):
        response = self.client.get("/api/league/competitors/999999/ticker-memories/")
        self.assertEqual(response.status_code, 404)


class DailyBriefListViewTests(TestCase):
    """GET /api/league/briefs/ -- global (not competition-scoped), so no
    competition/competitor fixtures needed, just DailyBrief rows directly."""

    def setUp(self):
        self.user = User.objects.create_user(email="briefs@example.com", password="testpass123")
        access_token = RefreshToken.for_user(self.user).access_token
        self.client.defaults["HTTP_AUTHORIZATION"] = f"Bearer {access_token}"
        DailyBrief.objects.create(date=date(2026, 8, 18), slot="morning", content="Aug 18 morning brief")
        DailyBrief.objects.create(date=date(2026, 8, 19), slot="morning", content="Aug 19 morning brief")
        DailyBrief.objects.create(date=date(2026, 8, 19), slot="noon", content="Aug 19 noon brief")

    def test_lists_newest_first(self):
        response = self.client.get("/api/league/briefs/")
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(len(body), 3)
        self.assertEqual(body[0]["date"], "2026-08-19")

    def test_slot_filter_and_limit_give_the_latest_brief_for_one_slot(self):
        response = self.client.get("/api/league/briefs/?slot=morning&limit=1")
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(len(body), 1)
        self.assertEqual(body[0], {
            "id": body[0]["id"], "date": "2026-08-19", "slot": "morning",
            "content": "Aug 19 morning brief", "generated_at": body[0]["generated_at"],
        })

    def test_since_until_narrow_the_date_range(self):
        response = self.client.get("/api/league/briefs/?since=2026-08-19&until=2026-08-19")
        self.assertEqual(response.status_code, 200)
        dates = {row["date"] for row in response.json()}
        self.assertEqual(dates, {"2026-08-19"})

    def test_bad_since_400s(self):
        response = self.client.get("/api/league/briefs/?since=not-a-date")
        self.assertEqual(response.status_code, 400)

    def test_requires_authentication(self):
        self.client.defaults.pop("HTTP_AUTHORIZATION", None)
        response = self.client.get("/api/league/briefs/")
        self.assertEqual(response.status_code, 401)


# --- OpenAI (Responses API) provider path ---
#
# Fixtures use the real openai SDK's Response pydantic model
# (.model_validate) rather than MagicMock -- a bare mock would silently
# pass assertions a real payload (e.g. `arguments` as an actual JSON
# string) would fail on.

def _openai_response(
    output=None, status="completed", input_tokens=100, output_tokens=50,
    cached_tokens=0, cache_write_tokens=0, incomplete_details=None, id_="resp_1",
):
    data = {
        "id": id_, "created_at": 1234567890.0, "model": "gpt-5.6-sol", "object": "response",
        "output": output or [], "parallel_tool_calls": True, "tool_choice": "auto", "tools": [], "status": status,
        "usage": {
            "input_tokens": input_tokens, "output_tokens": output_tokens,
            "total_tokens": input_tokens + output_tokens,
            "input_tokens_details": {"cached_tokens": cached_tokens, "cache_write_tokens": cache_write_tokens},
            "output_tokens_details": {"reasoning_tokens": 0},
        },
    }
    if incomplete_details is not None:
        data["incomplete_details"] = incomplete_details
    return Response.model_validate(data)


def _message_item(text="All good, no trades."):
    return {
        "type": "message", "id": "msg_1", "role": "assistant", "status": "completed",
        "content": [{"type": "output_text", "text": text, "annotations": []}],
    }


def _function_call_item(name, arguments="{}", call_id="call_1"):
    return {"type": "function_call", "id": "fc_1", "call_id": call_id, "name": name, "arguments": arguments, "status": "completed"}


def _web_search_item():
    return {"type": "web_search_call", "id": "ws_1", "status": "completed", "action": {"type": "search", "query": "NVDA news"}}


class OpenAIToolDispatchTests(TestCase):
    """run_openai_tool -- dispatch by name into the same @beta_tool objects
    the Anthropic path uses, mirroring ExecutionTests' steady-mocking
    convention."""

    def setUp(self):
        self.competition = Competition.objects.create(
            name="OpenAI Tool Test", starting_capital=Decimal("5000"),
            start_date=date(2026, 8, 12), end_date=date(2026, 11, 4),
        )
        self.competitor = Competitor.objects.create(
            competition=self.competition, name="GPT", kind=Competitor.KIND_AI,
            cash_balance=Decimal("5000"),
        )

    def test_dispatches_real_business_logic(self):
        from .tools import build_league_tools
        tools = build_league_tools(self.competitor)
        with patch("companies.services.steady.get_quote", return_value=_quote(100, 99)):
            result = run_openai_tool(tools, "submit_trade", json.dumps({
                "ticker": "TEST", "side": "buy", "instrument_type": "stock",
                "quantity": 10, "thesis": "test thesis",
            }))
        self.assertTrue(result.startswith("FILLED"))
        self.assertEqual(Trade.objects.filter(competitor=self.competitor).count(), 1)

    def test_unknown_tool_returns_text_not_exception(self):
        result = run_openai_tool([], "not_a_real_tool", "{}")
        self.assertIn("Unknown tool", result)

    def test_tool_error_returns_text_not_exception(self):
        from .tools import build_league_tools
        tools = build_league_tools(self.competitor)
        # Missing required 'ticker' -- .call() raises ValueError, must not propagate.
        result = run_openai_tool(tools, "get_stock_quote", "{}")
        self.assertIsInstance(result, str)


class OpenAITurnLoopTests(TestCase):
    """llm_openai.run_openai_turn -- the hand-rolled Responses API loop
    (no tool_runner equivalent exists on the OpenAI SDK)."""

    def setUp(self):
        self.competition = Competition.objects.create(
            name="OpenAI Turn Test", starting_capital=Decimal("5000"),
            start_date=date(2026, 8, 12), end_date=date(2026, 11, 4),
        )
        self.gpt_model = AssistantModel.objects.create(
            model_id="test-gpt-flagship", display_name="Test GPT", provider=AssistantModel.PROVIDER_OPENAI,
            input_price_per_million=Decimal("5.00"), output_price_per_million=Decimal("30.00"),
        )
        self.competitor = Competitor.objects.create(
            competition=self.competition, name="GPT", kind=Competitor.KIND_AI,
            assistant_model=self.gpt_model, cash_balance=Decimal("5000"),
        )

    def test_tool_call_then_final_answer(self):
        """One round calls get_portfolio, the next has no tool calls -- loop
        should terminate after 2 API calls with the tool dispatched."""
        responses = [
            _openai_response(output=[_function_call_item("get_portfolio")]),
            _openai_response(output=[_message_item()], id_="resp_2"),
        ]
        with patch("league.services.llm_openai.OpenAI") as mock_openai_cls:
            mock_client = mock_openai_cls.return_value
            mock_client.responses.create.side_effect = responses
            result = llm_openai.run_openai_turn(self.competitor, "system prompt", "user content")

        self.assertEqual(mock_client.responses.create.call_count, 2)
        self.assertEqual(result.tool_calls, ["get_portfolio"])
        self.assertEqual(result.input_tokens, 200)   # 100 + 100
        self.assertEqual(result.output_tokens, 100)  # 50 + 50

    def test_web_search_call_logged_no_dispatch_needed(self):
        responses = [
            _openai_response(output=[_web_search_item(), _message_item()]),
        ]
        with patch("league.services.llm_openai.OpenAI") as mock_openai_cls:
            mock_client = mock_openai_cls.return_value
            mock_client.responses.create.side_effect = responses
            result = llm_openai.run_openai_turn(self.competitor, "system prompt", "user content")

        self.assertEqual(mock_client.responses.create.call_count, 1)
        self.assertEqual(result.tool_calls, ["web_search"])

    def test_loop_terminates_at_max_iterations(self):
        from .tools import LEAGUE_MAX_TOOL_ITERATIONS
        always_calling = _openai_response(output=[_function_call_item("get_portfolio")])
        with patch("league.services.llm_openai.OpenAI") as mock_openai_cls:
            mock_client = mock_openai_cls.return_value
            mock_client.responses.create.return_value = always_calling
            llm_openai.run_openai_turn(self.competitor, "system prompt", "user content")

        self.assertEqual(mock_client.responses.create.call_count, LEAGUE_MAX_TOOL_ITERATIONS)

    def test_unanswered_call_id_never_happens_on_dispatch_error(self):
        """An unknown/bad tool call must still get a function_call_output --
        an unanswered call_id would 400 the next Responses API call."""
        responses = [
            _openai_response(output=[_function_call_item("not_a_real_tool")]),
            _openai_response(output=[_message_item()], id_="resp_2"),
        ]
        with patch("league.services.llm_openai.OpenAI") as mock_openai_cls:
            mock_client = mock_openai_cls.return_value
            mock_client.responses.create.side_effect = responses
            llm_openai.run_openai_turn(self.competitor, "system prompt", "user content")

        second_call_kwargs = mock_client.responses.create.call_args_list[1].kwargs
        self.assertEqual(second_call_kwargs["input"][0]["type"], "function_call_output")
        self.assertIn("Unknown tool", second_call_kwargs["input"][0]["output"])

    def test_incomplete_status_stops_cleanly(self):
        responses = [_openai_response(status="incomplete", incomplete_details={"reason": "max_output_tokens"})]
        with patch("league.services.llm_openai.OpenAI") as mock_openai_cls:
            mock_client = mock_openai_cls.return_value
            mock_client.responses.create.side_effect = responses
            result = llm_openai.run_openai_turn(self.competitor, "system prompt", "user content")

        self.assertEqual(mock_client.responses.create.call_count, 1)
        self.assertEqual(result.output_tokens, 50)

    def test_missing_api_key_raises(self):
        with patch("league.services.llm_openai.settings.OPENAI_API_KEY", ""):
            with self.assertRaises(RuntimeError):
                llm_openai.run_openai_turn(self.competitor, "system prompt", "user content")


# --- Anthropic (hand-rolled, cache-control-breakpointed) provider path ---
#
# Fixtures use the real anthropic SDK's BetaMessage pydantic model
# (.model_validate), same reasoning as _openai_response above: a bare
# MagicMock would happily accept a malformed request/response shape a
# real payload wouldn't.

def _anthropic_message(content, stop_reason="end_turn", input_tokens=100, output_tokens=50,
                        cache_creation_input_tokens=0, cache_read_input_tokens=0, id_="msg_1"):
    return BetaMessage.model_validate({
        "id": id_, "type": "message", "role": "assistant", "model": "claude-sonnet-5",
        "content": content, "stop_reason": stop_reason, "stop_sequence": None,
        "usage": {
            "input_tokens": input_tokens, "output_tokens": output_tokens,
            "cache_creation_input_tokens": cache_creation_input_tokens,
            "cache_read_input_tokens": cache_read_input_tokens,
        },
    })


def _text_block(text="All good, no trades."):
    return {"type": "text", "text": text}


def _tool_use_block(name, input_=None, id_="toolu_1"):
    return {"type": "tool_use", "id": id_, "name": name, "input": input_ or {}}


class AnthropicTurnLoopTests(TestCase):
    """_run_anthropic_turn -- the hand-rolled anthropic.beta.messages.parse
    loop that replaced tool_runner so a prompt-cache breakpoint could move
    onto each round's tail message (tool_runner gives no hook for that
    between its internal append_messages and the next request)."""

    def setUp(self):
        self.competition = Competition.objects.create(
            name="Anthropic Turn Test", starting_capital=Decimal("5000"),
            start_date=date(2026, 8, 12), end_date=date(2026, 11, 4),
        )
        self.competitor = Competitor.objects.create(
            competition=self.competition, name="Claude", kind=Competitor.KIND_AI,
            assistant_model=AssistantModel.get_default(), cash_balance=Decimal("5000"),
        )

    def test_tool_call_then_final_answer(self):
        """One round calls get_portfolio, the next has no tool calls -- loop
        should terminate after 2 API calls with the tool dispatched."""
        responses = [
            _anthropic_message([_tool_use_block("get_portfolio")], stop_reason="tool_use"),
            _anthropic_message([_text_block()], id_="msg_2"),
        ]
        with patch("league.tasks.anthropic.Anthropic") as mock_anthropic_cls:
            mock_client = mock_anthropic_cls.return_value
            mock_client.beta.messages.parse.side_effect = responses
            result = _run_anthropic_turn(self.competitor, "system prompt", "user content")

        self.assertEqual(mock_client.beta.messages.parse.call_count, 2)
        self.assertEqual(result.tool_calls, ["get_portfolio"])
        self.assertEqual(result.input_tokens, 200)   # 100 + 100
        self.assertEqual(result.output_tokens, 100)  # 50 + 50

    def test_loop_terminates_at_max_iterations(self):
        from .tools import LEAGUE_MAX_TOOL_ITERATIONS
        always_calling = _anthropic_message([_tool_use_block("get_portfolio")], stop_reason="tool_use")
        with patch("league.tasks.anthropic.Anthropic") as mock_anthropic_cls:
            mock_client = mock_anthropic_cls.return_value
            mock_client.beta.messages.parse.return_value = always_calling
            _run_anthropic_turn(self.competitor, "system prompt", "user content")

        self.assertEqual(mock_client.beta.messages.parse.call_count, LEAGUE_MAX_TOOL_ITERATIONS)

    def test_unknown_tool_reports_error_not_exception(self):
        """A tool_use block naming a tool that doesn't exist must still get
        an answering tool_result -- an unanswered tool_use_id would 400 the
        next request, same failure mode as the OpenAI path's call_id case."""
        responses = [
            _anthropic_message([_tool_use_block("not_a_real_tool")], stop_reason="tool_use"),
            _anthropic_message([_text_block()], id_="msg_2"),
        ]
        with patch("league.tasks.anthropic.Anthropic") as mock_anthropic_cls:
            mock_client = mock_anthropic_cls.return_value
            mock_client.beta.messages.parse.side_effect = responses
            _run_anthropic_turn(self.competitor, "system prompt", "user content")

        second_call_kwargs = mock_client.beta.messages.parse.call_args_list[1].kwargs
        tool_result = second_call_kwargs["messages"][-1]["content"][0]
        self.assertEqual(tool_result["type"], "tool_result")
        self.assertTrue(tool_result["is_error"])
        self.assertIn("not found", tool_result["content"])

    def test_cache_control_breakpoints_on_system_tools_and_message_tail(self):
        """The whole point of the rewrite: every request must carry a
        breakpoint on system, on the last tool def, and on the last block
        of the last message -- otherwise nothing actually gets cached."""
        responses = [
            _anthropic_message([_tool_use_block("get_portfolio")], stop_reason="tool_use"),
            _anthropic_message([_text_block()], id_="msg_2"),
        ]
        with patch("league.tasks.anthropic.Anthropic") as mock_anthropic_cls:
            mock_client = mock_anthropic_cls.return_value
            mock_client.beta.messages.parse.side_effect = responses
            _run_anthropic_turn(self.competitor, "system prompt", "user content")

        for call in mock_client.beta.messages.parse.call_args_list:
            kwargs = call.kwargs
            self.assertEqual(kwargs["system"][-1]["cache_control"], {"type": "ephemeral"})
            self.assertEqual(kwargs["tools"][-1]["cache_control"], {"type": "ephemeral"})
            last_message_content = kwargs["messages"][-1]["content"]
            self.assertEqual(last_message_content[-1]["cache_control"], {"type": "ephemeral"})

    def test_stale_breakpoints_never_accumulate_across_rounds(self):
        """Only the CURRENT request's tail may carry a marker -- if an older
        one ever survived into a later request, a long enough turn would
        exceed Anthropic's 4-marked-blocks-per-request cap and start
        getting requests rejected outright. Three rounds (tool_use,
        tool_use, final text) so there's a round-1 tool_result message
        still sitting in the middle of round 3's request to check."""
        responses = [
            _anthropic_message([_tool_use_block("get_portfolio")], stop_reason="tool_use", id_="msg_1"),
            _anthropic_message([_tool_use_block("get_portfolio")], stop_reason="tool_use", id_="msg_2"),
            _anthropic_message([_text_block()], id_="msg_3"),
        ]
        with patch("league.tasks.anthropic.Anthropic") as mock_anthropic_cls:
            mock_client = mock_anthropic_cls.return_value
            mock_client.beta.messages.parse.side_effect = responses
            _run_anthropic_turn(self.competitor, "system prompt", "user content")

        third_call_messages = mock_client.beta.messages.parse.call_args_list[2].kwargs["messages"]
        # [user_text, assistant_1, tool_result_1, assistant_2, tool_result_2]
        self.assertEqual(len(third_call_messages), 5)
        initial_user_block = third_call_messages[0]["content"][-1]
        first_tool_result_block = third_call_messages[2]["content"][-1]
        second_tool_result_block = third_call_messages[4]["content"][-1]
        self.assertNotIn("cache_control", initial_user_block)
        self.assertNotIn("cache_control", first_tool_result_block)
        self.assertEqual(second_tool_result_block["cache_control"], {"type": "ephemeral"})

    def test_cache_tokens_flow_through_to_agent_turn_and_cost(self):
        response = _anthropic_message(
            [_text_block()], input_tokens=1000, output_tokens=200,
            cache_creation_input_tokens=500, cache_read_input_tokens=4000,
        )
        wakeup = AgentWakeup.objects.create(competitor=self.competitor, run_at=timezone.now(), reason="test")
        with patch("league.tasks.anthropic.Anthropic") as mock_anthropic_cls:
            mock_client = mock_anthropic_cls.return_value
            mock_client.beta.messages.parse.return_value = response
            turn = _run_wakeup(wakeup)

        self.assertEqual(turn.input_tokens, 1000)
        self.assertEqual(turn.cache_creation_input_tokens, 500)
        self.assertEqual(turn.cache_read_input_tokens, 4000)
        model = self.competitor.assistant_model
        expected_cost = (
            Decimal(1000) * model.input_price_per_million
            + Decimal(500) * model.input_price_per_million * Decimal("1.25")
            + Decimal(4000) * model.input_price_per_million * Decimal("0.1")
            + Decimal(200) * model.output_price_per_million
        ) / Decimal(1_000_000)
        self.assertEqual(turn.cost_usd, expected_cost)

    def test_missing_api_key_raises(self):
        with patch("league.tasks.settings.ANTHROPIC_API_KEY", ""):
            with self.assertRaises(RuntimeError):
                _run_anthropic_turn(self.competitor, "system prompt", "user content")


class RunWakeupProviderBranchTests(TestCase):
    """league.tasks._run_wakeup -- confirms the provider branch actually
    routes to the right implementation and the cache-adjusted cost math
    lands on AgentTurn correctly for every provider, without hitting a
    real API."""

    def setUp(self):
        self.competition = Competition.objects.create(
            name="Branch Test", starting_capital=Decimal("5000"),
            start_date=date(2026, 8, 12), end_date=date(2026, 11, 4),
        )
        self.gpt_model = AssistantModel.objects.create(
            model_id="test-gpt-flagship", display_name="Test GPT", provider=AssistantModel.PROVIDER_OPENAI,
            input_price_per_million=Decimal("5.00"), output_price_per_million=Decimal("30.00"),
        )
        self.gemini_model = AssistantModel.objects.create(
            model_id="test-gemini-flagship", display_name="Test Gemini", provider=AssistantModel.PROVIDER_GEMINI,
            input_price_per_million=Decimal("2.00"), output_price_per_million=Decimal("12.00"),
        )

    def test_openai_competitor_routes_through_responses_api(self):
        competitor = Competitor.objects.create(
            competition=self.competition, name="GPT", kind=Competitor.KIND_AI,
            assistant_model=self.gpt_model, cash_balance=Decimal("5000"),
        )
        wakeup = AgentWakeup.objects.create(competitor=competitor, run_at=timezone.now(), reason="test")
        response = _openai_response(
            output=[_message_item()], input_tokens=1000, output_tokens=200, cached_tokens=400,
        )
        with patch("league.services.llm_openai.OpenAI") as mock_openai_cls:
            mock_client = mock_openai_cls.return_value
            mock_client.responses.create.return_value = response
            turn = _run_wakeup(wakeup)

        self.assertEqual(turn.input_tokens, 1000)       # full raw count, for audit
        self.assertEqual(turn.cache_read_input_tokens, 400)
        # billed input = 1000 - 400 cached = 600, at $5/M; output 200 at $30/M
        expected_cost = (Decimal(600) * Decimal("5.00") + Decimal(200) * Decimal("30.00")) / Decimal(1_000_000)
        self.assertEqual(turn.cost_usd, expected_cost)

    def test_gemini_competitor_routes_through_generate_content(self):
        competitor = Competitor.objects.create(
            competition=self.competition, name="Gemini", kind=Competitor.KIND_AI,
            assistant_model=self.gemini_model, cash_balance=Decimal("5000"),
        )
        wakeup = AgentWakeup.objects.create(competitor=competitor, run_at=timezone.now(), reason="test")
        response = _gemini_response(
            parts=[{"text": "All good, no trades."}],
            input_tokens=1000, output_tokens=200, cached_tokens=400,
        )
        with patch("league.services.llm_gemini.genai.Client") as mock_client_cls:
            mock_client = mock_client_cls.return_value
            mock_client.models.generate_content.return_value = response
            turn = _run_wakeup(wakeup)

        self.assertEqual(turn.input_tokens, 1000)       # full raw count, for audit
        self.assertEqual(turn.cache_read_input_tokens, 400)
        # billed input = 1000 - 400 cached = 600, at $2/M; output 200 at $12/M
        expected_cost = (Decimal(600) * Decimal("2.00") + Decimal(200) * Decimal("12.00")) / Decimal(1_000_000)
        self.assertEqual(turn.cost_usd, expected_cost)

    def test_anthropic_competitor_routes_through_run_anthropic_turn(self):
        competitor = Competitor.objects.create(
            competition=self.competition, name="Claude", kind=Competitor.KIND_AI,
            assistant_model=AssistantModel.get_default(), cash_balance=Decimal("5000"),
        )
        wakeup = AgentWakeup.objects.create(competitor=competitor, run_at=timezone.now(), reason="test")
        with patch("league.tasks._run_anthropic_turn") as mock_anthropic_turn, \
             patch("league.services.llm_openai.OpenAI") as mock_openai_cls, \
             patch("league.services.llm_gemini.genai.Client") as mock_gemini_cls:
            from .services.llm_common import LLMTurnResult
            mock_anthropic_turn.return_value = LLMTurnResult(
                transcript=[], input_tokens=10, output_tokens=5,
                cache_creation_input_tokens=0, cache_read_input_tokens=0, tool_calls=[],
            )
            turn = _run_wakeup(wakeup)

        mock_anthropic_turn.assert_called_once()
        mock_openai_cls.assert_not_called()
        mock_gemini_cls.assert_not_called()
        self.assertEqual(turn.input_tokens, 10)


class WakeupPromptTickerMemoryIndexTests(TestCase):
    """_ticker_memory_index/_run_wakeup -- saved ticker research is
    pull-based (not dumped into the prompt like the scratchpad, see
    AgentTickerMemory's own docstring), but the TICKER LIST it's saved
    for still needs to reach the prompt, or the agent has no prompt-level
    reason to ever call list_ticker_memories/get_ticker_memories at all."""

    def setUp(self):
        self.competition = Competition.objects.create(
            name="Ticker Memory Prompt Test", starting_capital=Decimal("5000"),
            start_date=date(2026, 8, 12), end_date=date(2026, 11, 4),
        )
        self.competitor = Competitor.objects.create(
            competition=self.competition, name="Claude", kind=Competitor.KIND_AI,
            assistant_model=AssistantModel.get_default(), cash_balance=Decimal("5000"),
        )

    def _user_content(self):
        wakeup = AgentWakeup.objects.create(competitor=self.competitor, run_at=timezone.now(), reason="test")
        with patch("league.tasks._run_anthropic_turn") as mock_turn:
            from .services.llm_common import LLMTurnResult
            mock_turn.return_value = LLMTurnResult(
                transcript=[], input_tokens=10, output_tokens=5,
                cache_creation_input_tokens=0, cache_read_input_tokens=0, tool_calls=[],
            )
            _run_wakeup(wakeup)
        return mock_turn.call_args.args[2]

    def test_no_saved_tickers_shows_none_yet(self):
        self.assertIn("(none yet)", self._user_content())

    def test_saved_tickers_are_listed_not_their_content(self):
        AgentTickerMemory.objects.create(competitor=self.competitor, ticker="AAPL", content="Secret thesis text.")
        AgentTickerMemory.objects.create(competitor=self.competitor, ticker="TSLA", content="Another thesis.")
        user_content = self._user_content()
        self.assertIn("AAPL", user_content)
        self.assertIn("TSLA", user_content)
        self.assertNotIn("Secret thesis text.", user_content)


# --- Gemini (google-genai) provider path ---

def _gemini_response(parts=None, finish_reason="STOP", input_tokens=100, output_tokens=50, cached_tokens=0, thoughts_tokens=0, grounding_metadata=None):
    candidate = {"content": {"role": "model", "parts": parts or []}, "finish_reason": finish_reason}
    if grounding_metadata is not None:
        candidate["grounding_metadata"] = grounding_metadata
    return genai_types.GenerateContentResponse.model_validate({
        "candidates": [candidate],
        "usage_metadata": {
            "prompt_token_count": input_tokens, "candidates_token_count": output_tokens,
            "total_token_count": input_tokens + output_tokens,
            "cached_content_token_count": cached_tokens, "thoughts_token_count": thoughts_tokens,
        },
    })


class GeminiToolDispatchTests(TestCase):
    """run_gemini_tool -- dispatch by name into the same @beta_tool objects
    the Anthropic/OpenAI paths use."""

    def setUp(self):
        self.competition = Competition.objects.create(
            name="Gemini Tool Test", starting_capital=Decimal("5000"),
            start_date=date(2026, 8, 12), end_date=date(2026, 11, 4),
        )
        self.competitor = Competitor.objects.create(
            competition=self.competition, name="Gemini", kind=Competitor.KIND_AI,
            cash_balance=Decimal("5000"),
        )

    def test_dispatches_real_business_logic(self):
        from .tools import build_league_tools
        tools = build_league_tools(self.competitor)
        with patch("companies.services.steady.get_quote", return_value=_quote(100, 99)):
            result = run_gemini_tool(tools, "submit_trade", {
                "ticker": "TEST", "side": "buy", "instrument_type": "stock",
                "quantity": 10, "thesis": "test thesis",
            })
        self.assertTrue(result.startswith("FILLED"))
        self.assertEqual(Trade.objects.filter(competitor=self.competitor).count(), 1)

    def test_unknown_tool_returns_text_not_exception(self):
        result = run_gemini_tool([], "not_a_real_tool", {})
        self.assertIn("Unknown tool", result)

    def test_tool_error_returns_text_not_exception(self):
        from .tools import build_league_tools
        tools = build_league_tools(self.competitor)
        result = run_gemini_tool(tools, "get_stock_quote", {})  # missing required 'ticker'
        self.assertIsInstance(result, str)


class GeminiTurnLoopTests(TestCase):
    """llm_gemini.run_gemini_turn -- the hand-rolled generate_content loop
    (automatic_function_calling deliberately disabled, see that module's
    docstring)."""

    def setUp(self):
        self.competition = Competition.objects.create(
            name="Gemini Turn Test", starting_capital=Decimal("5000"),
            start_date=date(2026, 8, 12), end_date=date(2026, 11, 4),
        )
        self.gemini_model = AssistantModel.objects.create(
            model_id="test-gemini-flagship", display_name="Test Gemini", provider=AssistantModel.PROVIDER_GEMINI,
            input_price_per_million=Decimal("2.00"), output_price_per_million=Decimal("12.00"),
        )
        self.competitor = Competitor.objects.create(
            competition=self.competition, name="Gemini", kind=Competitor.KIND_AI,
            assistant_model=self.gemini_model, cash_balance=Decimal("5000"),
        )

    def test_tool_call_then_final_answer(self):
        responses = [
            _gemini_response(parts=[{"function_call": {"name": "get_portfolio", "args": {}}}]),
            _gemini_response(parts=[{"text": "All good."}]),
        ]
        with patch("league.services.llm_gemini.genai.Client") as mock_client_cls:
            mock_client = mock_client_cls.return_value
            mock_client.models.generate_content.side_effect = responses
            result = llm_gemini.run_gemini_turn(self.competitor, "system prompt", "user content")

        self.assertEqual(mock_client.models.generate_content.call_count, 2)
        self.assertEqual(result.tool_calls, ["get_portfolio"])
        self.assertEqual(result.input_tokens, 200)   # 100 + 100
        self.assertEqual(result.output_tokens, 100)  # 50 + 50

    def test_thinking_tokens_folded_into_output(self):
        responses = [_gemini_response(parts=[{"text": "ok"}], output_tokens=50, thoughts_tokens=30)]
        with patch("league.services.llm_gemini.genai.Client") as mock_client_cls:
            mock_client = mock_client_cls.return_value
            mock_client.models.generate_content.side_effect = responses
            result = llm_gemini.run_gemini_turn(self.competitor, "system prompt", "user content")

        self.assertEqual(result.output_tokens, 80)  # 50 candidates + 30 thoughts

    def test_grounding_logged_and_no_dispatch_needed(self):
        responses = [
            _gemini_response(
                parts=[{"text": "Per search, NVDA is up."}],
                grounding_metadata={"web_search_queries": ["NVDA news today"]},
            ),
        ]
        with patch("league.services.llm_gemini.genai.Client") as mock_client_cls:
            mock_client = mock_client_cls.return_value
            mock_client.models.generate_content.side_effect = responses
            result = llm_gemini.run_gemini_turn(self.competitor, "system prompt", "user content")

        self.assertEqual(mock_client.models.generate_content.call_count, 1)
        self.assertEqual(result.tool_calls, ["google_search"])
        grounding_entries = [e for e in result.transcript if e.get("type") == "grounding_metadata"]
        self.assertEqual(len(grounding_entries), 1)

    def test_loop_terminates_at_max_iterations(self):
        from .tools import LEAGUE_MAX_TOOL_ITERATIONS
        always_calling = _gemini_response(parts=[{"function_call": {"name": "get_portfolio", "args": {}}}])
        with patch("league.services.llm_gemini.genai.Client") as mock_client_cls:
            mock_client = mock_client_cls.return_value
            mock_client.models.generate_content.return_value = always_calling
            llm_gemini.run_gemini_turn(self.competitor, "system prompt", "user content")

        self.assertEqual(mock_client.models.generate_content.call_count, LEAGUE_MAX_TOOL_ITERATIONS)

    def test_unanswered_call_never_happens_on_dispatch_error(self):
        # `contents` is one list mutated in place across the whole loop --
        # Mock.call_args_list stores a reference to it, not a snapshot, so
        # inspecting it after the loop ends would show its FINAL state for
        # every past call, not what was actually sent at call time. Capture
        # a shallow copy ourselves at each call instead.
        responses = [
            _gemini_response(parts=[{"function_call": {"name": "not_a_real_tool", "args": {}}}]),
            _gemini_response(parts=[{"text": "ok"}]),
        ]
        captured_contents = []

        def fake_generate_content(**kwargs):
            captured_contents.append(list(kwargs["contents"]))
            return responses[len(captured_contents) - 1]

        with patch("league.services.llm_gemini.genai.Client") as mock_client_cls:
            mock_client = mock_client_cls.return_value
            mock_client.models.generate_content.side_effect = fake_generate_content
            llm_gemini.run_gemini_turn(self.competitor, "system prompt", "user content")

        tool_content = captured_contents[1][-1]
        self.assertEqual(tool_content.role, "tool")
        self.assertIn("Unknown tool", tool_content.parts[0].function_response.response["result"])

    def test_max_tokens_finish_reason_stops_cleanly(self):
        responses = [_gemini_response(parts=[], finish_reason="MAX_TOKENS")]
        with patch("league.services.llm_gemini.genai.Client") as mock_client_cls:
            mock_client = mock_client_cls.return_value
            mock_client.models.generate_content.side_effect = responses
            result = llm_gemini.run_gemini_turn(self.competitor, "system prompt", "user content")

        self.assertEqual(mock_client.models.generate_content.call_count, 1)
        self.assertEqual(result.output_tokens, 50)

    def test_missing_api_key_raises(self):
        with patch("league.services.llm_gemini.settings.GEMINI_API_KEY", ""):
            with self.assertRaises(RuntimeError):
                llm_gemini.run_gemini_turn(self.competitor, "system prompt", "user content")

    def test_include_server_side_tool_invocations_is_set(self):
        """Regression test for a real live failure: combining google_search
        with function-calling tools 400s ("Please enable
        tool_config.include_server_side_tool_invocations...") unless this
        flag is set -- not documented anywhere found beforehand, only
        caught by actually triggering a live wake-up."""
        response = _gemini_response(parts=[{"text": "ok"}])
        with patch("league.services.llm_gemini.genai.Client") as mock_client_cls:
            mock_client = mock_client_cls.return_value
            mock_client.models.generate_content.return_value = response
            llm_gemini.run_gemini_turn(self.competitor, "system prompt", "user content")

        config = mock_client.models.generate_content.call_args.kwargs["config"]
        self.assertTrue(config.tool_config.include_server_side_tool_invocations)

    def test_thought_signature_bytes_saves_to_agentturn(self):
        """Regression test for a second real live failure, right after the
        first fix: a thinking-capable turn's parts can carry
        thought_signature as raw `bytes` (confirmed live -- 3x 200 OK from
        Gemini, then AgentTurn.objects.create() itself threw "Object of
        type bytes is not JSON serializable", since transcript is a
        JSONField). Exercises the full _run_wakeup path, including the
        actual DB write, since that's the layer that broke in production
        -- run_gemini_turn's return value alone wouldn't have caught it."""
        competitor = Competitor.objects.create(
            competition=self.competition, name="Gemini Thinker", kind=Competitor.KIND_AI,
            assistant_model=self.gemini_model, cash_balance=Decimal("5000"),
        )
        wakeup = AgentWakeup.objects.create(competitor=competitor, run_at=timezone.now(), reason="test")
        signature = base64.b64encode(b"opaque-thought-signature").decode()
        response = _gemini_response(parts=[
            {"text": "Let me think about this.", "thought_signature": signature},
            {"text": "All good, no trades."},
        ])
        with patch("league.services.llm_gemini.genai.Client") as mock_client_cls:
            mock_client = mock_client_cls.return_value
            mock_client.models.generate_content.return_value = response
            turn = _run_wakeup(wakeup)  # would raise TypeError pre-fix

        self.assertTrue(AgentTurn.objects.filter(id=turn.id).exists())
