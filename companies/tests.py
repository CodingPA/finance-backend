from datetime import date, timedelta
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from django.db import IntegrityError
from django.test import TestCase, override_settings
from django.utils import timezone
from rest_framework_simplejwt.tokens import RefreshToken

from accounts.models import User

from .models import Company, CompanySnapshot, ResearchClip, SubredditMention, TrackedCompany, TrackedSubreddit
from .services import brave_search, fmp, pricing, reddit_tickers, sec_edgar, steady, technicals, tradier, xbrl_tag_mapping
from .services.presentation import annual_values, build_report, latest_entry
from .services.screening import REVENUE_TAGS, run_screen
from .tasks import refresh_tracked_subreddit, scan_reddit_for_tracked_tickers, scan_tracked_subreddits


def _reddit_post(id_="abc123", subreddit="wallstreetbets", title="Test post", selftext=""):
    return {
        "id": id_, "title": title, "selftext": selftext, "author": "someone",
        "subreddit": subreddit, "score": 100, "upvote_ratio": 0.9,
        "num_comments": 10, "created_utc": 1755000000, "permalink": f"/r/{subreddit}/comments/{id_}/",
        "url": "https://reddit.com", "extra_field_not_in_allowlist": "dropped",
    }


@override_settings(STEADY_API_TOKEN="test-token")
class RedditSteadyClientTests(TestCase):
    """companies/services/steady.py's get_reddit_search/get_reddit_posts --
    direct unit coverage of param validation and the field-allowlist
    normalization; end-to-end tool behavior (including the confirmed-live
    subreddit-filter bug and the real extraction pipeline) is covered by
    league/tests.py's GetRedditSentimentToolTests/
    ScanSubredditForTickersToolTests."""

    def test_get_reddit_search_normalizes_and_allowlists_fields(self):
        mock_resp = MagicMock()
        mock_resp.json.return_value = {"body": [_reddit_post()]}
        with patch("companies.services.steady.http.get", return_value=mock_resp) as mock_get:
            result = steady.get_reddit_search("SLS")
        self.assertEqual(len(result), 1)
        self.assertNotIn("extra_field_not_in_allowlist", result[0])
        self.assertEqual(result[0]["subreddit"], "wallstreetbets")
        called_url = mock_get.call_args[0][0]
        self.assertIn("search=SLS", called_url)

    def test_get_reddit_search_forwards_subreddit_param(self):
        # Confirmed-live bug: SteadyAPI itself silently ignores this param
        # server-side -- but our client
        # still forwards it in case that ever gets fixed, so this just
        # checks OUR request, not that filtering actually happens.
        mock_resp = MagicMock()
        mock_resp.json.return_value = {"body": []}
        with patch("companies.services.steady.http.get", return_value=mock_resp) as mock_get:
            steady.get_reddit_search("SLS", subreddit="wallstreetbets")
        called_url = mock_get.call_args[0][0]
        self.assertIn("subreddit=wallstreetbets", called_url)

    def test_get_reddit_search_invalid_sort_type_raises(self):
        with self.assertRaises(ValueError):
            steady.get_reddit_search("SLS", sort_type="bogus")

    def test_get_reddit_search_invalid_time_filter_raises(self):
        with self.assertRaises(ValueError):
            steady.get_reddit_search("SLS", time_filter="bogus")

    def test_get_reddit_search_returns_none_when_unconfigured(self):
        with override_settings(STEADY_API_TOKEN=""):
            self.assertIsNone(steady.get_reddit_search("SLS"))

    def test_get_reddit_posts_normalizes_and_allowlists_fields(self):
        mock_resp = MagicMock()
        mock_resp.json.return_value = {"body": [_reddit_post()]}
        with patch("companies.services.steady.http.get", return_value=mock_resp):
            result = steady.get_reddit_posts("wallstreetbets")
        self.assertEqual(len(result), 1)
        self.assertNotIn("extra_field_not_in_allowlist", result[0])

    def test_get_reddit_posts_invalid_sort_type_raises(self):
        with self.assertRaises(ValueError):
            steady.get_reddit_posts("wallstreetbets", sort_type="bogus")

    def test_get_reddit_posts_returns_none_when_unconfigured(self):
        with override_settings(STEADY_API_TOKEN=""):
            self.assertIsNone(steady.get_reddit_posts("wallstreetbets"))


@override_settings(BRAVE_API_KEY="test-key")
class BraveSearchClientTests(TestCase):
    """companies/services/brave_search.py::search_web -- the only GENERIC
    open-web search this app has, shared by assistant/tools.py and
    league/tools.py's search_web tool (end-to-end tool behavior covered
    by each app's own SearchWebToolTests)."""

    def test_returns_normalized_results(self):
        mock_resp = MagicMock()
        mock_resp.json.return_value = {
            "web": {"results": [
                {"title": "Rocket Lab", "url": "https://example.com/1", "description": "...", "extra_snippets": ["x"]},
            ]},
        }
        with patch("companies.services.brave_search.http.get", return_value=mock_resp) as mock_get:
            result = brave_search.search_web("Rocket Lab Neutron")
        self.assertEqual(result, [{"title": "Rocket Lab", "url": "https://example.com/1", "description": "..."}])
        called_url = mock_get.call_args[0][0]
        self.assertIn("q=Rocket%20Lab%20Neutron", called_url)
        self.assertEqual(mock_get.call_args.kwargs["headers"]["X-Subscription-Token"], "test-key")

    def test_returns_empty_list_for_no_results(self):
        mock_resp = MagicMock()
        mock_resp.json.return_value = {"web": {"results": []}}
        with patch("companies.services.brave_search.http.get", return_value=mock_resp):
            self.assertEqual(brave_search.search_web("zzzznoresultszzzz"), [])

    def test_returns_none_when_unconfigured(self):
        with override_settings(BRAVE_API_KEY=""):
            self.assertIsNone(brave_search.search_web("anything"))

    def test_count_is_clamped_to_braves_max(self):
        mock_resp = MagicMock()
        mock_resp.json.return_value = {"web": {"results": []}}
        with patch("companies.services.brave_search.http.get", return_value=mock_resp) as mock_get:
            brave_search.search_web("query", count=999)
        called_url = mock_get.call_args[0][0]
        self.assertIn(f"count={brave_search.MAX_RESULTS}", called_url)


class ExtractTickersTests(TestCase):
    """companies/services/reddit_tickers.py's extract_tickers -- only the
    not-configured/no-posts edge cases are unit-tested here; the actual
    contextual-extraction behavior (telling "$SLS the stock" from "SLS
    the NASA rocket") was confirmed live against the real model rather
    than mocked, since mocking the LLM's judgment would just test the
    mock, not the real behavior."""

    def test_returns_none_when_openai_not_configured(self):
        with override_settings(OPENAI_API_KEY=""):
            self.assertIsNone(reddit_tickers.extract_tickers([_reddit_post()]))

    def test_returns_empty_list_for_no_posts_without_calling_openai(self):
        with override_settings(OPENAI_API_KEY="test-key"), \
             patch("companies.services.reddit_tickers.OpenAI") as mock_openai:
            result = reddit_tickers.extract_tickers([])
        self.assertEqual(result, [])
        mock_openai.assert_not_called()


