import logging

import anthropic
import openai
from celery import shared_task
from django.conf import settings
from openai import OpenAI

from companies.models import Company, TrackedCompany
from companies.services import filing_ingestion, sec_edgar
from companies.services.tool_bridge import openai_tool_defs, run_openai_tool
from realtime.utils import send_to_user

from .models import AssistantModel, Conversation, Message, PendingAction
from .tools import build_tools

logger = logging.getLogger(__name__)

# Hard cap on the tool-call loop for one turn -- read-only tools plus one
# confirmation-only tool shouldn't ever need many rounds; this just
# prevents a runaway loop from a model stuck re-calling tools. Shared by
# both provider branches below.
MAX_TOOL_ITERATIONS = 6
# Matches the Anthropic path's own max_tokens=4096 below -- deliberately
# NOT league's OPENAI_MAX_OUTPUT_TOKENS=12000/reasoning="low"
# (league/services/llm_openai.py): those are tuned for a repeated,
# cost-sensitive AUTOMATED wake-up loop, not an interactive turn a human
# is actively waiting on. Also deliberately no OPENAI_WEB_SEARCH_TOOL
# here (league/tools.py's native-search constant) -- the chat's web
# search capability is Brave's search_web tool (assistant/tools.py),
# uniform across every model; attaching OpenAI's own native search only
# on this path would make chat's capabilities differ by which model you
# picked, for no benefit.
OPENAI_CHAT_MAX_OUTPUT_TOKENS = 4096


def _record_failure(conversation, model, text: str) -> None:
    """Every failure path still gets an assistant-role Message row (not just
    a WebSocket push) -- otherwise the next user turn would follow this one
    directly, two 'user' messages in a row in history, and the Anthropic API
    400s on non-alternating roles."""
    Message.objects.create(conversation=conversation, role=Message.ROLE_ASSISTANT, text=text, model=model)
    send_to_user(conversation.user_id, "assistant.error", conversation_id=conversation.id, message=text)


def _api_content(message: Message) -> str:
    """The text actually sent to Claude for this turn -- message.text plus a
    ticker annotation if this specific turn was scoped to a company.
    Deliberately not stored in message.text itself: that field is what the
    user typed / what the UI shows verbatim, and the annotation is a
    backend-only framing detail, reconstructed the same way on every call
    (including when this message is replayed as history on a later turn).

    This is the documented fallback for models that don't support
    mid-conversation `role: "system"` messages (Claude Sonnet 5, the
    default here, is one of them) -- a plain `<context>` block in the user
    turn instead."""
    if message.company_id:
        return f"<context>This message is about {message.company.ticker} ({message.company.name}).</context>\n\n{message.text}"
    return message.text


def _history_messages(conversation, upto_message_id) -> list[dict]:
    """Last ASSISTANT_HISTORY_LENGTH messages up to and including the turn
    this task was asked to answer (both roles), oldest first -- the shape
    client.messages.create()/count_tokens() expects. Bounded by
    upto_message_id rather than just "whatever's newest" so a user firing
    off a second message before this task starts can't pull an unanswered
    later turn into this one's context (which would also break the
    strict user/assistant role alternation the API requires)."""
    recent = (
        conversation.messages.filter(id__lte=upto_message_id)
        .select_related("company")
        .order_by("-created_at")[: settings.ASSISTANT_HISTORY_LENGTH]
    )
    return [{"role": m.role, "content": _api_content(m)} for m in reversed(recent)]


def _trim_to_fit(client, model: AssistantModel, api_messages: list[dict]) -> list[dict]:
    """Drop the oldest message until the candidate history + new message
    fits under the model's max_input_tokens, or only the newest (current
    turn's) message is left. Uses the real count_tokens endpoint -- not a
    tiktoken-style estimate, which is wrong for Claude models anyway."""
    while len(api_messages) > 1:
        count = client.messages.count_tokens(model=model.model_id, messages=api_messages).input_tokens
        if count <= model.max_input_tokens:
            break
        api_messages = api_messages[1:]
    return api_messages


