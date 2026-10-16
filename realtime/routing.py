from django.urls import re_path

from . import consumers

websocket_urlpatterns = [
    re_path(r"^ws/user/$", consumers.UserConsumer.as_asgi()),
    re_path(r"^ws/league/(?P<competition_id>\d+)/$", consumers.LeagueFeedConsumer.as_asgi()),
]