class AttributeMentionsTests(TestCase):
    """companies/services/reddit_tickers.py's attribute_mentions -- pure
    Python, no LLM call, so fully unit-testable without mocking."""

    def test_matches_cashtag_case_insensitively(self):
        posts = [_reddit_post(id_="p1", title="$sls mooning today")]
        self.assertEqual(reddit_tickers.attribute_mentions(posts, ["SLS"]), {"SLS": posts})

    def test_matches_bare_uppercase_ticker(self):
        posts = [_reddit_post(id_="p1", title="SLS just announced a partnership")]
        self.assertEqual(reddit_tickers.attribute_mentions(posts, ["SLS"]), {"SLS": posts})

    def test_does_not_match_lowercase_bare_substring(self):
        # Deliberately case-sensitive for the bare (non-cashtag) form --
        # a stray lowercase "sls" inside unrelated text shouldn't count.
        posts = [_reddit_post(id_="p1", title="results were pretty sls-y today")]
        self.assertEqual(reddit_tickers.attribute_mentions(posts, ["SLS"]), {})

    def test_word_boundary_does_not_partial_match_inside_a_larger_word(self):
        # "AI" (a real ticker, C3.ai) shouldn't match inside "MAIN" even
        # though the substring is present, uppercase and all.
        posts = [_reddit_post(id_="p1", title="The MAIN event is today")]
        self.assertEqual(reddit_tickers.attribute_mentions(posts, ["AI"]), {})

    def test_omits_tickers_with_no_matching_posts(self):
        posts = [_reddit_post(id_="p1", title="Nothing about any ticker here")]
        self.assertEqual(reddit_tickers.attribute_mentions(posts, ["SLS", "MRNA"]), {})

    def test_empty_ticker_list_returns_empty_without_scanning(self):
        posts = [_reddit_post(id_="p1", title="$SLS mooning")]
        self.assertEqual(reddit_tickers.attribute_mentions(posts, []), {})


class ScanTrackedSubredditsTaskTests(TestCase):
    """companies/tasks.py::scan_tracked_subreddits -- the twice-daily
    discovery scan."""

    def setUp(self):
        self.user = User.objects.create_user(email="watcher@example.com", password="testpass123")
        self.watch = TrackedSubreddit.objects.create(user=self.user, name="wallstreetbets")

    def test_creates_mention_rows_from_extracted_and_attributed_tickers(self):
        posts = [_reddit_post(id_="p1", title="$SLS mooning today")]
        with patch("companies.services.steady.get_reddit_posts", return_value=posts), \
             patch("companies.services.reddit_tickers.extract_tickers", return_value=["SLS"]):
            scan_tracked_subreddits()
        mention = SubredditMention.objects.get(user=self.user, subreddit="wallstreetbets", ticker="SLS", post_id="p1")
        self.assertEqual(mention.title, "$SLS mooning today")

    def test_rescanning_the_same_post_does_not_duplicate(self):
        posts = [_reddit_post(id_="p1", title="$SLS mooning today")]
        with patch("companies.services.steady.get_reddit_posts", return_value=posts), \
             patch("companies.services.reddit_tickers.extract_tickers", return_value=["SLS"]):
            scan_tracked_subreddits()
            scan_tracked_subreddits()
        self.assertEqual(SubredditMention.objects.filter(user=self.user).count(), 1)

    def test_no_posts_creates_no_mentions_without_calling_extraction(self):
        with patch("companies.services.steady.get_reddit_posts", return_value=[]), \
             patch("companies.services.reddit_tickers.extract_tickers") as mock_extract:
            scan_tracked_subreddits()
        mock_extract.assert_not_called()
        self.assertFalse(SubredditMention.objects.exists())

    def test_one_subreddits_failure_does_not_block_another(self):
        TrackedSubreddit.objects.create(user=self.user, name="stocks")

        def _side_effect(name, sort_type="hot"):
            if name == "wallstreetbets":
                raise RuntimeError("Steady is down")
            return [_reddit_post(id_="p2", title="$MRNA news")]

        with patch("companies.services.steady.get_reddit_posts", side_effect=_side_effect), \
             patch("companies.services.reddit_tickers.extract_tickers", return_value=["MRNA"]):
            scan_tracked_subreddits()
        self.assertTrue(SubredditMention.objects.filter(subreddit="stocks", ticker="MRNA").exists())
        self.assertFalse(SubredditMention.objects.filter(subreddit="wallstreetbets").exists())

    def test_last_scanned_at_advances_even_on_failure(self):
        # "last ATTEMPTED", not "last successful" -- see that field's own
        # docstring: a Steady failure must not leave last_scanned_at
        # looking identical to this subreddit never having been reached.
        self.assertIsNone(self.watch.last_scanned_at)
        with patch("companies.services.steady.get_reddit_posts", side_effect=RuntimeError("Steady is down")):
            scan_tracked_subreddits()
        self.watch.refresh_from_db()
        self.assertIsNotNone(self.watch.last_scanned_at)

    def test_last_scanned_at_advances_on_a_successful_empty_scan(self):
        with patch("companies.services.steady.get_reddit_posts", return_value=[]):
            scan_tracked_subreddits()
        self.watch.refresh_from_db()
        self.assertIsNotNone(self.watch.last_scanned_at)


class RefreshTrackedSubredditTaskTests(TestCase):
    """companies/tasks.py::refresh_tracked_subreddit -- the on-demand,
    single-subreddit counterpart to scan_tracked_subreddits, queued by
    POST .../subreddits/{id}/refresh/. Result arrives as subreddit.progress
    / subreddit.complete / subreddit.error pushes (docs/websockets.md),
    same shape as screen.*, so assertions here are against
    companies.tasks.send_to_user calls rather than the DB alone."""

    def setUp(self):
        self.user = User.objects.create_user(email="refreshuser@example.com", password="testpass123")
        self.watch = TrackedSubreddit.objects.create(user=self.user, name="wallstreetbets")

    def test_pushes_progress_then_complete_with_new_mentions_count(self):
        posts = [_reddit_post(id_="p1", title="$SLS mooning today")]
        with patch("companies.services.steady.get_reddit_posts", return_value=posts), \
             patch("companies.services.reddit_tickers.extract_tickers", return_value=["SLS"]), \
             patch("companies.tasks.send_to_user") as mock_send:
            refresh_tracked_subreddit(self.watch.id)

        mention = SubredditMention.objects.get(user=self.user, subreddit="wallstreetbets", ticker="SLS", post_id="p1")
        self.assertIsNotNone(mention)
        self.assertEqual(mock_send.call_count, 2)
        progress_call, complete_call = mock_send.call_args_list
        self.assertEqual(progress_call.args[:2], (self.user.id, "subreddit.progress"))
        self.assertEqual(progress_call.kwargs["subreddit_id"], self.watch.id)
        self.assertEqual(complete_call.args[:2], (self.user.id, "subreddit.complete"))
        self.assertEqual(complete_call.kwargs["new_mentions"], 1)
        self.assertEqual(complete_call.kwargs["tickers"], ["SLS"])

    def test_rescanning_the_same_post_reports_zero_new_mentions(self):
        posts = [_reddit_post(id_="p1", title="$SLS mooning today")]
        with patch("companies.services.steady.get_reddit_posts", return_value=posts), \
             patch("companies.services.reddit_tickers.extract_tickers", return_value=["SLS"]):
            refresh_tracked_subreddit(self.watch.id)
            with patch("companies.tasks.send_to_user") as mock_send:
                refresh_tracked_subreddit(self.watch.id)
        complete_call = mock_send.call_args_list[-1]
        self.assertEqual(complete_call.kwargs["new_mentions"], 0)
        self.assertEqual(SubredditMention.objects.filter(user=self.user).count(), 1)

    def test_pushes_error_event_on_failure_instead_of_raising(self):
        with patch("companies.services.steady.get_reddit_posts", side_effect=RuntimeError("Steady is down")), \
             patch("companies.tasks.send_to_user") as mock_send:
            refresh_tracked_subreddit(self.watch.id)  # must not raise
        error_call = mock_send.call_args_list[-1]
        self.assertEqual(error_call.args[:2], (self.user.id, "subreddit.error"))
        self.assertIn("Steady is down", error_call.kwargs["message"])

    def test_skips_quietly_for_a_deleted_subreddit(self):
        deleted_id = self.watch.id
        self.watch.delete()
        with patch("companies.tasks.send_to_user") as mock_send:
            refresh_tracked_subreddit(deleted_id)  # must not raise
        mock_send.assert_not_called()


