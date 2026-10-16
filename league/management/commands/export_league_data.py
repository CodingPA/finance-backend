"""Dump the whole league app to one JSON file for offline analysis.

Read-only, safe on production. Written instead of `manage.py dumpdata
league` for three reasons:

 - AgentTurn.transcript is a full LLM transcript (every content block of
   every round, including tool results -- get_market_news dumps especially)
   and would dominate the file by an order of magnitude. dumpdata can
   exclude whole models but not one field, and the OTHER AgentTurn fields
   (trigger, tool_calls, token counts, cost) are the diagnostically
   interesting ones. Here transcripts are omitted by default and
   --full-transcripts brings them back.
 - AccountValueSnapshot is written every ~10 minutes per competitor for
   charting only; it's the highest-volume table in the app and irrelevant
   to anything but the equity curve. Summarized by default,
   --include-snapshots to dump it.
 - The interesting context isn't only rows: TIME_ZONE, the league's Beat
   entries, and the scheduling constants (LEAGUE_MAX_WAKEUPS_PER_DAY,
   MIN_WAKEUP_SPACING, SAFETY_NET_WAKEUP_DELAY) all shape how the wake-up
   loop behaves, so they're dumped alongside the data in "meta".

Not a fixture -- the shape is flattened for reading, not for loaddata.

    python manage.py export_league_data --days 30 -o /tmp/league.json
"""

import json
import os
import zoneinfo
from datetime import timedelta

from django.conf import settings
from django.core.management.base import BaseCommand
from django.db.models import Q
from django.utils import timezone

from league.models import (
    AccountValueSnapshot, AgentScratchpad, AgentTurn, AgentWakeup, Competition, Competitor, DailyBrief, ForcedCheckin,
    Position, Trade, WeeklyMark,
)
from league.services.wakeup_spacing import MIN_WAKEUP_SPACING
from league.tasks import SAFETY_NET_WAKEUP_DELAY
from league.tools import LEAGUE_MAX_TOOL_ITERATIONS, LEAGUE_MAX_WAKEUPS_PER_DAY

EASTERN = zoneinfo.ZoneInfo("America/New_York")


def _et(dt):
    """ET rendering alongside every raw UTC timestamp -- the DB is UTC but
    every question about this app ("did the 8am fire?", "was the market
    open?") is an ET question, and converting by hand while reading a dump
    is where mistakes come from."""
    return dt.astimezone(EASTERN).isoformat() if dt else None


