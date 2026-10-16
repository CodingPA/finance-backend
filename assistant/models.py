from decimal import Decimal

from django.conf import settings
from django.db import models


class AssistantModel(models.Model):
    """Which models the chat assistant (and league's AI competitors) may
    use, managed from the admin panel -- add a new one when a provider
    ships a model worth offering, deactivate one without deleting it (so
    past references, once conversations/turns are persisted, still resolve
    to a real row).

    `model_id` is the literal API model string for whichever `provider`
    this row names (e.g. "claude-opus-5" for Anthropic, a GPT model id for
    OpenAI, a Gemini model id for Google) passed straight to that
    provider's SDK -- no separate mapping table to keep in sync.
    `provider` determines which client/call-loop picks this row up
    (assistant/tasks.py and league/tasks.py both branch on it) -- the
    three providers' SDKs are NOT interchangeable, so getting this field
    wrong on a new row means the wrong code path runs.

    `max_input_tokens` and the two price-per-million fields are admin data,
    not looked up live from any provider's Models API -- they change
    (introductory pricing windows, new models with different context
    windows), and the admin should be able to update them without a code
    deploy. Prices are per-million-token, matching how every provider
    publishes them, so cost math elsewhere doesn't need a unit conversion.

    `is_default` should only ever be set on a row whose provider is in
    CHAT_PROVIDERS below: get_default() has no provider awareness, and the
    chat assistant (assistant/tasks.py) only has a call loop for Anthropic
    and OpenAI -- flipping a Gemini row to default would silently break
    chat on the next turn that doesn't name a model. (Anthropic-only used
    to be the whole story here; assistant/tasks.py grew an OpenAI branch
    once the chat assistant needed to offer OpenAI models too.)"""

    PROVIDER_ANTHROPIC = "anthropic"
    PROVIDER_OPENAI = "openai"
    PROVIDER_GEMINI = "gemini"
    PROVIDER_CHOICES = [
        (PROVIDER_ANTHROPIC, "Anthropic"),
        (PROVIDER_OPENAI, "OpenAI"),
        (PROVIDER_GEMINI, "Gemini"),
    ]
    # Providers the CHAT assistant's own call loop (assistant/tasks.py) can
    # actually run a turn against -- AssistantModelListView and
    # ConversationMessagesView both filter on this, not on `provider`
    # directly, so adding a provider to chat is a one-line change here
    # rather than hunting down every filter site. League's AI competitors
    # are NOT limited by this -- league/tasks.py branches on `provider`
    # itself and already supports all three (Anthropic/OpenAI/Gemini).
    CHAT_PROVIDERS = (PROVIDER_ANTHROPIC, PROVIDER_OPENAI)

    model_id = models.CharField(max_length=64, unique=True)
    display_name = models.CharField(max_length=100)
    provider = models.CharField(max_length=16, choices=PROVIDER_CHOICES, default=PROVIDER_ANTHROPIC)
    is_active = models.BooleanField(default=True)
    is_default = models.BooleanField(default=False)
    max_input_tokens = models.PositiveIntegerField(default=1_000_000)
    input_price_per_million = models.DecimalField(max_digits=8, decimal_places=4, default=Decimal("0"))
    output_price_per_million = models.DecimalField(max_digits=8, decimal_places=4, default=Decimal("0"))
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["display_name"]

    def __str__(self):
        return self.display_name

    def save(self, *args, **kwargs):
        if self.is_default:
            AssistantModel.objects.exclude(pk=self.pk).update(is_default=False)
        super().save(*args, **kwargs)

    @classmethod
    def get_default(cls) -> "AssistantModel":
        """The model a chat uses when the caller doesn't name one. Falls
        back to any active model if none is flagged default (misconfigured
        admin data shouldn't take the assistant down); raises if there's
        truly nothing active to use, since that's a real setup problem the
        admin needs to fix, not something to paper over silently."""
        model = cls.objects.filter(is_active=True, is_default=True).first()
        if model is None:
            model = cls.objects.filter(is_active=True).first()
        if model is None:
            raise cls.DoesNotExist("No active AssistantModel configured -- add one in /admin/.")
        return model

    # Anthropic's ephemeral (5-minute TTL) prompt-cache pricing -- a cache
    # WRITE costs more than a plain input token (you're paying to populate
    # the cache, not just to process it), a cache READ costs far less.
    # OpenAI/Gemini have no equivalent multiplier here: OpenAI's caching is
    # implicit (callers fold its discount into a reduced input_tokens count
    # before calling this) and Gemini's explicit cachedContent API isn't
    # wired up (see llm_gemini.py), so cache_creation_tokens/cache_read_tokens
    # should only ever be nonzero for an Anthropic call.
    _ANTHROPIC_CACHE_WRITE_MULTIPLIER = Decimal("1.25")
    _ANTHROPIC_CACHE_READ_MULTIPLIER = Decimal("0.1")

    def cost_for(
        self, input_tokens: int, output_tokens: int,
        *, cache_creation_tokens: int = 0, cache_read_tokens: int = 0,
    ) -> Decimal:
        return (
            Decimal(input_tokens) * self.input_price_per_million
            + Decimal(cache_creation_tokens) * self.input_price_per_million * self._ANTHROPIC_CACHE_WRITE_MULTIPLIER
            + Decimal(cache_read_tokens) * self.input_price_per_million * self._ANTHROPIC_CACHE_READ_MULTIPLIER
            + Decimal(output_tokens) * self.output_price_per_million
        ) / Decimal(1_000_000)