class SubredditRefreshEndpointTests(TestCase):
    """POST /api/companies/subreddits/{id}/refresh/ and
    POST /api/companies/subreddits/refresh/ -- queue refresh_tracked_subreddit
    rather than running it inline, same 202 contract as ScreenView."""

    def setUp(self):
        self.user = User.objects.create_user(email="refreshapi@example.com", password="testpass123")
        access_token = RefreshToken.for_user(self.user).access_token
        self.client.defaults["HTTP_AUTHORIZATION"] = f"Bearer {access_token}"
        self.watch = TrackedSubreddit.objects.create(user=self.user, name="wallstreetbets")

    def test_single_refresh_queues_task_and_returns_202(self):
        with patch("companies.views.refresh_tracked_subreddit.delay") as mock_delay:
            response = self.client.post(f"/api/companies/subreddits/{self.watch.id}/refresh/")
        self.assertEqual(response.status_code, 202, response.content)
        mock_delay.assert_called_once_with(self.watch.id)

    def test_single_refresh_404s_for_someone_elses_subreddit(self):
        other_user = User.objects.create_user(email="otherrefresh@example.com", password="testpass123")
        other_watch = TrackedSubreddit.objects.create(user=other_user, name="stocks")
        with patch("companies.views.refresh_tracked_subreddit.delay") as mock_delay:
            response = self.client.post(f"/api/companies/subreddits/{other_watch.id}/refresh/")
        self.assertEqual(response.status_code, 404)
        mock_delay.assert_not_called()

    def test_refresh_all_queues_one_task_per_watched_subreddit(self):
        TrackedSubreddit.objects.create(user=self.user, name="stocks")
        other_user = User.objects.create_user(email="otherrefreshall@example.com", password="testpass123")
        TrackedSubreddit.objects.create(user=other_user, name="options")

        with patch("companies.views.refresh_tracked_subreddit.delay") as mock_delay:
            response = self.client.post("/api/companies/subreddits/refresh/")
        self.assertEqual(response.status_code, 202, response.content)
        self.assertEqual(response.json()["count"], 2)
        self.assertEqual(mock_delay.call_count, 2)  # not the other user's third watch


class ScanRedditForTrackedTickersTaskTests(TestCase):
    """companies/tasks.py::scan_reddit_for_tracked_tickers -- the daily
    per-screener-ticker enrichment scan, feeding the existing
    ResearchClip -> Weaviate pipeline."""

    def setUp(self):
        self.user = User.objects.create_user(email="tracker@example.com", password="testpass123")
        self.company = Company.objects.create(ticker="SLS", cik=1234567, name="SELLAS Life Sciences")
        self.tracked = TrackedCompany.objects.create(user=self.user, company=self.company)

    def test_creates_research_clip_and_queues_indexing(self):
        posts = [_reddit_post(id_="p1", title="$SLS news", selftext="big move")]
        with patch("companies.services.steady.get_reddit_search", return_value=posts), \
             patch("companies.tasks.index_clip.delay") as mock_delay:
            scan_reddit_for_tracked_tickers()
        clip = ResearchClip.objects.get(tracked_company=self.tracked)
        self.assertEqual(clip.source_type, ResearchClip.SOURCE_TYPE_REDDIT)
        self.assertEqual(clip.label, "$SLS news")
        mock_delay.assert_called_once_with(clip.id)

    def test_rerunning_does_not_duplicate_or_requeue(self):
        posts = [_reddit_post(id_="p1", title="$SLS news")]
        with patch("companies.services.steady.get_reddit_search", return_value=posts), \
             patch("companies.tasks.index_clip.delay") as mock_delay:
            scan_reddit_for_tracked_tickers()
            scan_reddit_for_tracked_tickers()
        self.assertEqual(ResearchClip.objects.filter(tracked_company=self.tracked).count(), 1)
        mock_delay.assert_called_once()

    def test_no_results_creates_no_clip(self):
        with patch("companies.services.steady.get_reddit_search", return_value=[]), \
             patch("companies.tasks.index_clip.delay") as mock_delay:
            scan_reddit_for_tracked_tickers()
        self.assertFalse(ResearchClip.objects.filter(tracked_company=self.tracked).exists())
        mock_delay.assert_not_called()

    def test_one_tracked_companys_failure_does_not_block_another(self):
        company2 = Company.objects.create(ticker="MRNA", cik=7654321, name="Moderna")
        tracked2 = TrackedCompany.objects.create(user=self.user, company=company2)

        def _side_effect(ticker):
            if ticker == "SLS":
                raise RuntimeError("Steady is down")
            return [_reddit_post(id_="p2", title="$MRNA news")]

        with patch("companies.services.steady.get_reddit_search", side_effect=_side_effect), \
             patch("companies.tasks.index_clip.delay"):
            scan_reddit_for_tracked_tickers()
        self.assertFalse(ResearchClip.objects.filter(tracked_company=self.tracked).exists())
        self.assertTrue(ResearchClip.objects.filter(tracked_company=tracked2).exists())


class SubredditEndpointTests(TestCase):
    """GET/POST /api/companies/subreddits/, DELETE /api/companies/subreddits/{id}/."""

    def setUp(self):
        self.user = User.objects.create_user(email="restuser@example.com", password="testpass123")
        access_token = RefreshToken.for_user(self.user).access_token
        self.client.defaults["HTTP_AUTHORIZATION"] = f"Bearer {access_token}"

    def test_create_and_list_subreddits(self):
        response = self.client.post("/api/companies/subreddits/", {"name": "wallstreetbets"})
        self.assertEqual(response.status_code, 201, response.content)
        self.assertEqual(response.json()["name"], "wallstreetbets")
        # Never scanned yet -- null, not missing, and not "just now".
        self.assertIsNone(response.json()["last_scanned_at"])

        response = self.client.get("/api/companies/subreddits/")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(response.json()), 1)

    def test_list_reflects_last_scanned_at_once_a_scan_has_run(self):
        watch = TrackedSubreddit.objects.create(user=self.user, name="wallstreetbets")
        with patch("companies.services.steady.get_reddit_posts", return_value=[]):
            scan_tracked_subreddits()
        response = self.client.get("/api/companies/subreddits/")
        row = next(r for r in response.json() if r["id"] == watch.id)
        self.assertIsNotNone(row["last_scanned_at"])

    def test_create_strips_r_prefix(self):
        response = self.client.post("/api/companies/subreddits/", {"name": "r/wallstreetbets"})
        self.assertEqual(response.status_code, 201, response.content)
        self.assertEqual(response.json()["name"], "wallstreetbets")

    def test_create_does_not_mangle_a_name_that_starts_with_r(self):
        response = self.client.post("/api/companies/subreddits/", {"name": "robinhood"})
        self.assertEqual(response.status_code, 201, response.content)
        self.assertEqual(response.json()["name"], "robinhood")

    def test_create_missing_name_400s(self):
        response = self.client.post("/api/companies/subreddits/", {})
        self.assertEqual(response.status_code, 400)

    def test_create_race_400s_without_leaking_the_db_error(self):
        # A concurrent request inserting the same name makes get_or_create
        # raise IntegrityError -- the response must not echo the DB's text.
        db_error = IntegrityError('duplicate key value violates unique constraint "unique_user_subreddit"')
        with patch.object(TrackedSubreddit.objects, "get_or_create", side_effect=db_error):
            response = self.client.post("/api/companies/subreddits/", {"name": "wallstreetbets"})
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json(), {"name": ["r/wallstreetbets is already on your watch list."]})
        self.assertNotIn("unique_user_subreddit", response.content.decode())

    def test_delete_removes_subreddit(self):
        watch = TrackedSubreddit.objects.create(user=self.user, name="wallstreetbets")
        response = self.client.delete(f"/api/companies/subreddits/{watch.id}/")
        self.assertEqual(response.status_code, 204)
        self.assertFalse(TrackedSubreddit.objects.filter(id=watch.id).exists())

    def test_delete_404_for_someone_elses_subreddit(self):
        other_user = User.objects.create_user(email="other@example.com", password="testpass123")
        watch = TrackedSubreddit.objects.create(user=other_user, name="wallstreetbets")
        response = self.client.delete(f"/api/companies/subreddits/{watch.id}/")
        self.assertEqual(response.status_code, 404)