class Command(BaseCommand):
    help = "Export league models to a single JSON file for offline analysis (read-only)."

    def add_arguments(self, parser):
        parser.add_argument("--days", type=int, default=30, help="Window for the high-volume tables (default 30).")
        parser.add_argument("-o", "--output", default="league_export.json", help="Output path.")
        parser.add_argument(
            "--full-transcripts", action="store_true",
            help="Include AgentTurn.transcript in full. Large -- expect a multi-MB file.",
        )
        parser.add_argument(
            "--include-snapshots", action="store_true",
            help="Include AccountValueSnapshot rows (equity-curve charting data, very high volume).",
        )

    def handle(self, *args, **options):
        days = options["days"]
        since = timezone.now() - timedelta(days=days)
        data = {
            "meta": self._meta(days, since),
            "competitions": self._competitions(),
            "forced_checkins": self._forced_checkins(),
            "competitors": self._competitors(),
            "scratchpads": self._scratchpads(),
            "positions": self._positions(),
            "trades": self._trades(since),
            "agent_wakeups": self._wakeups(since),
            "agent_turns": self._turns(since, options["full_transcripts"]),
            "daily_briefs": self._daily_briefs(since),
            "weekly_marks": self._weekly_marks(since),
            "account_value_snapshots": self._snapshots(since, options["include_snapshots"]),
        }

        path = options["output"]
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(data, fh, indent=1, default=str)

        size_mb = os.path.getsize(path) / (1024 * 1024)
        self.stdout.write(self.style.SUCCESS(f"Wrote {path} ({size_mb:.2f} MB)"))
        for key, value in data.items():
            if isinstance(value, list):
                self.stdout.write(f"  {key}: {len(value)}")

    # ------------------------------------------------------------------

    def _meta(self, days, since):
        """Config that shapes the wake-up loop, dumped next to the rows so
        the export is self-contained -- reading run_at values without
        knowing TIME_ZONE, or wake-up counts without knowing the daily cap,
        invites the wrong conclusion."""
        league_beat = {
            name: str(entry["schedule"])
            for name, entry in settings.CELERY_BEAT_SCHEDULE.items()
            if entry["task"].startswith("league.")
        }
        return {
            "generated_at": timezone.now().isoformat(),
            "generated_at_et": _et(timezone.now()),
            "window_days": days,
            "window_since": since.isoformat(),
            "django_time_zone": settings.TIME_ZONE,
            "use_tz": settings.USE_TZ,
            "celery_timezone": getattr(settings, "CELERY_TIMEZONE", None),
            "celery_broker_transport_options": settings.CELERY_BROKER_TRANSPORT_OPTIONS,
            "league_beat_schedule": league_beat,
            "constants": {
                "LEAGUE_MAX_WAKEUPS_PER_DAY": LEAGUE_MAX_WAKEUPS_PER_DAY,
                "LEAGUE_MAX_TOOL_ITERATIONS": LEAGUE_MAX_TOOL_ITERATIONS,
                "MIN_WAKEUP_SPACING_seconds": MIN_WAKEUP_SPACING.total_seconds(),
                "SAFETY_NET_WAKEUP_DELAY_seconds": SAFETY_NET_WAKEUP_DELAY.total_seconds(),
            },
        }

    def _competitions(self):
        return [
            {
                "id": c.id, "name": c.name, "status": c.status,
                "start_date": str(c.start_date), "end_date": str(c.end_date),
                "starting_capital": c.starting_capital,
                "max_position_pct": c.max_position_pct, "max_index_pct": c.max_index_pct,
                "cash_apy": c.cash_apy,
                # The full list is ~250 tickers of pure noise for this
                # purpose; the count is the only part worth carrying.
                "index_tickers_count": len(c.index_tickers or []),
                "created_at": c.created_at.isoformat(),
            }
            for c in Competition.objects.all()
        ]

    def _forced_checkins(self):
        return [
            {
                "id": f.id, "competition_id": f.competition_id, "competition": f.competition.name,
                "hour": f.hour, "minute": f.minute, "label": f.label, "prompt": f.prompt,
                "inject_news": f.inject_news, "news_slot": f.news_slot,
            }
            for f in ForcedCheckin.objects.select_related("competition")
        ]

    def _competitors(self):
        return [
            {
                "id": c.id, "competition_id": c.competition_id, "name": c.name, "kind": c.kind,
                "is_paused": c.is_paused, "user_id": c.user_id,
                "assistant_model": (
                    {
                        "id": c.assistant_model.id,
                        "provider": c.assistant_model.provider,
                        "model_id": c.assistant_model.model_id,
                    }
                    if c.assistant_model_id else None
                ),
                "cash_balance": c.cash_balance, "system_prompt": c.system_prompt,
                "created_at": c.created_at.isoformat(),
            }
            for c in Competitor.objects.select_related("competition", "assistant_model")
        ]

    def _scratchpads(self):
        # Kept in full -- it's the agent's own durable state, small, and the
        # place its stated intentions ("check back at 3:30") actually live.
        return [
            {
                "competitor_id": s.competitor_id, "competitor": s.competitor.name,
                "updated_at": s.updated_at.isoformat(), "updated_at_et": _et(s.updated_at),
                "content": s.content,
            }
            for s in AgentScratchpad.objects.select_related("competitor")
        ]

    def _positions(self):
        return [
            {
                "id": p.id, "competitor_id": p.competitor_id, "competitor": p.competitor.name,
                "instrument_type": p.instrument_type, "ticker": p.ticker, "strike": p.strike,
                "expiration": str(p.expiration) if p.expiration else None, "quantity": p.quantity,
                "is_short": p.is_short, "avg_cost": p.avg_cost, "covers_id": p.covers_id,
                "target_price": p.target_price, "dead_price": p.dead_price,
                "opened_at": p.opened_at.isoformat(),
            }
            for p in Position.objects.select_related("competitor")
        ]

    def _trades(self, since):
        return [
            {
                "id": t.id, "competitor_id": t.competitor_id, "competitor": t.competitor.name,
                "position_id": t.position_id, "side": t.side, "instrument_type": t.instrument_type,
                "ticker": t.ticker, "strike": t.strike,
                "expiration": str(t.expiration) if t.expiration else None,
                "quantity": t.quantity, "limit_price": t.limit_price, "fill_price": t.fill_price,
                "status": t.status, "rejection_code": t.rejection_code,
                "rejection_reason": t.rejection_reason, "thesis": t.thesis,
                "created_at": t.created_at.isoformat(), "created_at_et": _et(t.created_at),
            }
            for t in Trade.objects.select_related("competitor").filter(created_at__gte=since).order_by("created_at")
        ]

    def _wakeups(self, since):
        # Deliberately NOT filtered to created_at >= since alone: a wake-up
        # created before the window but still STATUS_SCHEDULED is exactly
        # the orphan case worth seeing, so those come along regardless of age.
        rows = AgentWakeup.objects.select_related("competitor").filter(
            Q(created_at__gte=since) | Q(status=AgentWakeup.STATUS_SCHEDULED)
        ).order_by("run_at")
        return [
            {
                "id": w.id, "competitor_id": w.competitor_id, "competitor": w.competitor.name,
                "status": w.status,
                "run_at": w.run_at.isoformat(), "run_at_et": _et(w.run_at),
                "created_at": w.created_at.isoformat(), "created_at_et": _et(w.created_at),
                "completed_at": w.completed_at.isoformat() if w.completed_at else None,
                "completed_at_et": _et(w.completed_at),
                "reason": w.reason,
            }
            for w in rows
        ]

    def _turns(self, since, full_transcripts):
        rows = AgentTurn.objects.select_related("competitor").filter(created_at__gte=since).order_by("created_at")
        out = []
        for t in rows.iterator():
            row = {
                "id": t.id, "competitor_id": t.competitor_id, "competitor": t.competitor.name,
                "wakeup_id": t.wakeup_id, "trigger": t.trigger,
                "created_at": t.created_at.isoformat(), "created_at_et": _et(t.created_at),
                "input_tokens": t.input_tokens, "output_tokens": t.output_tokens,
                "cache_creation_input_tokens": t.cache_creation_input_tokens,
                "cache_read_input_tokens": t.cache_read_input_tokens,
                "cost_usd": t.cost_usd,
                "tool_calls": t.tool_calls,
                # Rounds is the tell for a turn that died against
                # LEAGUE_MAX_TOOL_ITERATIONS before reaching schedule_wakeup
                # -- the incident _ensure_future_wakeup_scheduled exists for.
                "transcript_blocks": len(t.transcript or []),
            }
            if full_transcripts:
                row["transcript"] = t.transcript
            out.append(row)
        return out

    def _daily_briefs(self, since):
        return [
            {
                "id": b.id, "date": str(b.date), "slot": b.slot,
                "generated_at": b.generated_at.isoformat(), "generated_at_et": _et(b.generated_at),
                "content_length": len(b.content or ""),
                "content_preview": (b.content or "")[:300],
            }
            for b in DailyBrief.objects.filter(generated_at__gte=since).order_by("date", "slot")
        ]

    def _weekly_marks(self, since):
        return [
            {
                "id": m.id, "competitor_id": m.competitor_id, "competitor": m.competitor.name,
                "week_ending": str(m.week_ending), "account_value": m.account_value,
                "created_at": m.created_at.isoformat(),
            }
            for m in WeeklyMark.objects.select_related("competitor").filter(created_at__gte=since)
        ]

    def _snapshots(self, since, include):
        qs = AccountValueSnapshot.objects.filter(at__gte=since)
        if not include:
            # Summary only by default -- one row per competitor per ~10min
            # of market hours dwarfs everything else in the file, and none
            # of it bears on scheduling. The per-competitor count and range
            # is still worth having: it independently shows whether the
            # Beat tick that writes them was running on a given day.
            first = qs.order_by("at").values_list("at", flat=True).first()
            last = qs.order_by("-at").values_list("at", flat=True).first()
            return {
                "omitted": "pass --include-snapshots to dump rows",
                "count": qs.count(),
                "first_at_et": _et(first), "last_at_et": _et(last),
            }
        return [
            {
                "competitor_id": s.competitor_id, "at": s.at.isoformat(), "at_et": _et(s.at),
                "account_value": s.account_value,
            }
            for s in qs.select_related("competitor").order_by("at").iterator()
        ]
