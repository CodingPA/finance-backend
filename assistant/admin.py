from django.contrib import admin

from .models import AssistantModel, Conversation, Message, PendingAction


@admin.register(AssistantModel)
class AssistantModelAdmin(admin.ModelAdmin):
    list_display = [
        "display_name", "model_id", "provider", "is_active", "is_default",
        "max_input_tokens", "input_price_per_million", "output_price_per_million", "created_at",
    ]
    list_editable = [
        "provider", "is_active", "is_default", "max_input_tokens",
        "input_price_per_million", "output_price_per_million",
    ]


class MessageInline(admin.TabularInline):
    model = Message
    extra = 0
    fields = [
        "role", "text", "company", "model",
        "input_tokens", "output_tokens", "tool_calls", "tool_use_output_tokens", "cost_usd", "created_at",
    ]
    readonly_fields = fields
    can_delete = False


@admin.register(Conversation)
class ConversationAdmin(admin.ModelAdmin):
    list_display = ["id", "user", "title", "created_at", "updated_at"]
    list_filter = ["user"]
    inlines = [MessageInline]


@admin.register(Message)
class MessageAdmin(admin.ModelAdmin):
    list_display = [
        "id", "conversation", "role", "company", "model",
        "input_tokens", "output_tokens", "tool_calls", "tool_use_output_tokens", "cost_usd", "created_at",
    ]
    list_filter = ["role", "model", "company"]


@admin.register(PendingAction)
class PendingActionAdmin(admin.ModelAdmin):
    list_display = ["id", "conversation", "action_type", "description", "status", "created_at", "resolved_at"]
    list_filter = ["action_type", "status"]