class QuoteEndpointTests(TestCase):
    """GET /api/companies/{ticker}/quote/, GET /api/companies/{ticker}/options-chain/
    -- live pricing for the pre-trade "here's the current price" display, no
    tracking/screening required (unlike CompanyDetailView)."""

    def setUp(self):
        self.user = User.objects.create_user(email="quoteuser@example.com", password="testpass123")
        access_token = RefreshToken.for_user(self.user).access_token
        self.client.defaults["HTTP_AUTHORIZATION"] = f"Bearer {access_token}"

    def test_quote_returns_pricing_service_result(self):
        quote = {"symbol": "RKLB", "last_price": 67.62, "bid": 67.5, "ask": 67.7}
        with patch("companies.views.pricing.get_quote", return_value=quote) as mock_get_quote:
            response = self.client.get("/api/companies/rklb/quote/")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), quote)
        mock_get_quote.assert_called_once_with("RKLB", asset_class="STOCKS")

    def test_quote_accepts_asset_class_query_param(self):
        with patch("companies.views.pricing.get_quote", return_value={"symbol": "SPY"}) as mock_get_quote:
            response = self.client.get("/api/companies/spy/quote/?asset_class=etf")
        self.assertEqual(response.status_code, 200)
        mock_get_quote.assert_called_once_with("SPY", asset_class="etf")

    def test_quote_404s_when_no_provider_configured(self):
        with patch("companies.views.pricing.get_quote", return_value=None):
            response = self.client.get("/api/companies/rklb/quote/")
        self.assertEqual(response.status_code, 404)

    def test_quote_400s_on_invalid_asset_class(self):
        with patch("companies.views.pricing.get_quote", side_effect=ValueError("asset_class must be one of ...")):
            response = self.client.get("/api/companies/rklb/quote/?asset_class=bogus")
        self.assertEqual(response.status_code, 400)

    def test_options_chain_returns_pricing_service_result(self):
        chain = {"expirations": ["2026-08-15"], "calls": [], "puts": []}
        with patch("companies.views.pricing.get_options_chain", return_value=chain) as mock_get_chain:
            response = self.client.get("/api/companies/rklb/options-chain/")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), chain)
        mock_get_chain.assert_called_once_with("RKLB", expiration=None)

    def test_options_chain_passes_through_expiration_param(self):
        with patch("companies.views.pricing.get_options_chain", return_value={"expirations": [], "calls": [], "puts": []}) as mock_get_chain:
            response = self.client.get("/api/companies/rklb/options-chain/?expiration=2026-08-15")
        self.assertEqual(response.status_code, 200)
        mock_get_chain.assert_called_once_with("RKLB", expiration="2026-08-15")

    def test_options_chain_404s_when_no_provider_configured(self):
        with patch("companies.views.pricing.get_options_chain", return_value=None):
            response = self.client.get("/api/companies/rklb/options-chain/")
        self.assertEqual(response.status_code, 404)

    def test_options_chain_strikes_param_trims_to_nearest_the_money(self):
        # Contracts deliberately out of moneyness order, to prove the view
        # sorts rather than just slicing whatever order the provider sent.
        calls = [
            {"strike_price": 60, "moneyness_pct": -25.0},
            {"strike_price": 70, "moneyness_pct": 3.5},
            {"strike_price": 65, "moneyness_pct": -10.0},
        ]
        puts = [
            {"strike_price": 80, "moneyness_pct": 15.0},
            {"strike_price": 68, "moneyness_pct": 1.0},
        ]
        chain = {"expirations": ["2026-08-15"], "calls": calls, "puts": puts}
        with patch("companies.views.pricing.get_options_chain", return_value=chain):
            response = self.client.get("/api/companies/rklb/options-chain/?strikes=2")
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual([c["strike_price"] for c in body["calls"]], [70, 65])
        self.assertEqual([p["strike_price"] for p in body["puts"]], [68, 80])
        self.assertEqual(body["expirations"], ["2026-08-15"])

    def test_options_chain_strikes_must_be_a_positive_integer(self):
        chain = {"expirations": [], "calls": [], "puts": []}
        with patch("companies.views.pricing.get_options_chain", return_value=chain):
            response = self.client.get("/api/companies/rklb/options-chain/?strikes=0")
        self.assertEqual(response.status_code, 400)
        with patch("companies.views.pricing.get_options_chain", return_value=chain):
            response = self.client.get("/api/companies/rklb/options-chain/?strikes=abc")
        self.assertEqual(response.status_code, 400)

    def test_options_expirations_returns_pricing_service_result(self):
        with patch("companies.views.pricing.get_expirations", return_value=["2026-08-15", "2026-09-19"]) as mock_get_expirations:
            response = self.client.get("/api/companies/rklb/options-expirations/")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"expirations": ["2026-08-15", "2026-09-19"]})
        mock_get_expirations.assert_called_once_with("RKLB")

    def test_options_expirations_404s_when_no_provider_configured(self):
        with patch("companies.views.pricing.get_expirations", return_value=None):
            response = self.client.get("/api/companies/rklb/options-expirations/")
        self.assertEqual(response.status_code, 404)


class NearestTheMoneyTests(TestCase):
    """pricing.nearest_the_money -- shared by the options-chain endpoint's
    ?strikes= param and both AI agent tool surfaces."""

    def test_sorts_by_absolute_moneyness_ascending(self):
        contracts = [
            {"strike_price": 60, "moneyness_pct": -25.0},
            {"strike_price": 70, "moneyness_pct": 3.5},
            {"strike_price": 65, "moneyness_pct": -10.0},
        ]
        result = pricing.nearest_the_money(contracts, 2)
        self.assertEqual([c["strike_price"] for c in result], [70, 65])

    def test_unknown_moneyness_sorts_last_rather_than_erroring(self):
        contracts = [
            {"strike_price": 60, "moneyness_pct": None},
            {"strike_price": 70, "moneyness_pct": 3.5},
        ]
        result = pricing.nearest_the_money(contracts, 1)
        self.assertEqual([c["strike_price"] for c in result], [70])


@override_settings(PRICING_PROVIDER="tradier", TRADIER_API_TOKEN="test-token")
class GetExpirationsTests(TestCase):
    """pricing.get_expirations -- cheap dedicated call on Tradier, and
    Steady's {"weekly": [...], "monthly": [...]} meta flattened into one
    sorted list."""

    def test_tradier_delegates_to_dedicated_endpoint(self):
        with patch("companies.services.tradier.get_expirations", return_value=["2026-08-15", "2026-09-19"]) as mock:
            result = pricing.get_expirations("rklb")
        self.assertEqual(result, ["2026-08-15", "2026-09-19"])
        mock.assert_called_once_with("RKLB")

    @override_settings(PRICING_PROVIDER="steady")
    def test_steady_flattens_weekly_monthly_dict_into_one_sorted_list(self):
        chain = {
            "expirations": {"weekly": ["2026-08-22"], "monthly": ["2026-09-19", "2026-08-15"]},
            "calls": [], "puts": [],
        }
        with patch("companies.services.steady.get_options_chain", return_value=chain):
            result = pricing.get_expirations("rklb")
        self.assertEqual(result, ["2026-08-15", "2026-08-22", "2026-09-19"])

    @override_settings(PRICING_PROVIDER="steady")
    def test_steady_returns_none_when_not_configured(self):
        with patch("companies.services.steady.get_options_chain", return_value=None):
            result = pricing.get_expirations("rklb")
        self.assertIsNone(result)


