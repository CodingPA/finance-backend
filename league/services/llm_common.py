from dataclasses import dataclass, field


@dataclass
class LLMTurnResult:
    """What league.tasks._run_wakeup needs to build the downstream
    AgentTurn, independent of which provider produced it -- the shared
    contract between _run_anthropic_turn and llm_openai.run_openai_turn,
    kept in its own module (no anthropic/openai imports) so neither
    provider-specific module has to import from the other."""

    transcript: list
    input_tokens: int
    output_tokens: int
    cache_creation_input_tokens: int
    cache_read_input_tokens: int
    tool_calls: list = field(default_factory=list)