@shared_task
def ask_assistant(user_id, conversation_id, user_message_id, model_id=None):
    """Answer one turn of a Conversation, giving the model access to the
    tools in tools.py via the SDK's tool-calling loop. Mirrors
    screen_ticker's shape: queued from a view that's already persisted the
    user's message and validated the input, result delivered over the
    user's WebSocket group (docs/websockets.md `assistant.*`).

    Branches on the resolved model's provider -- AssistantModel.
    CHAT_PROVIDERS (Anthropic, OpenAI) are the only two this app's call
    loop actually knows how to run; ConversationMessagesView.post already
    validates model_id against that same set before ever queuing this
    task, so the `else` below is a belt-and-suspenders invariant check,
    not an expected path.

    Takes the specific user_message_id rather than re-reading "the latest
    user message" off the conversation, so there's no ambiguity about which
    turn this task is answering if messages ever get queued out of order."""
    conversation = Conversation.objects.get(id=conversation_id, user_id=user_id)

    try:
        model = AssistantModel.objects.get(model_id=model_id, is_active=True) if model_id else AssistantModel.get_default()
    except AssistantModel.DoesNotExist:
        _record_failure(conversation, None, f"{model_id!r} isn't an available assistant model.")
        return

    if model.provider == AssistantModel.PROVIDER_OPENAI:
        _run_openai_turn(user_id, conversation, user_message_id, model)
    elif model.provider == AssistantModel.PROVIDER_ANTHROPIC:
        _run_anthropic_turn(user_id, conversation, user_message_id, model)
    else:
        _record_failure(conversation, model, f"{model.get_provider_display()} isn't a supported chat provider.")


def _run_anthropic_turn(user_id, conversation, user_message_id, model: AssistantModel) -> None:
    """Streams the reply live (assistant.token per chunk) via the SDK's
    tool_runner -- see module docstring for the overall shape. Usage/cost
    is summed across every tool-calling round of this turn, not just the
    final one -- a single user message can cost several API calls if the
    model calls tools before answering, and the Message row should reflect
    the true total, not just the last round-trip."""
    if not settings.ANTHROPIC_API_KEY:
        _record_failure(conversation, model, "Assistant isn't configured -- ANTHROPIC_API_KEY isn't set.")
        return

    client = anthropic.Anthropic(api_key=settings.ANTHROPIC_API_KEY)

    api_messages = _history_messages(conversation, user_message_id)
    api_messages = _trim_to_fit(client, model, api_messages)
    final_count = client.messages.count_tokens(model=model.model_id, messages=api_messages).input_tokens
    if final_count > model.max_input_tokens:
        _record_failure(conversation, model, "Your message is too long for this model's context window, even alone.")
        return

    tools = build_tools(conversation.user, conversation)

    full_text_parts: list[str] = []
    total_input_tokens = total_output_tokens = 0
    total_cache_creation = total_cache_read = 0
    tool_use_output_tokens = 0
    tool_calls: list[str] = []
    final_message = None

    try:
        runner = client.beta.messages.tool_runner(
            model=model.model_id,
            max_tokens=4096,
            tools=tools,
            messages=api_messages,
            stream=True,
            max_iterations=MAX_TOOL_ITERATIONS,
        )
        for stream in runner:
            for text in stream.text_stream:
                send_to_user(user_id, "assistant.token", conversation_id=conversation.id, text=text)
                full_text_parts.append(text)
            final_message = stream.get_final_message()
            usage = final_message.usage
            total_input_tokens += usage.input_tokens
            total_output_tokens += usage.output_tokens
            total_cache_creation += usage.cache_creation_input_tokens or 0
            total_cache_read += usage.cache_read_input_tokens or 0

            # A round that called a tool vs. the final text-only round --
            # input tokens aren't split this way (every round resends the
            # full prior history, so input is dominated by "everything said
            # so far," not by this round's tool call), but output tokens
            # are genuinely this round's own work, so they split cleanly.
            round_tool_calls = [block.name for block in final_message.content if block.type == "tool_use"]
            if round_tool_calls:
                tool_calls.extend(round_tool_calls)
                tool_use_output_tokens += usage.output_tokens
    except anthropic.APIStatusError as exc:
        _record_failure(conversation, model, f"Assistant request failed: {exc.message}")
        return
    except anthropic.APIConnectionError:
        _record_failure(conversation, model, "Couldn't reach the assistant service -- try again.")
        return

    if final_message is None:
        _record_failure(conversation, model, "The assistant didn't produce a response.")
        return

    if final_message.stop_reason == "refusal":
        _record_failure(conversation, model, "The assistant declined to respond to that message.")
        return

    full_text = "".join(full_text_parts)
    cost = model.cost_for(total_input_tokens, total_output_tokens)

    assistant_message = Message.objects.create(
        conversation=conversation,
        role=Message.ROLE_ASSISTANT,
        text=full_text,
        model=model,
        input_tokens=total_input_tokens,
        output_tokens=total_output_tokens,
        cache_creation_input_tokens=total_cache_creation,
        cache_read_input_tokens=total_cache_read,
        cost_usd=cost,
        tool_calls=tool_calls,
        tool_use_output_tokens=tool_use_output_tokens if tool_calls else None,
    )
    conversation.save(update_fields=["updated_at"])

    send_to_user(
        user_id,
        "assistant.complete",
        conversation_id=conversation.id,
        message_id=assistant_message.id,
        text=full_text,
        model_id=model.model_id,
        input_tokens=total_input_tokens,
        output_tokens=total_output_tokens,
        cost_usd=str(cost),
        tool_calls=tool_calls,
        tool_use_output_tokens=tool_use_output_tokens if tool_calls else None,
    )