class Conversation(models.Model):
    """One back-and-forth thread a user has with the assistant. Not itself
    tied to a ticker -- per-message company scoping lives on Message
    instead, so one conversation can mix ticker-scoped and unscoped turns."""

    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="assistant_conversations")
    title = models.CharField(max_length=255, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-updated_at"]

    def __str__(self):
        return self.title or f"Conversation {self.pk}"


class Message(models.Model):
    """One turn in a Conversation. User turns just carry text (plus,
    optionally, which company this specific turn is scoped to); assistant
    turns also carry which model answered and the real usage/cost Anthropic
    reported for that call -- model/tokens/cost are null on user turns,
    since a user message doesn't call the API on its own.

    `company` is per-message, not per-Conversation -- one conversation can
    range across "no ticker" / "just RKLB" / (later) "compare RKLB and
    ASTS" turns, since the request was for the backend to be able to
    handle that mix within a single thread rather than forcing one scope
    for the whole conversation. Single-ticker only for now (a plain FK);
    a list-of-tickers or an explicit ALL/NONE mode is a later expansion --
    deliberately not built until there's a real tool that uses it, since
    right now this only annotates context sent to the model, it doesn't
    filter or fetch anything company-specific yet.

    Every turn, including failed ones, gets a row: if a call errors or the
    model refuses, an assistant-role Message still gets created (with the
    failure noted as its text) so role alternation stays intact for the
    next turn -- the Anthropic API requires strict user/assistant
    alternation, and two consecutive user messages in history would 400 on
    the following call."""

    ROLE_USER = "user"
    ROLE_ASSISTANT = "assistant"
    ROLE_CHOICES = [
        (ROLE_USER, "User"),
        (ROLE_ASSISTANT, "Assistant"),
    ]

    conversation = models.ForeignKey(Conversation, on_delete=models.CASCADE, related_name="messages")
    role = models.CharField(max_length=16, choices=ROLE_CHOICES)
    text = models.TextField()
    company = models.ForeignKey(
        "companies.Company", on_delete=models.SET_NULL, null=True, blank=True, related_name="assistant_messages"
    )
    model = models.ForeignKey(AssistantModel, on_delete=models.SET_NULL, null=True, blank=True, related_name="messages")
    input_tokens = models.PositiveIntegerField(null=True, blank=True)
    output_tokens = models.PositiveIntegerField(null=True, blank=True)
    cache_creation_input_tokens = models.PositiveIntegerField(null=True, blank=True)
    cache_read_input_tokens = models.PositiveIntegerField(null=True, blank=True)
    cost_usd = models.DecimalField(max_digits=10, decimal_places=6, null=True, blank=True)
    # A single user message can trigger several API round-trips if the
    # model calls tools before answering (input_tokens/output_tokens above
    # are the sum across all of them). These two break that total down:
    # which tools ran, and how much of output_tokens is attributable to
    # tool-calling rounds specifically vs. the final answer. Input tokens
    # aren't split the same way -- every round resends the full prior
    # history, so a round's input total is dominated by "everything said
    # so far," not by that round's tool call; output tokens are what that
    # round actually produced, which is the meaningful split.
    tool_calls = models.JSONField(default=list, blank=True)
    tool_use_output_tokens = models.PositiveIntegerField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["created_at"]

    def __str__(self):
        return f"{self.conversation_id}:{self.role}:{self.pk}"


class PendingAction(models.Model):
    """An action the assistant proposed that needs explicit user
    confirmation before it actually runs -- right now, just ingesting a
    filing (a real fetch + Docling parse + embedding call, not free and not
    instant).

    Deliberately decoupled from Claude's own tool-call loop rather than
    built as a paused tool_use/tool_result round-trip: the tool that
    proposes an action persists this row, notifies the user, and returns a
    plain text acknowledgement to Claude immediately -- it does not block
    the turn waiting for a human who might not respond for minutes (or at
    all). Executing an approved action doesn't go back through Claude
    either; it's a plain Celery task, the same shape as any other
    user-triggered async action in this app (e.g. the dashboard's "Refresh"
    button), not a resumed model conversation."""

    TYPE_INGEST_FILING = "ingest_filing"
    TYPE_CHOICES = [
        (TYPE_INGEST_FILING, "Ingest filing"),
    ]

    STATUS_PENDING = "pending"
    STATUS_APPROVED = "approved"
    STATUS_DENIED = "denied"
    STATUS_CHOICES = [
        (STATUS_PENDING, "Pending"),
        (STATUS_APPROVED, "Approved"),
        (STATUS_DENIED, "Denied"),
    ]

    conversation = models.ForeignKey(Conversation, on_delete=models.CASCADE, related_name="pending_actions")
    action_type = models.CharField(max_length=32, choices=TYPE_CHOICES)
    params = models.JSONField()
    description = models.CharField(max_length=500)
    status = models.CharField(max_length=16, choices=STATUS_CHOICES, default=STATUS_PENDING)
    created_at = models.DateTimeField(auto_now_add=True)
    resolved_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return f"{self.action_type}:{self.status}:{self.pk}"
