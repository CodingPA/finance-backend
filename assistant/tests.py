import json
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import patch

import openai
from anthropic.lib.tools import ToolError
from django.test import TestCase, override_settings
from openai.types.responses import Response
from rest_framework_simplejwt.tokens import RefreshToken

from accounts.models import User

from .models import AssistantModel, Conversation, Message
from .tasks import ask_assistant


def _openai_response(
    output=None, status="completed", input_tokens=100, output_tokens=50,
    cached_tokens=0, cache_write_tokens=0, incomplete_details=None, id_="resp_1", error=None,
):
    # Same fixture shape as league/tests.py's own _openai_response --
    # duplicated rather than imported since league/assistant don't
    # otherwise share test fixtures, and this is small enough that
    # cross-app test-only coupling isn't worth it for one helper.
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
    if error is not None:
        data["error"] = error
    return Response.model_validate(data)


def _message_item(text="Here's what I found."):
    return {
        "type": "message", "id": "msg_1", "role": "assistant", "status": "completed",
        "content": [{"type": "output_text", "text": text, "annotations": []}],
    }


def _function_call_item(name, arguments="{}", call_id="call_1"):
    return {"type": "function_call", "id": "fc_1", "call_id": call_id, "name": name, "arguments": arguments, "status": "completed"}


def _text_delta(delta):
    """A response.output_text.delta stream event -- SimpleNamespace, not
    the real ResponseTextDeltaEvent pydantic model, since _run_openai_turn
    only ever reads .type/.delta off these; confirmed live (2026-09-19)
    that's the real event shape, no need to re-validate the SDK's own
    schema here too."""
    return SimpleNamespace(type="response.output_text.delta", delta=delta)


def _terminal_event(response, kind="completed"):
    """A response.completed/.failed/.incomplete stream event wrapping the
    same Response object client.responses.create() returns non-streaming
    (confirmed live) -- _run_openai_turn only reads .type/.response."""
    return SimpleNamespace(type=f"response.{kind}", response=response)


def _openai_stream(*text_deltas, response, kind="completed"):
    """One full simulated stream for one round: zero or more text-delta
    events (a tool-only round has none, confirmed live) followed by
    exactly one terminal event -- mock_client.responses.create's
    return_value/side_effect is this list directly (an ordinary list is
    iterable, same as the real Stream object _run_openai_turn iterates)."""
    return [_text_delta(d) for d in text_deltas] + [_terminal_event(response, kind)]


class AssistantModelListViewTests(TestCase):
    """GET /api/assistant/models/ is scoped to AssistantModel.CHAT_PROVIDERS
    (Anthropic + OpenAI) -- assistant/tasks.py's call loop (ask_assistant)
    only knows how to run those two (_run_anthropic_turn/_run_openai_turn).
    Gemini stays excluded: league's AI competitors support it
    (league/services/llm_gemini.py), but the chat assistant doesn't -- a
    Gemini row showing up here would be selectable and then hard-fail the
    moment it's picked."""

    def setUp(self):
        self.user = User.objects.create_user(email="chatuser@example.com", password="testpass123")
        access_token = RefreshToken.for_user(self.user).access_token
        self.client.defaults["HTTP_AUTHORIZATION"] = f"Bearer {access_token}"

    def test_openai_models_included_in_chat_picker(self):
        response = self.client.get("/api/assistant/models/")
        self.assertEqual(response.status_code, 200)
        body = response.json()
        model_ids = [m["model_id"] for m in body]
        self.assertIn("claude-sonnet-5", model_ids)
        self.assertIn("gpt-5.6-sol", model_ids)
        sol_row = next(m for m in body if m["model_id"] == "gpt-5.6-sol")
        self.assertEqual(sol_row["provider"], "openai")

    def test_gemini_models_excluded_from_chat_picker(self):
        response = self.client.get("/api/assistant/models/")
        self.assertEqual(response.status_code, 200)
        model_ids = [m["model_id"] for m in response.json()]
        self.assertNotIn("gemini-3.1-pro-preview-customtools", model_ids)

    def test_starting_chat_with_openai_model_id_accepted(self):
        from assistant.models import Conversation
        conversation = Conversation.objects.create(user=self.user)
        with patch("assistant.views.ask_assistant.delay") as mock_delay:
            response = self.client.post(
                f"/api/assistant/conversations/{conversation.id}/messages/",
                {"message": "hi", "model_id": "gpt-5.6-sol"},
            )
        self.assertEqual(response.status_code, 202, response.content)
        mock_delay.assert_called_once()

    def test_starting_chat_with_gemini_model_id_rejected(self):
        from assistant.models import Conversation
        conversation = Conversation.objects.create(user=self.user)
        response = self.client.post(
            f"/api/assistant/conversations/{conversation.id}/messages/",
            {"message": "hi", "model_id": "gemini-3.1-pro-preview-customtools"},
        )
        self.assertEqual(response.status_code, 400)