def _run_openai_turn(user_id, conversation, user_message_id, model: AssistantModel) -> None:
    """OpenAI branch of ask_assistant -- the Responses API, with a
    hand-rolled tool-call loop (there's no OpenAI-SDK equivalent of
    anthropic.beta.messages.tool_runner, confirmed -- same reasoning
    league/services/llm_openai.py's run_openai_turn already documents).
    Adapted from that function for an interactive chat turn instead of an
    unattended wake-up: the first call's `input` replays this
    CONVERSATION's real history (league only ever sends one fresh prompt,
    nothing to replay); later rounds within this same turn still chain via
    previous_response_id + function_call_output, identical to league's
    loop.

    Streams like the Anthropic path (assistant.token per text chunk) --
    unlike league/services/llm_openai.py's run_openai_turn, which is
    non-streaming (an unattended wake-up has no live UI waiting on
    incremental text, so it never needed this). Confirmed live
    (2026-09-19) against the real Responses API, stream=True: a
    response.output_text.delta event fires per text chunk, both for a
    plain reply and for whatever text a round emits alongside a tool
    call; a tool-only round emits ZERO text-delta events (only
    response.function_call_arguments.delta/.done, which this doesn't
    need to surface) -- so pushing every text-delta as assistant.token
    and nothing else is already correct with no extra "is this a tool
    round" branching. Exactly one of response.completed/.failed/
    .incomplete terminates each round and carries the SAME Response
    object client.responses.create() returns non-streaming (confirmed
    live) -- captured as `response` below so every check after the loop
    (usage, function_calls, status) is identical to a non-streaming call.

    No precise pre-flight context-window check, unlike the
    Anthropic path's client.messages.count_tokens call -- that's an
    Anthropic-specific endpoint, and OpenAI's SDK has no equivalent
    without adding a new dependency (tiktoken) or an approximate
    heuristic. ASSISTANT_HISTORY_LENGTH (default 10 messages) already
    bounds history to something far short of any current model's context
    window in practice; a genuinely oversized single message still fails
    clearly, just reactively (OpenAI's own 400 via APIStatusError below)
    rather than proactively."""
    if not settings.OPENAI_API_KEY:
        _record_failure(conversation, model, "Assistant isn't configured -- OPENAI_API_KEY isn't set.")
        return

    client = OpenAI(api_key=settings.OPENAI_API_KEY)
    tools = build_tools(conversation.user, conversation)
    tool_defs = openai_tool_defs(tools)

    next_input = _history_messages(conversation, user_message_id)
    previous_response_id = None

    total_input_tokens = total_output_tokens = 0
    total_cache_creation = total_cache_read = 0
    tool_use_output_tokens = 0
    tool_calls: list[str] = []
    full_text_parts: list[str] = []

    try:
        for _ in range(MAX_TOOL_ITERATIONS):
            stream = client.responses.create(
                model=model.model_id,
                input=next_input,
                tools=tool_defs,
                max_output_tokens=OPENAI_CHAT_MAX_OUTPUT_TOKENS,
                previous_response_id=previous_response_id,
                store=True,  # required for previous_response_id chaining below
                stream=True,
            )
            response = None
            for event in stream:
                if event.type == "response.output_text.delta":
                    send_to_user(user_id, "assistant.token", conversation_id=conversation.id, text=event.delta)
                    full_text_parts.append(event.delta)
                elif event.type in ("response.completed", "response.failed", "response.incomplete"):
                    response = event.response
            if response is None:
                # Every real stream ends with exactly one of the three
                # terminal events above (confirmed live) -- reaching here
                # means the connection dropped mid-stream with no
                # response.* terminal event at all, not a normal outcome.
                _record_failure(conversation, model, "The assistant connection ended unexpectedly -- try again.")
                return

            usage = response.usage
            total_input_tokens += usage.input_tokens
            total_output_tokens += usage.output_tokens
            if usage.input_tokens_details:
                total_cache_read += usage.input_tokens_details.cached_tokens or 0
                total_cache_creation += usage.input_tokens_details.cache_write_tokens or 0

            function_calls = [item for item in response.output if item.type == "function_call"]
            if function_calls:
                tool_calls.extend(call.name for call in function_calls)
                tool_use_output_tokens += usage.output_tokens

            if response.status == "failed":
                message = response.error.message if response.error else "Request failed."
                _record_failure(conversation, model, f"Assistant request failed: {message}")
                return
            if response.status == "incomplete":
                logger.warning(
                    "_run_openai_turn: incomplete response for conversation %s (%s)",
                    conversation.id, response.incomplete_details,
                )
                break
            if not function_calls:
                break  # final answer -- mirrors the Anthropic path's termination condition

            next_input = [
                {
                    "type": "function_call_output", "call_id": call.call_id,
                    "output": run_openai_tool(tools, call.name, call.arguments),
                }
                for call in function_calls
            ]
            previous_response_id = response.id
        else:
            logger.warning("_run_openai_turn: hit MAX_TOOL_ITERATIONS for conversation %s", conversation.id)
    except openai.APIStatusError as exc:
        _record_failure(conversation, model, f"Assistant request failed: {exc.message}")
        return
    except openai.APIConnectionError:
        _record_failure(conversation, model, "Couldn't reach the assistant service -- try again.")
        return

    full_text = "".join(full_text_parts)
    if not full_text:
        _record_failure(conversation, model, "The assistant didn't produce a response.")
        return

    # OpenAI's input_tokens INCLUDES cached tokens (billed at a discount,
    # not full price or free) -- unlike Anthropic's, which excludes cache
    # reads entirely (tracked separately, billed as their own line item at
    # cost_for's 1.25x/0.1x multipliers). Costing the raw input_tokens
    # here would price cached tokens at full rate, so the effective input
    # count is reduced by the cache read instead, with cache_creation/
    # cache_read passed as 0 to cost_for (whose multipliers are Anthropic-
    # cache-shaped, not applicable here). Same pattern -- including its
    # own caveat -- as league/tasks.py's _run_wakeup: this comes from
    # OpenAI's field naming/docs, not a confirmed live bill.
    billed_input = total_input_tokens - total_cache_read
    cost = model.cost_for(billed_input, total_output_tokens)

    assistant_message = Message.objects.create(
        conversation=conversation,
        role=Message.ROLE_ASSISTANT,
        text=full_text,
        model=model,
        input_tokens=total_input_tokens,
        output_tokens=total_output_tokens,
        cache_creation_input_tokens=total_cache_creation,
        cache_read_input_tokens=total_cache_read,
        cost_usd=cost,
        tool_calls=tool_calls,
        tool_use_output_tokens=tool_use_output_tokens if tool_calls else None,
    )
    conversation.save(update_fields=["updated_at"])

    send_to_user(
        user_id,
        "assistant.complete",
        conversation_id=conversation.id,
        message_id=assistant_message.id,
        text=full_text,
        model_id=model.model_id,
        input_tokens=total_input_tokens,
        output_tokens=total_output_tokens,
        cost_usd=str(cost),
        tool_calls=tool_calls,
        tool_use_output_tokens=tool_use_output_tokens if tool_calls else None,
    )


