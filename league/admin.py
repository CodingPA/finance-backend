from django.contrib import admin

from .models import (
    AgentScratchpad,
    AgentTickerMemory,
    AgentTurn,
    AgentWakeup,
    Competition,
    Competitor,
    DailyBrief,
    DEFAULT_INDEX_TICKERS,
    ForcedCheckin,
    Position,
    Trade,
    WeeklyMark,
)


class ForcedCheckinInline(admin.TabularInline):
    model = ForcedCheckin
    extra = 0


@admin.register(Competition)
class CompetitionAdmin(admin.ModelAdmin):
    list_display = [
        "name", "status", "starting_capital", "max_position_pct", "max_index_pct",
        "restrict_after_hours_trading", "start_date", "end_date",
    ]
    list_editable = ["status"]
    inlines = [ForcedCheckinInline]
    change_form_template = "admin/league/competition/change_form.html"

    def render_change_form(self, request, context, *args, **kwargs):
        # Powers the "Load default index tickers" button next to
        # index_tickers on the add/change form (see the template above and
        # its JS) -- embedded via json_script rather than duplicated in a
        # static JS file, so DEFAULT_INDEX_TICKERS (models.py) stays the
        # one place this list is maintained.
        context["default_index_tickers"] = DEFAULT_INDEX_TICKERS
        return super().render_change_form(request, context, *args, **kwargs)


@admin.register(Competitor)
class CompetitorAdmin(admin.ModelAdmin):
    list_display = [
        "avatar", "name", "color", "competition", "kind", "user", "assistant_model",
        "cash_balance", "total_budget_usd", "weekly_budget_usd", "is_paused", "created_at",
    ]
    list_editable = ["is_paused"]
    list_filter = ["competition", "kind", "is_paused"]


@admin.register(Position)
class PositionAdmin(admin.ModelAdmin):
    list_display = [
        "competitor", "instrument_type", "ticker", "strike", "expiration",
        "quantity", "is_short", "avg_cost", "covers", "opened_at",
    ]
    list_filter = ["instrument_type", "is_short", "competitor"]


@admin.register(Trade)
class TradeAdmin(admin.ModelAdmin):
    list_display = [
        "competitor", "side", "instrument_type", "ticker", "quantity",
        "limit_price", "fill_price", "status", "created_at",
    ]
    list_filter = ["status", "side", "instrument_type", "competitor"]


@admin.register(AgentScratchpad)
class AgentScratchpadAdmin(admin.ModelAdmin):
    list_display = ["competitor", "updated_at"]


@admin.register(AgentTickerMemory)
class AgentTickerMemoryAdmin(admin.ModelAdmin):
    list_display = ["competitor", "ticker", "updated_at"]
    list_filter = ["competitor"]
    search_fields = ["ticker"]


@admin.register(AgentWakeup)
class AgentWakeupAdmin(admin.ModelAdmin):
    list_display = ["competitor", "run_at", "status", "error_preview", "created_at", "completed_at"]
    list_filter = ["status", "competitor"]

    @admin.display(description="Error")
    def error_preview(self, obj):
        return (obj.error[:80] + "...") if len(obj.error) > 80 else obj.error


@admin.register(AgentTurn)
class AgentTurnAdmin(admin.ModelAdmin):
    list_display = [
        "competitor", "trigger", "wakeup", "input_tokens", "output_tokens", "cost_usd", "created_at",
    ]
    list_filter = ["trigger", "competitor"]


@admin.register(WeeklyMark)
class WeeklyMarkAdmin(admin.ModelAdmin):
    list_display = ["competitor", "week_ending", "account_value", "created_at"]
    list_filter = ["competitor"]


@admin.register(DailyBrief)
class DailyBriefAdmin(admin.ModelAdmin):
    list_display = ["date", "slot", "generated_at"]
    list_filter = ["slot"]
    readonly_fields = ["date", "slot", "content", "generated_at"]
