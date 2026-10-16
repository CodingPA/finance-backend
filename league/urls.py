from django.urls import path

from . import views

app_name = "league"

urlpatterns = [
    path("models/", views.LeagueModelListView.as_view(), name="models"),
    path("briefs/", views.DailyBriefListView.as_view(), name="daily-briefs"),
    path("competitions/", views.CompetitionListCreateView.as_view(), name="competition-list"),
    path("competitions/<int:competition_id>/scoreboard/", views.ScoreboardView.as_view(), name="scoreboard"),
    path("competitions/<int:competition_id>/trades/", views.CompetitionTradeFeedView.as_view(), name="competition-trades"),
    path("competitions/<int:competition_id>/refresh/", views.CompetitionRefreshView.as_view(), name="competition-refresh"),
    path(
        "competitions/<int:competition_id>/equity-curve/",
        views.EquityCurveView.as_view(), name="competition-equity-curve",
    ),
    path(
        "competitions/<int:competition_id>/competitors/",
        views.CompetitionCompetitorsView.as_view(), name="competition-competitors",
    ),
    path("competitors/<int:competitor_id>/", views.CompetitorDetailView.as_view(), name="competitor-detail"),
    path("competitors/<int:competitor_id>/refresh/", views.CompetitorRefreshView.as_view(), name="competitor-refresh"),
    path("competitors/<int:competitor_id>/trades/", views.CompetitorTradesView.as_view(), name="competitor-trades"),
    path(
        "competitors/<int:competitor_id>/trades/<int:trade_id>/cancel/",
        views.CompetitorTradeCancelView.as_view(), name="competitor-trade-cancel",
    ),
    path(
        "competitors/<int:competitor_id>/positions/<int:position_id>/price-targets/",
        views.PositionPriceTargetsView.as_view(), name="position-price-targets",
    ),
    path("competitors/<int:competitor_id>/wakeups/", views.CompetitorWakeupsView.as_view(), name="competitor-wakeups"),
    path(
        "competitors/<int:competitor_id>/wakeups/next/",
        views.CompetitorNextWakeupsView.as_view(), name="competitor-next-wakeups",
    ),
    path("competitors/<int:competitor_id>/turns/", views.CompetitorTurnsView.as_view(), name="competitor-turns"),
    path("competitors/<int:competitor_id>/turns/<int:turn_id>/", views.TurnDetailView.as_view(), name="turn-detail"),
    path(
        "competitors/<int:competitor_id>/scratchpad/",
        views.CompetitorScratchpadView.as_view(), name="competitor-scratchpad",
    ),
    path(
        "competitors/<int:competitor_id>/ticker-memories/",
        views.CompetitorTickerMemoriesView.as_view(), name="competitor-ticker-memories",
    ),
]