class SubredditMentionEndpointTests(TestCase):
    """GET /api/companies/subreddit-mentions/ -- aggregated counts by
    default, ?ticker= drill-down into individual posts."""

    def setUp(self):
        self.user = User.objects.create_user(email="mentionuser@example.com", password="testpass123")
        access_token = RefreshToken.for_user(self.user).access_token
        self.client.defaults["HTTP_AUTHORIZATION"] = f"Bearer {access_token}"
        SubredditMention.objects.create(
            user=self.user, subreddit="wallstreetbets", ticker="SLS", post_id="p1",
            title="$SLS mooning", permalink="/r/wallstreetbets/comments/p1/",
        )
        SubredditMention.objects.create(
            user=self.user, subreddit="wallstreetbets", ticker="SLS", post_id="p2",
            title="$SLS again", permalink="/r/wallstreetbets/comments/p2/",
        )
        SubredditMention.objects.create(
            user=self.user, subreddit="stocks", ticker="MRNA", post_id="p3",
            title="$MRNA news", permalink="/r/stocks/comments/p3/",
        )

    def test_aggregated_counts(self):
        response = self.client.get("/api/companies/subreddit-mentions/")
        self.assertEqual(response.status_code, 200)
        body = response.json()
        sls_row = next(r for r in body if r["ticker"] == "SLS")
        self.assertEqual(sls_row["count"], 2)
        self.assertEqual(sls_row["subreddit"], "wallstreetbets")

    def test_drill_down_by_ticker(self):
        response = self.client.get("/api/companies/subreddit-mentions/?ticker=SLS")
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(len(body), 2)
        permalinks = {r["permalink"] for r in body}
        self.assertEqual(permalinks, {"/r/wallstreetbets/comments/p1/", "/r/wallstreetbets/comments/p2/"})

    def test_only_scoped_to_requesting_user(self):
        other_user = User.objects.create_user(email="otherwatcher@example.com", password="testpass123")
        SubredditMention.objects.create(
            user=other_user, subreddit="wallstreetbets", ticker="SLS", post_id="p9",
            title="not mine", permalink="/r/wallstreetbets/comments/p9/",
        )
        response = self.client.get("/api/companies/subreddit-mentions/?ticker=SLS")
        self.assertEqual(len(response.json()), 2)  # still just the 2 belonging to self.user

    def test_aggregated_counts_filtered_by_subreddit(self):
        # setUp seeds 2 SLS mentions in wallstreetbets and 1 MRNA in stocks
        # -- ?subreddit=stocks should surface only the latter.
        response = self.client.get("/api/companies/subreddit-mentions/?subreddit=stocks")
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(len(body), 1)
        self.assertEqual(body[0]["ticker"], "MRNA")

    def test_days_filter_excludes_old_mentions(self):
        # scanned_at is auto_now_add -- .update() bypasses that to backdate
        # one row well outside any ?days= window a test would use.
        SubredditMention.objects.filter(post_id="p3").update(scanned_at=timezone.now() - timedelta(days=30))

        response = self.client.get("/api/companies/subreddit-mentions/?days=7")
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(len(body), 1)
        self.assertEqual(body[0]["ticker"], "SLS")  # the 2 recent ones; MRNA (30d old) excluded

    def test_days_filter_combines_with_ticker_drill_down(self):
        SubredditMention.objects.filter(post_id="p1").update(scanned_at=timezone.now() - timedelta(days=30))

        response = self.client.get("/api/companies/subreddit-mentions/?ticker=SLS&days=7")
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(len(body), 1)
        self.assertEqual(body[0]["permalink"], "/r/wallstreetbets/comments/p2/")

    def test_invalid_days_400s(self):
        for bad in ("0", "-3", "abc"):
            response = self.client.get(f"/api/companies/subreddit-mentions/?days={bad}")
            self.assertEqual(response.status_code, 400, bad)

    def test_drill_down_by_ticker_and_subreddit_together(self):
        # SLS only ever appears in wallstreetbets here, so this just proves
        # the combination doesn't error and still scopes correctly -- a
        # ticker mentioned in a subreddit it never appeared in should come
        # back empty rather than falling back to the ticker-only result.
        response = self.client.get("/api/companies/subreddit-mentions/?ticker=SLS&subreddit=stocks")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), [])


def _daily_bar(date_str, open_=100.0, high=105.0, low=99.0, close=102.0, volume=1_000_000):
    return {"date": date_str, "open": open_, "high": high, "low": low, "close": close, "volume": volume}


@override_settings(PRICING_PROVIDER="tradier", TRADIER_API_TOKEN="test-token")
class GetPriceHistoryTests(TestCase):
    """companies.services.technicals.get_price_history -- the raw daily-
    bar counterpart to _price_technicals' derived ATR/SMA/high_50d, added
    alongside get_intraday and the FMP sector/industry fields below for
    "research tools the AI/assistant toolkits didn't have" (intraday
    candles, last N closes, sector data)."""

    def test_returns_the_last_n_bars_only(self):
        bars = [_daily_bar(f"2026-01-{i:02d}", close=100 + i) for i in range(1, 21)]
        with patch("companies.services.tradier.get_history", return_value=bars) as mock_history:
            result = technicals.get_price_history("TEST", days=5)
        self.assertEqual(len(result), 5)
        self.assertEqual([b["date"] for b in result], ["2026-01-16", "2026-01-17", "2026-01-18", "2026-01-19", "2026-01-20"])
        # Requested trading days -> a wider CALENDAR-day lookback was passed
        # to tradier.get_history (see _CALENDAR_DAYS_PER_TRADING_DAY).
        self.assertGreater(mock_history.call_args.kwargs["lookback_days"], 5)

    def test_fewer_bars_than_requested_is_not_an_error(self):
        bars = [_daily_bar("2026-01-20")]
        with patch("companies.services.tradier.get_history", return_value=bars):
            result = technicals.get_price_history("NEWIPO", days=30)
        self.assertEqual(len(result), 1)

    def test_filters_out_bars_with_no_close(self):
        bars = [_daily_bar("2026-01-19", close=None), _daily_bar("2026-01-20", close=102)]
        with patch("companies.services.tradier.get_history", return_value=bars):
            result = technicals.get_price_history("TEST", days=5)
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["date"], "2026-01-20")

    def test_no_history_returns_empty_list(self):
        with patch("companies.services.tradier.get_history", return_value=[]):
            result = technicals.get_price_history("TEST", days=5)
        self.assertEqual(result, [])

    def test_unconfigured_tradier_returns_none(self):
        with patch("companies.services.tradier.get_history", return_value=None):
            result = technicals.get_price_history("TEST", days=5)
        self.assertIsNone(result)

    @override_settings(PRICING_PROVIDER="steady")
    def test_non_tradier_provider_returns_none_without_calling_tradier(self):
        with patch("companies.services.tradier.get_history") as mock_history:
            result = technicals.get_price_history("TEST", days=5)
        self.assertIsNone(result)
        mock_history.assert_not_called()

    def test_days_is_clamped_to_the_service_level_cap(self):
        with patch("companies.services.tradier.get_history", return_value=[]) as mock_history:
            technicals.get_price_history("TEST", days=10_000)
        # Clamped to technicals._PRICE_HISTORY_MAX_DAYS (200) before being
        # turned into a calendar-day lookback -- not passed through raw.
        requested_lookback = mock_history.call_args.kwargs["lookback_days"]
        self.assertLess(requested_lookback, 10_000)


@override_settings(FMP_API_KEY="test-key")
class FmpProfileSectorTests(TestCase):
    """companies.services.fmp.get_profile -- now also passes through
    sector/industry from FMP's raw profile response, not just price/
    market_cap. Mocks companies.services.http.get directly (the actual
    transport call), since _fmp_get's non-cached path is what maps FMP's
    raw field names onto this app's own narrower shape."""

    def setUp(self):
        self.company = Company.objects.create(ticker="RKLB", cik=1819994)

    def _fake_response(self, payload):
        response = MagicMock()
        response.json.return_value = payload
        return response

    def test_sector_and_industry_pass_through_on_a_fresh_fetch(self):
        raw = [{"price": 25.5, "marketCap": 12_000_000_000, "sector": "Industrials", "industry": "Aerospace & Defense"}]
        with patch("companies.services.http.get", return_value=self._fake_response(raw)):
            profile = fmp.get_profile(self.company)
        self.assertEqual(profile["sector"], "Industrials")
        self.assertEqual(profile["industry"], "Aerospace & Defense")
        self.assertEqual(profile["market_cap"], 12_000_000_000)

    def test_missing_sector_in_raw_response_is_null_not_an_error(self):
        raw = [{"price": 25.5, "marketCap": 12_000_000_000}]
        with patch("companies.services.http.get", return_value=self._fake_response(raw)):
            profile = fmp.get_profile(self.company)
        self.assertIsNone(profile["sector"])
        self.assertIsNone(profile["industry"])

    def test_cached_snapshot_from_before_this_field_existed_returns_none(self):
        CompanySnapshot.objects.create(
            company=self.company, data_type="profile:fmp",
            raw_json={"price": 25.5, "market_cap": 12_000_000_000, "queried_at": "2026-01-01T00:00:00Z"},
        )
        profile = fmp.get_profile(self.company)
        self.assertIsNone(profile.get("sector"))


