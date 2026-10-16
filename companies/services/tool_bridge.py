"""Reshapes a list of Anthropic-SDK @beta_tool closures (however they were
built -- assistant/tools.py's build_tools() or league/tools.py's
build_league_tools()) for OpenAI's Responses API, and dispatches a
function_call back to the matching tool's own .call(). Provider-generic:
nothing in here is specific to which app's tool set is passed in, which is
the whole reason this lives here rather than duplicated in both --
league/tools.py re-exports these two names unchanged (its own callers,
league/services/llm_openai.py, import them from there) so this move didn't
need to touch anything downstream of league/tools.py itself."""

import json

from anthropic.lib.tools import ToolError


def openai_tool_defs(tools: list) -> list[dict]:
    """OpenAI Responses API function-tool specs, reshaped from the same
    @beta_tool objects the Anthropic path uses -- .name/.description/
    .input_schema already come from the anthropic SDK's own pydantic
    introspection of each closure (confirmed live), so this is a pure
    reshape, not a duplicate schema -- zero forked business logic between
    providers. Responses API's function-tool shape is FLAT (name/
    parameters/description at the top level), unlike Chat Completions'
    nested {"function": {...}} envelope -- confirmed against the installed
    openai==2.46.0 SDK's FunctionToolParam. `strict` mode is deliberately
    NOT set: OpenAI's strict mode requires every property to be listed in
    `required` with no defaults, and these schemas have optional/defaulted
    fields (e.g. get_stock_quote's asset_class)."""
    return [
        {"type": "function", "name": t.name, "description": t.description, "parameters": t.input_schema}
        for t in tools
    ]


def run_openai_tool(tools: list, name: str, arguments_json: str) -> str:
    """Dispatch one Responses API function_call item by name to the
    matching @beta_tool object's own .call() -- same validation + business
    logic the Anthropic path uses. .call() RAISES rather than returning an
    error string (confirmed live: ToolError, a plain exception, and a bad
    args ValueError all raise) -- caught here and rendered as text, since
    every function_call MUST get a function_call_output reply or the next
    responses.create() call fails; nothing may propagate past this
    function."""
    dispatch = {t.name: t for t in tools}
    tool = dispatch.get(name)
    if tool is None:
        return f"Unknown tool {name!r}."
    try:
        return tool.call(json.loads(arguments_json or "{}"))
    except ToolError as exc:
        return str(exc)
    except Exception as exc:
        return f"Error: {exc!r}"
