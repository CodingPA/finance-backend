from django.urls import path

from . import views

app_name = "companies"

urlpatterns = [
    path("", views.CompanyListView.as_view(), name="list"),
    path("clips/search/", views.ClipSearchView.as_view(), name="clip-search"),
    path("subreddits/", views.SubredditListView.as_view(), name="subreddit-list"),
    path("subreddits/refresh/", views.SubredditRefreshAllView.as_view(), name="subreddit-refresh-all"),
    path("subreddits/<int:subreddit_id>/", views.SubredditDetailView.as_view(), name="subreddit-detail"),
    path("subreddits/<int:subreddit_id>/refresh/", views.SubredditRefreshView.as_view(), name="subreddit-refresh"),
    path("subreddit-mentions/", views.SubredditMentionListView.as_view(), name="subreddit-mentions"),
    path("<str:ticker>/", views.CompanyDetailView.as_view(), name="detail"),
    path("<str:ticker>/quote/", views.QuoteView.as_view(), name="quote"),
    path("<str:ticker>/options-expirations/", views.OptionsExpirationsView.as_view(), name="options-expirations"),
    path("<str:ticker>/options-chain/", views.OptionsChainView.as_view(), name="options-chain"),
    path("<str:ticker>/screen/", views.ScreenView.as_view(), name="screen"),
    path("<str:ticker>/status/", views.TrackedCompanyStatusView.as_view(), name="status"),
    path("<str:ticker>/price-history/", views.PriceHistoryView.as_view(), name="price-history"),
    path("<str:ticker>/clips/", views.ResearchClipListView.as_view(), name="clips"),
    path("<str:ticker>/clips/search/", views.ClipSearchView.as_view(), name="ticker-clip-search"),
    path("<str:ticker>/clips/<int:clip_id>/", views.ResearchClipDetailView.as_view(), name="clip-detail"),
    path("<str:ticker>/journal/", views.JournalView.as_view(), name="journal"),
    path("<str:ticker>/wiki/", views.WikiSourceListView.as_view(), name="wiki-sources"),
    path("<str:ticker>/wiki/<int:source_id>/", views.WikiSourceDetailView.as_view(), name="wiki-source-detail"),
]