def _submissions(cik: int, forms: list[str]) -> dict:
    """Minimal valid sec_edgar.list_filings() input -- one synthetic
    filing per form given, newest-dated first (matches EDGAR's own
    ordering, though list_filings doesn't itself re-sort)."""
    return {
        "cik": str(cik),
        "filings": {
            "recent": {
                "form": forms,
                "filingDate": [f"2026-0{i + 1}-01" for i in range(len(forms))],
                "reportDate": [f"2025-12-31" for _ in forms],
                "accessionNumber": [f"0001234567-26-00000{i}" for i in range(len(forms))],
                "primaryDocument": [f"doc{i}.htm" for i in range(len(forms))],
                "primaryDocDescription": forms,
            },
        },
    }


class ListFilingsFormsTests(TestCase):
    """sec_edgar.list_filings/latest_filing's default forms -- 20-F
    included (a foreign private issuer's 10-K equivalent; a live
    spot-check found most well-known FPIs file 20-F/6-K, never 10-K/10-Q
    at all), 6-K deliberately excluded (too heterogeneous -- routine
    disclosures mixed in with actual interim financials)."""

    def test_20f_included_by_default(self):
        submissions = _submissions(1, ["10-K", "20-F", "8-K"])
        forms = [f["form"] for f in sec_edgar.list_filings(submissions)]
        self.assertEqual(forms, ["10-K", "20-F"])

    def test_6k_excluded_by_default(self):
        submissions = _submissions(1, ["20-F", "6-K"])
        forms = [f["form"] for f in sec_edgar.list_filings(submissions)]
        self.assertEqual(forms, ["20-F"])

    def test_latest_filing_finds_a_20f_when_no_10k_exists(self):
        submissions = _submissions(1, ["20-F"])
        result = sec_edgar.latest_filing(submissions)
        self.assertIsNotNone(result)


class AnnualValuesAndLatestEntryTests(TestCase):
    """presentation.annual_values/latest_entry -- both bugs a live check
    against NBIS (Nebius Group N.V., a real 20-F filer reporting under
    us-gaap) surfaced: annual_values() only ever accepted form="10-K",
    silently discarding a real, correctly-tagged 20-F filer's entire
    financial history even though the raw XBRL data was fetched fine;
    both functions picked whichever currency key in `units` happened to
    come first in the dict, which is wrong the moment more than one
    currency is present (confirmed live: NBIS's Revenues concept has
    units {"RUB": [...], "USD": [...]} in that exact order -- legacy
    Yandex-era RUB reporting predates its USD-only present-day figures)."""

    def test_10k_entries_still_counted(self):
        concept = {"units": {"USD": [
            {"form": "10-K", "fp": "FY", "fy": 2024, "val": 100},
        ]}}
        self.assertEqual(annual_values(concept), {2024: 100})

    def test_20f_entries_now_counted(self):
        concept = {"units": {"USD": [
            {"form": "20-F", "fp": "FY", "fy": 2024, "val": 100},
        ]}}
        self.assertEqual(annual_values(concept), {2024: 100})

    def test_other_forms_still_excluded(self):
        concept = {"units": {"USD": [
            {"form": "8-K", "fp": "FY", "fy": 2024, "val": 100},
            {"form": "6-K", "fp": "FY", "fy": 2024, "val": 200},
        ]}}
        self.assertEqual(annual_values(concept), {})

    def test_non_fy_entries_excluded(self):
        concept = {"units": {"USD": [
            {"form": "10-Q", "fp": "Q1", "fy": 2024, "val": 100},
        ]}}
        self.assertEqual(annual_values(concept), {})

    def test_usd_preferred_over_another_currency_present_first(self):
        # Dict insertion order matches NBIS's real Revenues payload: RUB
        # entries (legacy) inserted before USD (current).
        concept = {"units": {
            "RUB": [{"form": "20-F", "fp": "FY", "fy": 2024, "val": 999999}],
            "USD": [{"form": "20-F", "fp": "FY", "fy": 2024, "val": 100}],
        }}
        self.assertEqual(annual_values(concept), {2024: 100})

    def test_single_non_currency_unit_still_works(self):
        # Shares outstanding: unit is "shares", not a currency -- USD
        # preference must not break the case where USD was never an option.
        concept = {"units": {"shares": [
            {"form": "10-K", "fp": "FY", "fy": 2024, "val": 5000},
        ]}}
        self.assertEqual(annual_values(concept), {2024: 5000})

    def test_none_concept_returns_empty_dict(self):
        self.assertEqual(annual_values(None), {})

    def test_latest_entry_prefers_usd_over_another_currency_present_first(self):
        concept = {"units": {
            "RUB": [{"val": 999999, "end": "2024-12-31", "form": "20-F"}],
            "USD": [{"val": 100, "end": "2024-12-31", "form": "20-F"}],
        }}
        self.assertEqual(latest_entry(concept)["val"], 100)

    def test_latest_entry_none_concept_returns_none(self):
        self.assertIsNone(latest_entry(None))


class BuildReportTwentyFFilerTests(TestCase):
    """build_report end-to-end for a 20-F filer reporting under us-gaap
    (NBIS's real shape) -- guards the whole path, not just annual_values
    in isolation: a revenue snapshot with only 20-F/FY entries should
    flow through to fiscal_years/financials_by_fy/derived, not come back
    as the empty-everything shell a company that's never been screened
    would show."""

    def setUp(self):
        self.company = Company.objects.create(ticker="FPITEST", cik=9999999)

    def test_20f_revenue_reaches_fiscal_years_and_derived(self):
        CompanySnapshot.objects.create(
            company=self.company, data_type="concept:us-gaap:Revenues",
            raw_json={"units": {"USD": [
                {"form": "20-F", "fp": "FY", "fy": 2023, "val": 1_000_000},
                {"form": "20-F", "fp": "FY", "fy": 2024, "val": 1_500_000},
            ]}},
        )
        CompanySnapshot.objects.create(
            company=self.company, data_type="profile:fmp",
            raw_json={"price": 10.0, "market_cap": 15_000_000},
        )
        report = build_report(self.company)
        self.assertEqual(report["fiscal_years"], [2023, 2024])
        self.assertEqual(report["financials_by_fy"]["revenue"], {2023: 1_000_000, 2024: 1_500_000})
        self.assertAlmostEqual(report["derived"]["revenue_growth_pct_by_fy"][2024], 0.5)
        self.assertEqual(report["derived"]["ps_trailing"], 10.0)  # 15M market cap / 1.5M FY2024 revenue
        self.assertTrue(report["screened"])


class RunScreenAiFallbackTriggerTests(TestCase):
    """screening.py::run_screen -- xbrl_tag_mapping.apply_fallback_mapping
    must trigger ONLY when the primary us-gaap revenue lookup finds
    nothing at all, never for the standard case, so a normal 10-K/
    us-gaap company incurs zero extra LLM cost/latency (the ~30%
    "standard" case from the 10-ticker spot-check that motivated this
    whole feature)."""

    def setUp(self):
        self.company = Company.objects.create(ticker="TRIGGERTEST", cik=6666666)

    def _run_screen(self, revenue_concept_data):
        def fake_get_concept(company, tag, max_age_days=1.0, taxonomy="us-gaap"):
            return revenue_concept_data if tag in REVENUE_TAGS else None

        with patch("companies.services.screening.sec_edgar.resolve_company", return_value=self.company), \
             patch("companies.services.screening.sec_edgar.get_submissions", return_value={}), \
             patch("companies.services.screening.sec_edgar.latest_filing", return_value=None), \
             patch("companies.services.screening.sec_edgar.get_concept", side_effect=fake_get_concept), \
             patch("companies.services.screening.fmp.get_profile", return_value=None), \
             patch("companies.services.screening.fmp.get_shares_float", return_value=None), \
             patch("companies.services.screening.xbrl_tag_mapping.apply_fallback_mapping") as mock_fallback:
            run_screen("TRIGGERTEST")
        return mock_fallback

    def test_standard_case_does_not_trigger_fallback(self):
        mock_fallback = self._run_screen(revenue_concept_data={"units": {"USD": []}})
        mock_fallback.assert_not_called()

    def test_no_revenue_data_triggers_fallback(self):
        mock_fallback = self._run_screen(revenue_concept_data=None)
        mock_fallback.assert_called_once_with(self.company)


