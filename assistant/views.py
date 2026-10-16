from django.utils import timezone
from rest_framework import permissions, status
from rest_framework.exceptions import NotFound, ValidationError
from rest_framework.response import Response
from rest_framework.views import APIView

from companies.services.sec_edgar import resolve_company

from .models import AssistantModel, Conversation, Message, PendingAction
from .tasks import ask_assistant, execute_pending_action


class AssistantModelListView(APIView):
    """GET the active models a chat can be started with, for a frontend
    model picker -- same catalog `ConversationMessagesView` validates
    `model_id` against. Scoped to AssistantModel.CHAT_PROVIDERS
    (Anthropic + OpenAI) deliberately: assistant/tasks.py's call loop only
    knows how to run those two -- a Gemini row (active for league's AI
    competitors, which branch on provider themselves) would otherwise show
    up here as a selectable chat model and then hard-fail the moment
    someone actually picked it."""

    permission_classes = [permissions.IsAuthenticated]

    def get(self, request):
        models = AssistantModel.objects.filter(is_active=True, provider__in=AssistantModel.CHAT_PROVIDERS)
        return Response([
            {"model_id": m.model_id, "display_name": m.display_name, "provider": m.provider, "is_default": m.is_default}
            for m in models
        ])


def _conversation_row(conversation: Conversation) -> dict:
    return {
        "id": conversation.id,
        "title": conversation.title,
        "created_at": conversation.created_at,
        "updated_at": conversation.updated_at,
    }


def _message_row(message: Message) -> dict:
    return {
        "id": message.id,
        "role": message.role,
        "text": message.text,
        "ticker": message.company.ticker if message.company_id else None,
        "model_id": message.model.model_id if message.model_id else None,
        "input_tokens": message.input_tokens,
        "output_tokens": message.output_tokens,
        "tool_calls": message.tool_calls,
        "tool_use_output_tokens": message.tool_use_output_tokens,
        "cost_usd": str(message.cost_usd) if message.cost_usd is not None else None,
        "created_at": message.created_at,
    }


def _pending_action_row(pending: PendingAction) -> dict:
    return {
        "id": pending.id,
        "action_type": pending.action_type,
        "params": pending.params,
        "description": pending.description,
        "status": pending.status,
        "created_at": pending.created_at,
        "resolved_at": pending.resolved_at,
    }


def _get_conversation_or_404(user, conversation_id) -> Conversation:
    try:
        return Conversation.objects.get(id=conversation_id, user=user)
    except Conversation.DoesNotExist:
        raise NotFound(f"No conversation {conversation_id}.")


class ConversationListView(APIView):
    """GET every conversation the user has, newest-active first (so a
    frontend can render a back-and-forth conversation switcher). POST
    creates a new, empty one -- sending the first message is a separate
    call, same as tracking a company vs. running its first screen."""

    permission_classes = [permissions.IsAuthenticated]

    def get(self, request):
        conversations = Conversation.objects.filter(user=request.user)
        return Response([_conversation_row(c) for c in conversations])

    def post(self, request):
        conversation = Conversation.objects.create(user=request.user, title=request.data.get("title", ""))
        return Response(_conversation_row(conversation), status=status.HTTP_201_CREATED)


class ConversationDetailView(APIView):
    """DELETE a conversation the user no longer wants -- cascades to its
    messages."""

    permission_classes = [permissions.IsAuthenticated]

    def delete(self, request, conversation_id):
        conversation = _get_conversation_or_404(request.user, conversation_id)
        conversation.delete()
        return Response(status=status.HTTP_204_NO_CONTENT)


class ConversationMessagesView(APIView):
    """GET every message in a conversation (for loading history when a
    frontend switches to it) and POST a new user turn into it. POST
    persists the user's message synchronously (so it shows up in a
    follow-up GET immediately, before the assistant has replied) and queues
    the actual model call -- the reply streams over the user's WebSocket
    group as assistant.token / assistant.complete / assistant.error
    (docs/websockets.md), not over this response.

    `ticker` is optional and per-message, not per-conversation -- one
    conversation can mix scoped and unscoped turns. Resolved the same way
    /api/companies/ does (creates the Company on first sight if it's a
    real SEC ticker); invalid tickers 400 the same way."""

    permission_classes = [permissions.IsAuthenticated]

    def get(self, request, conversation_id):
        conversation = _get_conversation_or_404(request.user, conversation_id)
        return Response([_message_row(m) for m in conversation.messages.select_related("company", "model")])

    def post(self, request, conversation_id):
        conversation = _get_conversation_or_404(request.user, conversation_id)

        text = request.data.get("message")
        if not text:
            raise ValidationError({"message": ["This field is required."]})

        company = None
        ticker = request.data.get("ticker")
        if ticker:
            try:
                company = resolve_company(ticker)
            except ValueError as exc:
                raise ValidationError({"ticker": [str(exc)]})

        model_id = request.data.get("model_id")
        if model_id and not AssistantModel.objects.filter(
            model_id=model_id, is_active=True, provider__in=AssistantModel.CHAT_PROVIDERS,
        ).exists():
            raise ValidationError({"model_id": [f"{model_id!r} isn't an available assistant model."]})

        user_message = Message.objects.create(conversation=conversation, role=Message.ROLE_USER, text=text, company=company)
        ask_assistant.delay(str(request.user.id), conversation.id, user_message.id, model_id)
        return Response(_message_row(user_message), status=status.HTTP_202_ACCEPTED)


class PendingActionConfirmView(APIView):
    """POST to approve or deny an action the assistant proposed (currently
    just ingest_filing, from the propose_filing_ingestion tool -- see
    PendingAction's docstring). Approving queues the real work as a plain
    Celery task; either way the result (success, failure, or "declined")
    lands as a normal assistant-role message and an assistant.action_complete
    WebSocket push (docs/websockets.md) -- not routed back through Claude,
    since the confirmation itself isn't part of the model's tool-call loop."""

    permission_classes = [permissions.IsAuthenticated]

    def post(self, request, conversation_id, pending_id):
        conversation = _get_conversation_or_404(request.user, conversation_id)
        try:
            pending = conversation.pending_actions.get(id=pending_id, status=PendingAction.STATUS_PENDING)
        except PendingAction.DoesNotExist:
            raise NotFound(f"No pending action {pending_id} awaiting confirmation on this conversation.")

        approved = bool(request.data.get("approved"))
        pending.status = PendingAction.STATUS_APPROVED if approved else PendingAction.STATUS_DENIED
        pending.resolved_at = timezone.now()
        pending.save(update_fields=["status", "resolved_at"])

        if approved:
            execute_pending_action.delay(pending.id)
        else:
            Message.objects.create(
                conversation=conversation, role=Message.ROLE_ASSISTANT, text=f"Declined: {pending.description}"
            )

        return Response(_pending_action_row(pending))
