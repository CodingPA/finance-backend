"""Poll the active pricing provider's (settings.PRICING_PROVIDER --
SteadyAPI or Tradier) live quote (stock) or options chain (one contract)
for a ticker on a fixed interval and print it -- a manual eyeball check
against a real brokerage (e.g. "does this track what Fidelity shows me
right now") rather than an automated test. Ctrl+C to stop.

Always live: stock mode calls valuation.get_stock_quote(), option mode
calls pricing.get_options_chain() -- the exact same calls execution.py's
trade fills use for actual pricing -- never TickerSnapshot, so this is
never up to ~10 minutes stale the way the scoreboard/competitor-detail
display path can be (see league/services/valuation.py's module
docstring). If what prints here doesn't match Fidelity, that's a real
provider data issue, not a caching artifact.

Examples:
  python manage.py watch_quote AAPL
  python manage.py watch_quote AAPL --interval 30
  python manage.py watch_quote AAPL --strike 230 --expiration 2026-09-19 --type call
"""

import time
from datetime import datetime
from decimal import Decimal, InvalidOperation

from django.core.management.base import BaseCommand, CommandError

from companies.services import pricing
from league.services import valuation


class Command(BaseCommand):
    help = (
        "Poll a ticker's live quote from the active pricing provider (or, with --strike/--expiration, "
        "one option contract's chain entry) every N seconds and print it, for comparing against a real brokerage."
    )

    def add_arguments(self, parser):
        parser.add_argument("ticker", type=str, help="Ticker to watch, e.g. AAPL.")
        parser.add_argument(
            "--interval", type=float, default=60,
            help="Seconds between polls (default: 60).",
        )
        parser.add_argument(
            "--count", type=int, default=None,
            help="Stop after this many polls instead of running until Ctrl+C.",
        )
        parser.add_argument(
            "--strike", type=str, default=None,
            help="Option strike price, e.g. 230. Combine with --expiration to watch an option "
                 "contract instead of the underlying stock/ETF quote.",
        )
        parser.add_argument(
            "--expiration", type=str, default=None,
            help="Option expiration date, YYYY-MM-DD. Required together with --strike.",
        )
        parser.add_argument(
            "--type", choices=["call", "put"], default="call",
            help="Option type -- only used with --strike/--expiration (default: call).",
        )

    def handle(self, *args, **options):
        ticker = options["ticker"].strip().upper()
        if not ticker:
            raise CommandError("ticker is required.")
        interval = options["interval"]
        count = options["count"]

        strike_raw = options["strike"]
        expiration = options["expiration"]
        if bool(strike_raw) != bool(expiration):
            raise CommandError("--strike and --expiration must be given together.")

        if strike_raw is not None:
            try:
                strike = Decimal(strike_raw)
            except InvalidOperation:
                raise CommandError(f"Bad --strike {strike_raw!r} -- must be a number.")
            option_type = options["type"]
            label = f"{ticker} {strike}{'C' if option_type == 'call' else 'P'} exp {expiration}"
            header = f"{'time':<10} {'last':>10} {'bid':>10} {'ask':>10} {'chg':>9} {'chg%':>8}  iv%"
            poll = lambda: self._poll_option(ticker, strike, expiration, option_type)  # noqa: E731
        else:
            label = ticker
            header = f"{'time':<10} {'last':>10} {'bid':>10} {'ask':>10} {'chg':>9} {'chg%':>8}  status"
            poll = lambda: self._poll_stock(ticker)  # noqa: E731

        self.stdout.write(f"Watching {label} every {interval:.0f}s" + (f" ({count} polls)" if count else "") + " -- Ctrl+C to stop.\n")
        self.stdout.write(header)

        polls = 0
        try:
            while count is None or polls < count:
                poll()
                polls += 1
                if count is None or polls < count:
                    time.sleep(interval)
        except KeyboardInterrupt:
            self.stdout.write("\nStopped.")

    def _poll_stock(self, ticker: str) -> None:
        now = datetime.now().strftime("%H:%M:%S")
        try:
            quote = valuation.get_stock_quote(ticker)
        except Exception as exc:
            self.stderr.write(f"{now:<10} ERROR: {exc}")
            return

        if quote is None:
            self.stderr.write(f"{now:<10} No quote returned for {ticker} -- check the symbol or the pricing provider's API token.")
            return

        self.stdout.write(
            f"{now:<10} {self._fmt(quote.get('last_price')):>10} {self._fmt(quote.get('bid')):>10} "
            f"{self._fmt(quote.get('ask')):>10} {self._fmt(quote.get('net_change')):>9} "
            f"{self._fmt(quote.get('percent_change_pct')):>7}%  {quote.get('market_status')}"
        )

    def _poll_option(self, ticker: str, strike: Decimal, expiration: str, option_type: str) -> None:
        now = datetime.now().strftime("%H:%M:%S")
        try:
            chain = pricing.get_options_chain(ticker, expiration=expiration)
        except Exception as exc:
            self.stderr.write(f"{now:<10} ERROR: {exc}")
            return

        if chain is None:
            self.stderr.write(f"{now:<10} No options chain returned for {ticker} -- check the pricing provider's API token.")
            return

        contracts = chain["calls"] if option_type == "call" else chain["puts"]
        contract = next(
            (c for c in contracts if c["strike_price"] is not None and Decimal(str(c["strike_price"])) == strike),
            None,
        )
        if contract is None:
            self.stderr.write(
                f"{now:<10} No {strike} {option_type} found for {ticker} exp {expiration} "
                f"-- check strike/expiration/availability (available expirations: {chain.get('expirations')})."
            )
            return

        self.stdout.write(
            f"{now:<10} {self._fmt(contract.get('last')):>10} {self._fmt(contract.get('bid')):>10} "
            f"{self._fmt(contract.get('ask')):>10} {self._fmt(contract.get('price_change')):>9} "
            f"{self._fmt(contract.get('percent_change_pct')):>7}%  {self._fmt(contract.get('implied_volatility_pct'))}"
        )

    @staticmethod
    def _fmt(value) -> str:
        if value is None:
            return "--"
        return f"{value:.2f}" if isinstance(value, (int, float)) else str(value)
