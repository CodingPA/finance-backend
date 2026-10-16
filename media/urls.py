from django.urls import path

from . import views

app_name = "media"

urlpatterns = [
    path("", views.MediaFileListView.as_view(), name="list"),
    path("<int:media_file_id>/", views.MediaFileDetailView.as_view(), name="detail"),
]
