"""OpenAI-backed AI competitor turn execution (league.tasks._run_wakeup's
OpenAI branch). Uses the Responses API (client.responses.create), not Chat
Completions -- OpenAI's hosted web_search tool only lives on the Responses
API, and the league competitors need it alongside their local function
tools (get_stock_quote, submit_trade, etc.) in the same turn, same as the
Anthropic path's web_search + local tools. There is no OpenAI equivalent
of anthropic.beta.messages.tool_runner (confirmed -- OpenAI's SDK has no
single-call agentic-loop helper), so this hand-rolls the loop: call, check
for function_call items in the response, dispatch them locally, feed
results back via previous_response_id chaining, repeat."""

import logging

from django.conf import settings
from openai import OpenAI

from ..tools import LEAGUE_MAX_TOOL_ITERATIONS, OPENAI_WEB_SEARCH_TOOL, build_league_tools, openai_tool_defs, run_openai_tool
from .llm_common import LLMTurnResult

logger = logging.getLogger(__name__)

# Flagship models bill internal reasoning as output tokens -- a routine
# check-in can burn the whole budget on reasoning before it ever places a
# tool call or writes a final answer, so this is deliberately larger than
# the Anthropic path's 4096 (which has no comparable reasoning-token
# cost). reasoning effort is capped at "low" for the same reason: this is
# a repeated, cost-sensitive wake-up loop, not a one-off hard problem.
OPENAI_MAX_OUTPUT_TOKENS = 12000


def run_openai_turn(competitor, system_prompt: str, user_content: str) -> LLMTurnResult:
    if not settings.OPENAI_API_KEY:
        raise RuntimeError("OPENAI_API_KEY isn't configured.")

    client = OpenAI(api_key=settings.OPENAI_API_KEY)
    tools = build_league_tools(competitor)
    tool_defs = [*openai_tool_defs(tools), OPENAI_WEB_SEARCH_TOOL]

    transcript = [{"role": "user", "text": user_content}]
    total_input = total_output = total_cache_read = total_cache_creation = 0
    tool_calls_log: list[str] = []

    previous_response_id = None
    next_input = [{"role": "user", "content": user_content}]

    for _ in range(LEAGUE_MAX_TOOL_ITERATIONS):
        response = client.responses.create(
            model=competitor.assistant_model.model_id,
            instructions=system_prompt,
            input=next_input,
            tools=tool_defs,
            max_output_tokens=OPENAI_MAX_OUTPUT_TOKENS,
            reasoning={"effort": "low"},
            previous_response_id=previous_response_id,
            store=True,  # required for previous_response_id chaining below
        )
        usage = response.usage
        total_input += usage.input_tokens
        total_output += usage.output_tokens
        if usage.input_tokens_details:
            total_cache_read += usage.input_tokens_details.cached_tokens or 0
            total_cache_creation += usage.input_tokens_details.cache_write_tokens or 0

        # model_dump() generically for every item type rather than
        # hand-picking known ones -- the Anthropic path's own history is
        # the reason why: a hand-picked text/tool_use-only capture there
        # silently dropped server_tool_use/web_search_tool_result blocks
        # until a live search caught it. Reasoning items, web_search_call
        # items, everything gets logged here from day one instead.
        function_calls = []
        for item in response.output:
            transcript.append({"role": "assistant", **item.model_dump(exclude_none=True)})
            if item.type == "function_call":
                function_calls.append(item)
                tool_calls_log.append(item.name)
            elif item.type == "web_search_call":
                tool_calls_log.append("web_search")

        if response.status == "incomplete":
            logger.warning(
                "run_openai_turn: incomplete response for competitor %s (%s)",
                competitor.id, response.incomplete_details,
            )
            break
        if not function_calls:
            break  # no more tool calls -- final answer, mirrors the Anthropic path's termination condition

        outputs = [
            {"type": "function_call_output", "call_id": call.call_id, "output": run_openai_tool(tools, call.name, call.arguments)}
            for call in function_calls
        ]
        transcript.extend(outputs)
        previous_response_id = response.id
        next_input = outputs
    else:
        logger.warning("run_openai_turn: hit LEAGUE_MAX_TOOL_ITERATIONS for competitor %s", competitor.id)

    return LLMTurnResult(
        transcript=transcript, input_tokens=total_input, output_tokens=total_output,
        cache_creation_input_tokens=total_cache_creation, cache_read_input_tokens=total_cache_read,
        tool_calls=tool_calls_log,
    )
