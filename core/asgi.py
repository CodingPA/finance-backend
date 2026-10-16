"""
ASGI config for core project.

Routes HTTP to Django as normal, and WebSocket connections to Channels.
django.core.asgi.get_asgi_application() must run (and be assigned) before
anything that imports models/consumers -- it's what populates Django's app
registry; importing app code first raises AppRegistryNotReady.
"""

import os

from django.conf import settings
from django.core.asgi import get_asgi_application

os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'core.settings')

django_asgi_app = get_asgi_application()

# runserver serves /static/ (admin CSS/JS, etc.) automatically in DEBUG;
# a plain ASGI app run via uvicorn (how this project always runs it, see
# README -- WebSockets need a real ASGI server) does not. This is the
# ASGI equivalent of that runserver convenience -- local dev only, since
# it's gated on DEBUG; prod (DEBUG=False) serves /static/ from nginx per
# docs/deployment.md, same as before.
if settings.DEBUG:
    from django.contrib.staticfiles.handlers import ASGIStaticFilesHandler
    django_asgi_app = ASGIStaticFilesHandler(django_asgi_app)

from channels.routing import ProtocolTypeRouter, URLRouter  # noqa: E402
from channels.security.websocket import AllowedHostsOriginValidator  # noqa: E402

from realtime.routing import websocket_urlpatterns  # noqa: E402

application = ProtocolTypeRouter({
    "http": django_asgi_app,
    "websocket": AllowedHostsOriginValidator(URLRouter(websocket_urlpatterns)),
})
