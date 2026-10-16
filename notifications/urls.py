from django.urls import path

from . import views

app_name = "notifications"

urlpatterns = [
    path("", views.NotificationListView.as_view(), name="list"),
    path("<int:notification_id>/ack/", views.NotificationAckView.as_view(), name="ack"),
]