class OpenAIChatTurnTests(TestCase):
    """assistant/tasks.py::_run_openai_turn (via ask_assistant) -- the
    hand-rolled, STREAMING Responses API loop. Mirrors league/tests.py's
    OpenAITurnLoopTests for llm_openai.run_openai_turn's non-streaming
    version, but every mocked round here is a list of stream events
    (_openai_stream) ending in one response.completed/.failed/.incomplete
    event carrying the same Response object client.responses.create()
    returns non-streaming -- confirmed live (2026-09-19) that's the real
    shape. Also covers billed_input reduced by cache reads before
    cost_for (see that function's own comment) since OpenAI folds cached
    tokens into input_tokens rather than excluding them the way
    Anthropic's usage does."""

    def setUp(self):
        self.user = User.objects.create_user(email="gptchat@example.com", password="testpass123")
        self.conversation = Conversation.objects.create(user=self.user)
        self.model = AssistantModel.objects.create(
            model_id="test-gpt-chat", display_name="Test GPT Chat", provider=AssistantModel.PROVIDER_OPENAI,
            input_price_per_million=Decimal("5.00"), output_price_per_million=Decimal("30.00"),
        )
        self.user_message = Message.objects.create(
            conversation=self.conversation, role=Message.ROLE_USER, text="What's my portfolio worth?",
        )

    def _ask(self):
        ask_assistant(str(self.user.id), self.conversation.id, self.user_message.id, self.model.model_id)

    def test_streams_token_events_then_completes(self):
        response = _openai_response(output=[_message_item("Hello there!")])
        with patch("assistant.tasks.OpenAI") as mock_openai_cls, \
             patch("assistant.tasks.send_to_user") as mock_send:
            mock_openai_cls.return_value.responses.create.return_value = _openai_stream(
                "Hello", " there", "!", response=response,
            )
            self._ask()

        token_calls = [c for c in mock_send.call_args_list if c.args[1] == "assistant.token"]
        self.assertEqual([c.kwargs["text"] for c in token_calls], ["Hello", " there", "!"])
        complete_calls = [c for c in mock_send.call_args_list if c.args[1] == "assistant.complete"]
        self.assertEqual(complete_calls[0].kwargs["text"], "Hello there!")
        message = Message.objects.get(conversation=self.conversation, role=Message.ROLE_ASSISTANT)
        self.assertEqual(message.text, "Hello there!")

    def test_tool_call_then_final_answer(self):
        # list_tracked_subreddits needs no external mocking (plain local
        # DB read) -- keeps this test about the turn loop's own mechanics,
        # not any one tool's business logic (already covered by that
        # tool's own tests). The tool-call round has NO text deltas
        # (confirmed live: a tool-only round emits zero response.output_
        # text.delta events), matching _openai_stream()'s default.
        streams = [
            _openai_stream(response=_openai_response(output=[_function_call_item("list_tracked_subreddits")])),
            _openai_stream(
                "You aren't watching any subreddits.",
                response=_openai_response(output=[_message_item("You aren't watching any subreddits.")], id_="resp_2"),
            ),
        ]
        with patch("assistant.tasks.OpenAI") as mock_openai_cls:
            mock_client = mock_openai_cls.return_value
            mock_client.responses.create.side_effect = streams
            self._ask()

        self.assertEqual(mock_client.responses.create.call_count, 2)
        message = Message.objects.get(conversation=self.conversation, role=Message.ROLE_ASSISTANT)
        self.assertEqual(message.text, "You aren't watching any subreddits.")
        self.assertEqual(message.tool_calls, ["list_tracked_subreddits"])
        self.assertEqual(message.input_tokens, 200)   # 100 + 100
        self.assertEqual(message.output_tokens, 100)  # 50 + 50

    def test_cache_read_tokens_reduce_billed_input_not_raw_input_tokens(self):
        # cost_for's cache multipliers are Anthropic-cache-shaped -- OpenAI
        # cache reads must come out of input_tokens before costing (see
        # _run_openai_turn's own comment), while the raw totals still land
        # on the Message row unadjusted, for audit fidelity.
        response = _openai_response(output=[_message_item("ok")], input_tokens=1000, cached_tokens=400, output_tokens=50)
        with patch("assistant.tasks.OpenAI") as mock_openai_cls:
            mock_openai_cls.return_value.responses.create.return_value = _openai_stream("ok", response=response)
            self._ask()

        message = Message.objects.get(conversation=self.conversation, role=Message.ROLE_ASSISTANT)
        self.assertEqual(message.input_tokens, 1000)
        self.assertEqual(message.cache_read_input_tokens, 400)
        # billed = (1000 - 400) * 5/1e6 + 50 * 30/1e6 = 0.003 + 0.0015
        self.assertEqual(message.cost_usd, Decimal("0.003") + Decimal("0.0015"))

    def test_loop_terminates_at_max_iterations(self):
        from .tasks import MAX_TOOL_ITERATIONS
        always_calling = _openai_response(output=[_function_call_item("list_tracked_subreddits")])
        with patch("assistant.tasks.OpenAI") as mock_openai_cls:
            mock_openai_cls.return_value.responses.create.return_value = _openai_stream(response=always_calling)
            self._ask()
        self.assertEqual(mock_openai_cls.return_value.responses.create.call_count, MAX_TOOL_ITERATIONS)

    def test_incomplete_status_with_no_text_records_failure(self):
        response = _openai_response(status="incomplete", incomplete_details={"reason": "max_output_tokens"})
        with patch("assistant.tasks.OpenAI") as mock_openai_cls:
            mock_openai_cls.return_value.responses.create.return_value = _openai_stream(response=response, kind="incomplete")
            self._ask()
        message = Message.objects.get(conversation=self.conversation, role=Message.ROLE_ASSISTANT)
        self.assertIn("didn't produce a response", message.text)

    def test_failed_status_records_the_providers_error_message(self):
        response = _openai_response(status="failed", error={"code": "server_error", "message": "Something broke upstream."})
        with patch("assistant.tasks.OpenAI") as mock_openai_cls:
            mock_openai_cls.return_value.responses.create.return_value = _openai_stream(response=response, kind="failed")
            self._ask()
        message = Message.objects.get(conversation=self.conversation, role=Message.ROLE_ASSISTANT)
        self.assertIn("Something broke upstream.", message.text)

    def test_stream_with_no_terminal_event_records_failure(self):
        # A dropped connection mid-stream -- no response.completed/.failed/
        # .incomplete ever arrived. Must not crash on response=None.
        with patch("assistant.tasks.OpenAI") as mock_openai_cls:
            mock_openai_cls.return_value.responses.create.return_value = [_text_delta("partial")]
            self._ask()
        message = Message.objects.get(conversation=self.conversation, role=Message.ROLE_ASSISTANT)
        self.assertIn("ended unexpectedly", message.text)

    def test_missing_api_key_records_failure_without_calling_openai(self):
        with patch("assistant.tasks.settings.OPENAI_API_KEY", ""), \
             patch("assistant.tasks.OpenAI") as mock_openai_cls:
            self._ask()
        mock_openai_cls.assert_not_called()
        message = Message.objects.get(conversation=self.conversation, role=Message.ROLE_ASSISTANT)
        self.assertIn("OPENAI_API_KEY", message.text)

    def test_api_status_error_records_failure(self):
        import httpx
        request = httpx.Request("POST", "https://api.openai.com/v1/responses")
        response = httpx.Response(401, request=request)
        exc = openai.AuthenticationError("Invalid API key.", response=response, body=None)
        with patch("assistant.tasks.OpenAI") as mock_openai_cls:
            mock_openai_cls.return_value.responses.create.side_effect = exc
            self._ask()
        message = Message.objects.get(conversation=self.conversation, role=Message.ROLE_ASSISTANT)
        self.assertIn("Invalid API key.", message.text)


