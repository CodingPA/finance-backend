from django.urls import path

from . import views

app_name = "assistant"

urlpatterns = [
    path("models/", views.AssistantModelListView.as_view(), name="models"),
    path("conversations/", views.ConversationListView.as_view(), name="conversations"),
    path("conversations/<int:conversation_id>/", views.ConversationDetailView.as_view(), name="conversation-detail"),
    path("conversations/<int:conversation_id>/messages/", views.ConversationMessagesView.as_view(), name="conversation-messages"),
    path(
        "conversations/<int:conversation_id>/pending-actions/<int:pending_id>/",
        views.PendingActionConfirmView.as_view(), name="pending-action-confirm",
    ),
]