def _facts_payload(taxonomy_tags: dict[str, list[str]]) -> dict:
    return {"facts": {taxonomy: {tag: {} for tag in tags} for taxonomy, tags in taxonomy_tags.items()}}


def _fallback_concept_data(*, usd=True, unit="EUR", filed="2026-01-01"):
    entry = {"form": "20-F", "fp": "FY", "fy": 2025, "val": 100, "filed": filed}
    return {"units": {"USD": [entry]}} if usd else {"units": {unit: [entry]}}


def _empty_mapping(**overrides) -> "xbrl_tag_mapping._ConceptMapping":
    fields = [
        "revenue", "gross_profit", "net_loss", "operating_cash_flow", "capex",
        "stock_based_comp", "cash", "debt_current", "debt_noncurrent", "shares_outstanding",
    ]
    defaults = {f: None for f in fields}
    defaults.update(overrides)
    return xbrl_tag_mapping._ConceptMapping(**defaults)


@override_settings(OPENAI_API_KEY="test-key")
class XbrlTagMappingTests(TestCase):
    """xbrl_tag_mapping.apply_fallback_mapping -- taxonomy detection and
    every guardrail a live 10-ticker spot-check demanded: single tag per
    concept, USD-only (except shares_outstanding), recency, hallucination
    rejection, and long-lived caching (including of a negative result)."""

    def setUp(self):
        self.company = Company.objects.create(ticker="IFRSTEST", cik=7777777)

    def _mock_parse(self, mock_openai_cls, parsed):
        mock_openai_cls.return_value.responses.parse.return_value = SimpleNamespace(output_parsed=parsed)

    def test_no_non_financial_taxonomy_caches_empty_mapping_without_calling_openai(self):
        facts = _facts_payload({"us-gaap": ["Revenues"], "dei": ["EntityCommonStockSharesOutstanding"]})
        with patch("companies.services.xbrl_tag_mapping.sec_edgar.get_company_facts", return_value=facts), \
             patch("companies.services.xbrl_tag_mapping.OpenAI") as mock_openai_cls:
            xbrl_tag_mapping.apply_fallback_mapping(self.company)
        mock_openai_cls.assert_not_called()
        snapshot = CompanySnapshot.objects.get(company=self.company, data_type=xbrl_tag_mapping.MAPPING_SNAPSHOT_TYPE)
        self.assertEqual(snapshot.raw_json, {"taxonomy": None, "mapping": {}})

    def test_valid_mapping_resolves_and_caches(self):
        facts = _facts_payload({"ifrs-full": ["Revenue", "ProfitLoss"]})
        parsed = _empty_mapping(revenue="Revenue", net_loss="ProfitLoss")
        with patch("companies.services.xbrl_tag_mapping.sec_edgar.get_company_facts", return_value=facts), \
             patch("companies.services.xbrl_tag_mapping.OpenAI") as mock_openai_cls, \
             patch("companies.services.xbrl_tag_mapping.sec_edgar.get_concept", return_value=_fallback_concept_data(usd=True)):
            self._mock_parse(mock_openai_cls, parsed)
            xbrl_tag_mapping.apply_fallback_mapping(self.company)
        snapshot = CompanySnapshot.objects.get(company=self.company, data_type=xbrl_tag_mapping.MAPPING_SNAPSHOT_TYPE)
        self.assertEqual(
            snapshot.raw_json, {"taxonomy": "ifrs-full", "mapping": {"revenue": "Revenue", "net_loss": "ProfitLoss"}},
        )

    def test_hallucinated_tag_not_in_list_is_discarded(self):
        facts = _facts_payload({"ifrs-full": ["Revenue"]})
        parsed = _empty_mapping(revenue="NotARealTag")
        with patch("companies.services.xbrl_tag_mapping.sec_edgar.get_company_facts", return_value=facts), \
             patch("companies.services.xbrl_tag_mapping.OpenAI") as mock_openai_cls:
            self._mock_parse(mock_openai_cls, parsed)
            xbrl_tag_mapping.apply_fallback_mapping(self.company)
        snapshot = CompanySnapshot.objects.get(company=self.company, data_type=xbrl_tag_mapping.MAPPING_SNAPSHOT_TYPE)
        self.assertEqual(snapshot.raw_json["mapping"], {})

    def test_non_usd_tag_discarded_for_a_currency_concept(self):
        facts = _facts_payload({"ifrs-full": ["Revenue"]})
        parsed = _empty_mapping(revenue="Revenue")
        with patch("companies.services.xbrl_tag_mapping.sec_edgar.get_company_facts", return_value=facts), \
             patch("companies.services.xbrl_tag_mapping.OpenAI") as mock_openai_cls, \
             patch("companies.services.xbrl_tag_mapping.sec_edgar.get_concept", return_value=_fallback_concept_data(usd=False)):
            self._mock_parse(mock_openai_cls, parsed)
            xbrl_tag_mapping.apply_fallback_mapping(self.company)
        snapshot = CompanySnapshot.objects.get(company=self.company, data_type=xbrl_tag_mapping.MAPPING_SNAPSHOT_TYPE)
        self.assertEqual(snapshot.raw_json["mapping"], {})

    def test_non_currency_concept_accepted_without_usd(self):
        # shares_outstanding's unit is "shares" -- never has a "USD" key
        # to require in the first place.
        facts = _facts_payload({"ifrs-full": ["NumberOfShares"]})
        parsed = _empty_mapping(shares_outstanding="NumberOfShares")
        shares_data = {"units": {"shares": [{"form": "20-F", "fp": "FY", "fy": 2025, "val": 100, "filed": "2026-01-01"}]}}
        with patch("companies.services.xbrl_tag_mapping.sec_edgar.get_company_facts", return_value=facts), \
             patch("companies.services.xbrl_tag_mapping.OpenAI") as mock_openai_cls, \
             patch("companies.services.xbrl_tag_mapping.sec_edgar.get_concept", return_value=shares_data):
            self._mock_parse(mock_openai_cls, parsed)
            xbrl_tag_mapping.apply_fallback_mapping(self.company)
        snapshot = CompanySnapshot.objects.get(company=self.company, data_type=xbrl_tag_mapping.MAPPING_SNAPSHOT_TYPE)
        self.assertEqual(snapshot.raw_json["mapping"], {"shares_outstanding": "NumberOfShares"})

    def test_stale_tag_discarded(self):
        facts = _facts_payload({"ifrs-full": ["Revenue"]})
        parsed = _empty_mapping(revenue="Revenue")
        old_filed = (date.today() - timedelta(days=xbrl_tag_mapping._MAX_TAG_AGE_DAYS + 30)).isoformat()
        with patch("companies.services.xbrl_tag_mapping.sec_edgar.get_company_facts", return_value=facts), \
             patch("companies.services.xbrl_tag_mapping.OpenAI") as mock_openai_cls, \
             patch(
                 "companies.services.xbrl_tag_mapping.sec_edgar.get_concept",
                 return_value=_fallback_concept_data(usd=True, filed=old_filed),
             ):
            self._mock_parse(mock_openai_cls, parsed)
            xbrl_tag_mapping.apply_fallback_mapping(self.company)
        snapshot = CompanySnapshot.objects.get(company=self.company, data_type=xbrl_tag_mapping.MAPPING_SNAPSHOT_TYPE)
        self.assertEqual(snapshot.raw_json["mapping"], {})

    def test_fresh_cache_skips_openai_and_company_facts_entirely(self):
        CompanySnapshot.objects.create(
            company=self.company, data_type=xbrl_tag_mapping.MAPPING_SNAPSHOT_TYPE,
            raw_json={"taxonomy": "ifrs-full", "mapping": {"revenue": "Revenue"}},
        )
        with patch("companies.services.xbrl_tag_mapping.OpenAI") as mock_openai_cls, \
             patch("companies.services.xbrl_tag_mapping.sec_edgar.get_company_facts") as mock_facts:
            xbrl_tag_mapping.apply_fallback_mapping(self.company)
        mock_openai_cls.assert_not_called()
        mock_facts.assert_not_called()

    @override_settings(OPENAI_API_KEY="")
    def test_missing_openai_key_does_not_cache_a_negative_result(self):
        facts = _facts_payload({"ifrs-full": ["Revenue"]})
        with patch("companies.services.xbrl_tag_mapping.sec_edgar.get_company_facts", return_value=facts):
            xbrl_tag_mapping.apply_fallback_mapping(self.company)
        self.assertFalse(
            CompanySnapshot.objects.filter(company=self.company, data_type=xbrl_tag_mapping.MAPPING_SNAPSHOT_TYPE).exists()
        )