class GetPriceHistoryToolTests(TestCase):
    """The chat-assistant-facing get_price_history tool (assistant/
    tools.py) -- same tool as league/tools.py's version (league/tests.py's
    GetPriceHistoryToolTests), mirrored here for parity."""

    def setUp(self):
        self.user = User.objects.create_user(email="researcher@example.com", password="testpass123")
        self.conversation = Conversation.objects.create(user=self.user)

    def _tool(self):
        from .tools import build_tools
        tools = build_tools(self.user, self.conversation)
        return {t.name: t for t in tools}["get_price_history"]

    def test_returns_bars(self):
        bars = [{"date": "2026-08-20", "open": 100, "high": 102, "low": 99, "close": 101, "volume": 500}]
        with patch("companies.services.technicals.get_price_history", return_value=bars) as mock_history:
            result = json.loads(self._tool().call({"ticker": "rklb", "days": 10}))
        self.assertEqual(result, bars)
        mock_history.assert_called_once_with("rklb", days=10)

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


@override_settings(PRICING_PROVIDER="tradier", TRADIER_API_TOKEN="test-token")
class GetIntradayCandlesToolTests(TestCase):
    """The chat-assistant-facing get_intraday_candles tool (assistant/
    tools.py) -- same tool as league/tools.py's version, mirrored here
    for parity. Returns an envelope ({"bars": [...], "truncated": ...}),
    not a bare list, so a 1min call that silently dropped most of the
    session is distinguishable from one that returned everything."""

    def setUp(self):
        self.user = User.objects.create_user(email="researcher2@example.com", password="testpass123")
        self.conversation = Conversation.objects.create(user=self.user)

    def _tools(self):
        from .tools import build_tools
        return build_tools(self.user, self.conversation)

    def _tool(self):
        return {t.name: t for t in self._tools()}["get_intraday_candles"]

    def test_returns_bars_in_an_envelope(self):
        bars = [{"time": "2026-08-20T09:30:00", "open": 100, "high": 101, "low": 99.5, "close": 100.5, "volume": 1000}]
        with patch("companies.services.tradier.get_intraday", return_value=bars) as mock_intraday:
            result = json.loads(self._tool().call({"ticker": "rklb", "interval": "1min"}))
        self.assertEqual(result["bars"], bars)
        self.assertFalse(result["truncated"])
        mock_intraday.assert_called_once_with("rklb", interval="1min")

    def test_invalid_interval_raises_without_calling_tradier(self):
        with patch("companies.services.tradier.get_intraday") as mock_intraday:
            with self.assertRaises(ToolError):
                self._tool().call({"ticker": "RKLB", "interval": "1hour"})
        mock_intraday.assert_not_called()

    @override_settings(PRICING_PROVIDER="steady")
    def test_non_tradier_provider_is_not_even_registered(self):
        """build_tools() now filters this out entirely on a non-Tradier
        deployment (league/tools.py's build_league_tools() mirrors the
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
        self.assertEqual(result["bars"][-1], bars[-1])
        self.assertTrue(result["truncated"])


@override_settings(BRAVE_API_KEY="test-brave-key")
class SearchWebToolTests(TestCase):
    """The chat-assistant-facing search_web tool (assistant/tools.py) --
    same tool as league/tools.py's version, mirrored here for parity.
    Brave Search-backed generic open-web search."""

    def setUp(self):
        self.user = User.objects.create_user(email="searcher@example.com", password="testpass123")
        self.conversation = Conversation.objects.create(user=self.user)

    def _tool(self):
        from .tools import build_tools
        tools = build_tools(self.user, self.conversation)
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


@override_settings(
    STEADY_API_TOKEN="test-token", OPENAI_API_KEY="test-key",
    WEAVIATE_URL="http://test-weaviate:8080", ENABLE_MEDIA=True, BRAVE_API_KEY="test-brave-key",
)
class ToolCapabilityGatingTests(TestCase):
    """build_tools() filters out provider-exclusive tools whose capability
    isn't configured, rather than registering them and letting every call
    raise ToolError (league/tools.py's build_league_tools() mirrors the
    same gate -- see its own ToolCapabilityGatingTests). Covers the tools
    not already pinned by their own class above (GetIntradayCandlesToolTests
    already covers get_intraday_candles). This app has no get_market_news
    tool to gate, unlike league's version.

    Class-level tokens set so "fully configured" is this class's actual
    default, matching the first test below -- individual tests override a
    single one back to "" (or, for ENABLE_MEDIA, False) to test that
    specific absence, relying on override_settings correctly layering
    per-test on top of this. ENABLE_MEDIA=True here only ever affects
    tool-list membership, never a real media.models query -- nothing in
    this class calls search_media_transcripts, only checks whether it's
    registered, so it's safe despite the media app not actually being in
    INSTALLED_APPS for this test process (override_settings can't
    retroactively register an app -- a real functional test of this tool
    would need ENABLE_MEDIA=true set before the test process starts)."""

    def setUp(self):
        self.user = User.objects.create_user(email="gated@example.com", password="testpass123")
        self.conversation = Conversation.objects.create(user=self.user)

    def _tool_names(self):
        from .tools import build_tools
        return {t.name for t in build_tools(self.user, self.conversation)}

    def test_all_gated_tools_present_when_fully_configured(self):
        names = self._tool_names()
        for name in (
            "get_reddit_sentiment", "scan_subreddit_for_tickers", "search_web",
            "search_research_clips", "search_media_transcripts",
        ):
            self.assertIn(name, names)

    @override_settings(BRAVE_API_KEY="")
    def test_search_web_absent_without_brave_key(self):
        self.assertNotIn("search_web", self._tool_names())

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

    @override_settings(WEAVIATE_URL="")
    def test_search_research_clips_absent_without_weaviate(self):
        self.assertNotIn("search_research_clips", self._tool_names())

    @override_settings(OPENAI_API_KEY="")
    def test_search_research_clips_absent_without_openai_key_even_with_weaviate_configured(self):
        """embeddings.embed_text only checks OPENAI_API_KEY, so gating on
        just that would still register a tool that fails on every call
        whenever Weaviate alone is unconfigured -- confirm the reverse
        holds too: OpenAI alone isn't enough without Weaviate either."""
        self.assertNotIn("search_research_clips", self._tool_names())

    @override_settings(WEAVIATE_URL="")
    def test_search_media_transcripts_absent_without_weaviate(self):
        self.assertNotIn("search_media_transcripts", self._tool_names())

    @override_settings(ENABLE_MEDIA=False)
    def test_search_media_transcripts_absent_without_enable_media(self):
        """Weaviate+OpenAI alone aren't enough -- the media app itself is
        opt-in and isn't in INSTALLED_APPS when ENABLE_MEDIA is off, so the
        tool must be gone regardless of what else is configured."""
        self.assertNotIn("search_media_transcripts", self._tool_names())
