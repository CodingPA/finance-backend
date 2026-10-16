"""Poll Tradier's live quote plus the closest in-the-money call and the
put at that same strike (nearest Friday expiration) for a ticker, once
every N seconds, one line per poll -- built to eyeball whether Tradier's
options pricing actually moves in real time, unlike SteadyAPI's
~10-minute-stale chain. Ctrl+C to stop, or --count N to stop automatically.

The expiration (and which strike counts as "closest ITM") is picked once
at startup from the expirations list (today counts as "the following
Friday" if run on a Friday itself). Spot price and the option chain are
re-fetched fresh on every poll, and the ITM strike is recomputed each
time in case the underlying trades across a strike boundary during the
session -- so the printed strike can change between polls, which is
correct, not a bug.

Needs a *production* TRADIER_API_TOKEN (see .env.example) -- Tradier's
sandbox serves delayed data, so polling it won't show real movement; the
command warns if TRADIER_BASE_URL points at sandbox.

Examples:
  python manage.py tradier_itm_quote AAPL
  python manage.py tradier_itm_quote AAPL --interval 10
  python manage.py tradier_itm_quote AAPL --count 20
"""

import time
from datetime import date, datetime, timedelta

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

from companies.services import tradier


class Command(BaseCommand):
    help = (
        "Poll a ticker's live Tradier quote plus the closest ITM call and same-strike put "
        "(nearest Friday expiration) every N seconds, one line per poll, until Ctrl+C."
    )

    def add_arguments(self, parser):
        parser.add_argument("ticker", type=str, help="Ticker to watch, e.g. AAPL.")
        parser.add_argument(
            "--interval", type=float, default=5,
            help="Seconds between polls (default: 5).",
        )
        parser.add_argument(
            "--count", type=int, default=None,
            help="Stop after this many polls instead of running until Ctrl+C.",
        )

    def handle(self, *args, **options):
        ticker = options["ticker"].strip().upper()
        if not ticker:
            raise CommandError("ticker is required.")
        interval = options["interval"]
        count = options["count"]

        if not settings.TRADIER_API_TOKEN:
            raise CommandError("TRADIER_API_TOKEN is not set -- see .env.example.")
        if "sandbox" in settings.TRADIER_BASE_URL:
            self.stdout.write(self.style.WARNING(
                "TRADIER_BASE_URL points at sandbox.tradier.com -- sandbox data is DELAYED, "
                "polling it won't show real-time movement.\n"
            ))

        try:
            expirations = tradier.get_expirations(ticker)
        except Exception as exc:
            raise CommandError(str(exc))
        if not expirations:
            raise CommandError(f"Tradier has no listed options for {ticker}.")

        today = date.today()
        target_friday = today + timedelta(days=(4 - today.weekday()) % 7)
        target_str = target_friday.isoformat()
        if target_str in expirations:
            expiration = target_str
        else:
            expiration = min(expirations, key=lambda d: abs((date.fromisoformat(d) - target_friday).days))
            self.stdout.write(self.style.WARNING(
                f"No exact {target_str} (Friday) listing for {ticker} -- using nearest listed expiration "
                f"{expiration} instead.\n"
            ))

        self.stdout.write(
            f"Watching {ticker} exp {expiration} every {interval:.0f}s" +
            (f" ({count} polls)" if count else "") + " -- Ctrl+C to stop.\n"
        )
        self.stdout.write(f"{'time':<10} {'ticker':<7} {'spot':>9}  {'exp':<12} {'strike':>8} {'call':>8} {'put':>8}")

        polls = 0
        try:
            while count is None or polls < count:
                self._poll(ticker, expiration)
                polls += 1
                if count is None or polls < count:
                    time.sleep(interval)
        except KeyboardInterrupt:
            self.stdout.write("\nStopped.")

    def _poll(self, ticker: str, expiration: str) -> None:
        now = datetime.now().strftime("%H:%M:%S")
        try:
            quote = tradier.get_quote(ticker)
            chain = tradier.get_options_chain(ticker, expiration)
        except Exception as exc:
            self.stderr.write(f"{now:<10} ERROR: {exc}")
            return

        # Spot prefers `last` (the actual last trade -- what Fidelity/Tradier's
        # own quote screen show as "the price") over bid/ask midpoint. Options
        # below still use _mid_or_last: a contract's `last` print can be
        # stale for minutes/hours between trades even while the market is
        # open, so its bid/ask midpoint is the more meaningful "live" number.
        spot = quote.get("last")
        if spot is None:
            spot = self._mid_or_last(quote)
        if spot is None:
            spot = quote.get("close")
        if spot is None or not chain:
            self.stderr.write(f"{now:<10} No usable quote/chain for {ticker}.")
            return

        calls = [c for c in chain if c.get("option_type") == "call" and c.get("strike") is not None]
        puts_by_strike = {p["strike"]: p for p in chain if p.get("option_type") == "put" and p.get("strike") is not None}
        if not calls:
            self.stderr.write(f"{now:<10} No call contracts for {ticker} exp {expiration}.")
            return

        itm_calls = [c for c in calls if c["strike"] < spot]
        call = max(itm_calls, key=lambda c: c["strike"]) if itm_calls else min(calls, key=lambda c: c["strike"])
        strike = call["strike"]
        put = puts_by_strike.get(strike)

        call_price = self._mid_or_last(call)
        put_price = self._mid_or_last(put) if put else None

        self.stdout.write(
            f"{now:<10} {ticker:<7} {self._fmt(spot):>9}  {expiration:<12} {strike:>8g} "
            f"{self._fmt(call_price):>8} {self._fmt(put_price):>8}"
        )

    @staticmethod
    def _mid_or_last(quoted: dict):
        bid, ask, last = quoted.get("bid"), quoted.get("ask"), quoted.get("last")
        if bid is not None and ask is not None:
            return (bid + ask) / 2
        return last

    @staticmethod
    def _fmt(value) -> str:
        return "--" if value is None else f"{value:.2f}"