class BuildReportAiFallbackTests(TestCase):
    """build_report's use of an already-resolved ai_tag_mapping snapshot
    (xbrl_tag_mapping.apply_fallback_mapping's own resolution logic is
    covered separately, above) -- the fallback must only fill in a
    concept that's genuinely empty, never override real us-gaap data,
    and must be visible via ai_mapped_concepts rather than silently
    indistinguishable from a directly-tagged figure."""

    def setUp(self):
        self.company = Company.objects.create(ticker="FALLBACKTEST", cik=5555555)
        CompanySnapshot.objects.create(
            company=self.company, data_type=xbrl_tag_mapping.MAPPING_SNAPSHOT_TYPE,
            raw_json={"taxonomy": "ifrs-full", "mapping": {"revenue": "Revenue", "cash": "CashAndCashEquivalents"}},
        )

    def test_empty_primary_concept_falls_back_to_mapped_tag(self):
        CompanySnapshot.objects.create(
            company=self.company, data_type="concept:ifrs-full:Revenue",
            raw_json={"units": {"USD": [{"form": "20-F", "fp": "FY", "fy": 2024, "val": 5_000_000}]}},
        )
        report = build_report(self.company)
        self.assertEqual(report["financials_by_fy"]["revenue"], {2024: 5_000_000})
        self.assertIn("revenue", report["ai_mapped_concepts"])

    def test_real_us_gaap_data_is_never_overridden_by_the_fallback(self):
        CompanySnapshot.objects.create(
            company=self.company, data_type="concept:us-gaap:Revenues",
            raw_json={"units": {"USD": [{"form": "10-K", "fp": "FY", "fy": 2024, "val": 9_000_000}]}},
        )
        CompanySnapshot.objects.create(
            company=self.company, data_type="concept:ifrs-full:Revenue",
            raw_json={"units": {"USD": [{"form": "20-F", "fp": "FY", "fy": 2024, "val": 5_000_000}]}},
        )
        report = build_report(self.company)
        self.assertEqual(report["financials_by_fy"]["revenue"], {2024: 9_000_000})
        self.assertNotIn("revenue", report["ai_mapped_concepts"])

    def test_concept_with_no_mapping_entry_stays_empty_not_an_error(self):
        report = build_report(self.company)
        self.assertEqual(report["financials_by_fy"]["gross_profit"], {})
        self.assertNotIn("gross_profit", report["ai_mapped_concepts"])

    def test_mapped_tag_with_no_actual_data_cached_yet_stays_empty(self):
        # ai_tag_mapping says "cash" -> CashAndCashEquivalents, but that
        # concept's own snapshot was never fetched/cached -- must not KeyError.
        report = build_report(self.company)
        self.assertEqual(report["financials_by_fy"]["cash"], {})
        self.assertNotIn("cash", report["ai_mapped_concepts"])

    def test_no_ai_mapping_snapshot_at_all_is_the_ordinary_unscreened_shape(self):
        other_company = Company.objects.create(ticker="NEVERSCREENED", cik=4444444)
        report = build_report(other_company)
        self.assertEqual(report["ai_mapped_concepts"], [])
        self.assertFalse(report["screened"])


class BuildReportSectorTests(TestCase):
    """companies.services.presentation.build_report -- surfaces
    sector/industry from the cached profile:fmp snapshot, the same way it
    already surfaces price/market_cap. league/tools.py and
    assistant/tools.py's get_company_report tool dumps this report
    wholesale, so no tool-level change was needed to expose this."""

    def setUp(self):
        self.company = Company.objects.create(ticker="RKLB", cik=1819994)

    def test_sector_and_industry_flow_through_to_the_report(self):
        CompanySnapshot.objects.create(
            company=self.company, data_type="profile:fmp",
            raw_json={"price": 25.5, "market_cap": 12_000_000_000, "sector": "Industrials", "industry": "Aerospace & Defense"},
        )
        report = build_report(self.company)
        self.assertEqual(report["sector"], "Industrials")
        self.assertEqual(report["industry"], "Aerospace & Defense")

    def test_unscreened_company_has_null_sector_not_an_error(self):
        report = build_report(self.company)
        self.assertIsNone(report["sector"])
        self.assertIsNone(report["industry"])


@override_settings(TRADIER_API_TOKEN="test-token")
class GetIntradayUrlConstructionTests(TestCase):
    """companies.services.tradier.get_intraday -- URL construction, pinned
    against a bug found in a live supervised check (2026-08-29, production
    Tradier account, AAPL): passing a bare date for start/end made Tradier
    treat it as a near-instant point instead of a whole day, silently
    returning ~1 bar instead of a real session (confirmed: session_filter
    ="open" with bare-date start/end returned 0 bars; the same call with
    both expanded to a full-day datetime range returned exactly the 78
    bars spanning the real 09:30-15:55 ET session). Fixed by expanding to
    "{date} 00:00"/"{date} 23:59" before sending -- this test exists so
    that fix can't silently regress back to the bare-date form."""

    def test_bare_date_start_end_are_expanded_to_a_full_day_range(self):
        mock_resp = MagicMock()
        mock_resp.json.return_value = {"series": {"data": []}}
        with patch("companies.services.tradier.http.get", return_value=mock_resp) as mock_get:
            tradier.get_intraday("AAPL", start=date(2026, 8, 28), end=date(2026, 8, 28))
        called_url = mock_get.call_args[0][0]
        self.assertIn("start=2026-08-28 00:00", called_url)
        self.assertIn("end=2026-08-28 23:59", called_url)

    def test_session_filter_defaults_to_open_not_tradiers_own_all_default(self):
        # Also confirmed live: session_filter="open" with a full-day range
        # returns exactly the regular session (78 5min bars, 09:30-15:55
        # ET, none outside it) -- the cutoff is exact, not approximate.
        mock_resp = MagicMock()
        mock_resp.json.return_value = {"series": {"data": []}}
        with patch("companies.services.tradier.http.get", return_value=mock_resp) as mock_get:
            tradier.get_intraday("AAPL")
        called_url = mock_get.call_args[0][0]
        self.assertIn("session_filter=open", called_url)

    def test_session_filter_can_be_overridden_for_extended_hours(self):
        mock_resp = MagicMock()
        mock_resp.json.return_value = {"series": {"data": []}}
        with patch("companies.services.tradier.http.get", return_value=mock_resp) as mock_get:
            tradier.get_intraday("AAPL", session_filter="all")
        called_url = mock_get.call_args[0][0]
        self.assertIn("session_filter=all", called_url)

    def test_unconfigured_token_returns_none_without_a_request(self):
        with override_settings(TRADIER_API_TOKEN=""), \
             patch("companies.services.tradier.http.get") as mock_get:
            result = tradier.get_intraday("AAPL")
        self.assertIsNone(result)
        mock_get.assert_not_called()
