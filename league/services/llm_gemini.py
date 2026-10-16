"""Gemini-backed AI competitor turn execution (league.tasks._run_wakeup's
Gemini branch). Uses google-genai's client.models.generate_content with a
manually driven function-calling loop, deliberately with
automatic_function_calling DISABLED (confirmed the SDK offers it as an
option -- it would auto-execute plain Python callables passed as tools
and hand the whole loop to the SDK, similar in spirit to Anthropic's
tool_runner) -- not used here so this stays structurally symmetric with
llm_openai.py's hand-rolled loop: the same bounded iteration count, the
same explicit dispatch/error handling via run_gemini_tool, and the same
generic model_dump()-everything transcript philosophy, rather than mixing
"one provider auto-loops, two hand-roll."

Google Search grounding results land on candidate.grounding_metadata, NOT
as a content part -- easy to silently drop from the transcript if you
only iterate `parts` (confirmed live). Logged explicitly below."""

import logging

from django.conf import settings
from google import genai
from google.genai import types

from ..tools import GEMINI_SEARCH_TOOL, LEAGUE_MAX_TOOL_ITERATIONS, build_league_tools, gemini_tool_defs, run_gemini_tool
from .llm_common import LLMTurnResult

logger = logging.getLogger(__name__)

# See llm_openai.py's OPENAI_MAX_OUTPUT_TOKENS for why this is larger than
# the Anthropic path's 4096 -- Gemini's thinking tokens are billed as
# output too (thoughts_token_count, summed in below) and can consume the
# whole budget on a routine check-in otherwise.
GEMINI_MAX_OUTPUT_TOKENS = 12000


def run_gemini_turn(competitor, system_prompt: str, user_content: str) -> LLMTurnResult:
    if not settings.GEMINI_API_KEY:
        raise RuntimeError("GEMINI_API_KEY isn't configured.")

    client = genai.Client(api_key=settings.GEMINI_API_KEY)
    tools = build_league_tools(competitor)
    config = types.GenerateContentConfig(
        system_instruction=system_prompt,
        tools=[GEMINI_SEARCH_TOOL, types.Tool(function_declarations=gemini_tool_defs(tools))],
        max_output_tokens=GEMINI_MAX_OUTPUT_TOKENS,
        automatic_function_calling=types.AutomaticFunctionCallingConfig(disable=True),
        # Confirmed live (not in any doc found beforehand): combining a
        # built-in tool (google_search) with function-calling tools in the
        # same request 400s -- "Please enable
        # tool_config.include_server_side_tool_invocations to use Built-in
        # tools with Function calling" -- unless this is explicitly set.
        tool_config=types.ToolConfig(include_server_side_tool_invocations=True),
    )

    contents = [types.Content(role="user", parts=[types.Part(text=user_content)])]
    transcript = [{"role": "user", "text": user_content}]
    total_input = total_output = total_cache_read = 0
    tool_calls_log: list[str] = []

    for _ in range(LEAGUE_MAX_TOOL_ITERATIONS):
        response = client.models.generate_content(
            model=competitor.assistant_model.model_id, contents=contents, config=config,
        )
        usage = response.usage_metadata
        total_input += usage.prompt_token_count or 0
        # candidates_token_count is the visible answer; thoughts_token_count
        # is internal reasoning, billed separately at the output rate but
        # NOT included in candidates_token_count (confirmed live: they're
        # two distinct usage_metadata fields) -- summed here so cost_for()
        # sees the full billed output, same reasoning as OpenAI's
        # reasoning_tokens (there, already folded into completion_tokens;
        # here, reported as a separate counter instead).
        total_output += (usage.candidates_token_count or 0) + (usage.thoughts_token_count or 0)
        # cached_content_token_count has no cache-WRITE counterpart to
        # accumulate -- Gemini's context caching is an explicit, separate
        # API this app never calls (unlike Anthropic/OpenAI's implicit
        # cache-on-first-use), so this is always 0 here, not just often 0.
        total_cache_read += usage.cached_content_token_count or 0

        candidate = response.candidates[0]
        model_content = candidate.content
        contents.append(model_content)

        function_calls = []
        for part in model_content.parts or []:
            # mode="json" (not the bare default) -- a thinking-capable
            # model's parts can carry thought_signature as raw `bytes`
            # (confirmed live: 200 OK from Gemini, then a TypeError writing
            # AgentTurn.transcript, a JSONField -- "Object of type bytes is
            # not JSON serializable"). mode="json" base64-encodes it instead
            # of leaving it as a Python bytes object.
            transcript.append({"role": "assistant", **part.model_dump(exclude_none=True, mode="json")})
            if part.function_call is not None:
                function_calls.append(part.function_call)
                tool_calls_log.append(part.function_call.name)

        # grounding_metadata lives on the candidate, not in `parts` --
        # logged separately so a grounded turn's search queries aren't
        # silently missing from the transcript.
        grounding = candidate.grounding_metadata
        if grounding is not None:
            transcript.append({
                "role": "assistant", "type": "grounding_metadata",
                **grounding.model_dump(exclude_none=True, mode="json"),
            })
            if grounding.web_search_queries:
                tool_calls_log.append("google_search")

        if candidate.finish_reason == types.FinishReason.MAX_TOKENS:
            logger.warning("run_gemini_turn: hit max_output_tokens for competitor %s", competitor.id)
            break
        if not function_calls:
            break  # no more tool calls -- final answer, mirrors the other providers' termination condition

        response_parts = [
            types.Part.from_function_response(name=call.name, response={"result": run_gemini_tool(tools, call.name, call.args)})
            for call in function_calls
        ]
        tool_content = types.Content(role="tool", parts=response_parts)
        contents.append(tool_content)
        transcript.extend({"role": "tool", **p.model_dump(exclude_none=True, mode="json")} for p in response_parts)
    else:
        logger.warning("run_gemini_turn: hit LEAGUE_MAX_TOOL_ITERATIONS for competitor %s", competitor.id)

    return LLMTurnResult(
        transcript=transcript, input_tokens=total_input, output_tokens=total_output,
        cache_creation_input_tokens=0, cache_read_input_tokens=total_cache_read,
        tool_calls=tool_calls_log,
    )