def _execute_ingest_filing(conversation: Conversation, params: dict) -> str:
    ticker = params["ticker"]
    accession_number = params["accession_number"]

    try:
        company = Company.objects.get(ticker=ticker)
    except Company.DoesNotExist:
        return f"Ingestion failed: {ticker} is no longer a known company."

    tracked, _ = TrackedCompany.objects.get_or_create(
        user=conversation.user, company=company, defaults={"origin": TrackedCompany.ORIGIN_USER}
    )
    submissions = sec_edgar.get_submissions(company)
    filing = next((f for f in sec_edgar.list_filings(submissions) if f["accession_number"] == accession_number), None)
    if filing is None:
        return f"Ingestion failed: filing {accession_number} for {ticker} is no longer listed by EDGAR."

    try:
        count = filing_ingestion.ingest_filing(tracked, filing)
    except ValueError as exc:
        return f"Ingestion failed: {exc}"

    return f"Ingested {filing['form']} filed {filing['filing_date']} for {ticker} -- {count} clips added to semantic search."


@shared_task
def execute_pending_action(pending_id):
    """Runs an approved PendingAction. A plain Celery task, not a
    continuation of Claude's conversation -- see PendingAction's docstring
    for why executing an approved action doesn't go back through the model
    at all. Result is appended as a plain assistant-role Message (keeps
    role alternation intact, same as every other assistant turn) and
    pushed over the WebSocket as assistant.action_complete."""
    pending = PendingAction.objects.select_related("conversation").get(id=pending_id)
    conversation = pending.conversation

    if pending.action_type == PendingAction.TYPE_INGEST_FILING:
        text = _execute_ingest_filing(conversation, pending.params)
    else:
        text = f"Unknown action type {pending.action_type!r}."

    message = Message.objects.create(conversation=conversation, role=Message.ROLE_ASSISTANT, text=text)
    conversation.save(update_fields=["updated_at"])

    send_to_user(
        conversation.user_id,
        "assistant.action_complete",
        conversation_id=conversation.id,
        pending_action_id=pending.id,
        message_id=message.id,
        text=text,
    )
