"""Down-streak/above-SMA screener over DailyBar -- the market-wide scan
league/tools.py's scan_down_streaks tool exposes to AI competitors,
filling the "no last-N-closes tool" gap documented in
docs/league-agent-guide.md's known-gaps section (a strategy rule like
"3-5 consecutive down closes" previously had no bulk daily-history tool
to check against, only per-ticker technicals.py calls). Computed entirely
from Polygon-backed DailyBar rows (companies/services/polygon.py) --
independent of PRICING_PROVIDER/Tradier, unlike technicals.py's own SMA
fields, which are null outright on a Steady deployment.

Benchmarked against a synthetic 220-session/12,592-ticker dataset
(2026-09-21): ~6.3s end to end (6.05s for the windowed SQL query, 0.25s
for the Python streak/SMA pass) for the ENTIRE market -- cheap enough to
scan on every call rather than needing a curated watchlist or a
precomputed/cached table."""

from itertools import groupby

from django.db import connection

from companies.models import DailyBar

# Hard caps on the two request-shaped inputs, same "bound it somewhere"
# reasoning as league/tools.py's other tool-level caps (get_price_history's
# 90-day cap, get_options_chain's 10-nearest-the-money) -- these bound a
# live query's cost/result size, not just context-window bloat.
MAX_SMA_WINDOW = 200  # DailyBar's own practical ceiling -- see backfill_daily_bars's --days default.
MAX_STREAK = 10
MAX_CANDIDATES = 50


def scan_down_streaks(min_streak: int = 3, max_streak: int = 5, sma_window: int = 200) -> list[dict]:
    """Every ticker currently on a `min_streak`-to-`max_streak`-session
    run of lower closes (today's close < yesterday's < the day before...)
    while its latest close sits above its own `sma_window`-day SMA.
    Requires at least `sma_window` sessions of DailyBar history for a
    ticker to be considered at all -- a recent IPO or a ticker not yet
    backfilled that far back is silently excluded, not an error.

    Results are capped at MAX_CANDIDATES, ordered by longest streak
    first -- a broad min_streak (e.g. 1) can match far more names than
    that, so a caller that needs "all of them" isn't what this returns;
    it returns the most pronounced ones.

    Each result: {ticker, down_streak, last_close, sma, avg_volume,
    as_of} -- sma/avg_volume are both averaged over the same sma_window
    sessions the streak was checked against. No earnings/liquidity/thesis
    filtering here -- that's the caller's job on the (small) candidate
    list this returns, not baked into a full-market scan."""
    sma_window = max(2, min(sma_window, MAX_SMA_WINDOW))
    min_streak = max(1, min(min_streak, MAX_STREAK))
    max_streak = max(min_streak, min(max_streak, MAX_STREAK))

    table = DailyBar._meta.db_table
    with connection.cursor() as cursor:
        cursor.execute(
            f"""
            SELECT ticker, date, close, volume FROM (
                SELECT ticker, date, close, volume,
                       ROW_NUMBER() OVER (PARTITION BY ticker ORDER BY date DESC) AS rn
                FROM {table}
            ) ranked
            WHERE rn <= %s
            ORDER BY ticker, date DESC
            """,
            [sma_window],
        )
        rows = cursor.fetchall()

    candidates = []
    for ticker, group in groupby(rows, key=lambda r: r[0]):
        bars = list(group)  # already ORDER BY date DESC -- most recent first
        if len(bars) < sma_window:
            continue

        closes = [b[2] for b in bars]
        streak = 0
        for i in range(len(closes) - 1):
            if closes[i] < closes[i + 1]:
                streak += 1
            else:
                break
        if not (min_streak <= streak <= max_streak):
            continue

        sma = sum(closes) / sma_window
        if closes[0] <= sma:
            continue

        as_of = bars[0][1]
        candidates.append({
            "ticker": ticker,
            "down_streak": streak,
            "last_close": closes[0],
            "sma": round(sma, 4),
            "avg_volume": round(sum(b[3] for b in bars) / sma_window, 2),
            # SQLite's date column round-trips as a plain "YYYY-MM-DD" str;
            # Postgres' native date type comes back as a real date object --
            # normalized here so callers get one consistent shape either way.
            "as_of": as_of.isoformat() if hasattr(as_of, "isoformat") else as_of,
        })

    candidates.sort(key=lambda c: c["down_streak"], reverse=True)
    return candidates[:MAX_CANDIDATES]
