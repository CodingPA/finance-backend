from django.conf import settings
from django.contrib.auth import get_user_model
from google.auth.transport import requests as google_requests
from google.oauth2 import id_token as google_id_token
from rest_framework import generics, permissions, status
from rest_framework.exceptions import AuthenticationFailed
from rest_framework.response import Response
from rest_framework.throttling import ScopedRateThrottle
from rest_framework.views import APIView
from rest_framework_simplejwt.exceptions import TokenError
from rest_framework_simplejwt.serializers import TokenObtainPairSerializer
from rest_framework_simplejwt.tokens import RefreshToken
from rest_framework_simplejwt.views import TokenObtainPairView

from .models import AllowedEmail
from .serializers import (
    GoogleAuthSerializer,
    LogoutSerializer,
    RegisterSerializer,
    UserSerializer,
)

User = get_user_model()


def _token_pair_response(user, status_code=status.HTTP_200_OK):
    refresh = RefreshToken.for_user(user)
    return Response(
        {
            "access": str(refresh.access_token),
            "refresh": str(refresh),
            "user": UserSerializer(user).data,
        },
        status=status_code,
    )


class RegisterView(generics.CreateAPIView):
    """Email + password sign-up. No email verification required yet --
    accounts are active immediately."""

    queryset = User.objects.all()
    serializer_class = RegisterSerializer
    permission_classes = [permissions.AllowAny]
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = "auth"

    def create(self, request, *args, **kwargs):
        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        user = serializer.save()
        return _token_pair_response(user, status.HTTP_201_CREATED)


class LoginSerializer(TokenObtainPairSerializer):
    """Same as simplejwt's default (keyed on USERNAME_FIELD, i.e. email) --
    just also returns the user object so the frontend doesn't need a second
    round trip after logging in."""

    def validate(self, attrs):
        data = super().validate(attrs)
        data["user"] = UserSerializer(self.user).data
        return data


class LoginView(TokenObtainPairView):
    serializer_class = LoginSerializer
    permission_classes = [permissions.AllowAny]
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = "auth"


class GoogleAuthView(APIView):
    """Frontend gets an ID token from Google Identity Services and posts it
    here. We verify it was actually issued by Google for this app, then find
    or create the matching user. Matches by google_sub first (stable across
    email changes), falling back to email (links a Google login to an
    existing email/password account with the same address, but only for a
    Google-verified email, and never over a different Google identity
    already linked to that account)."""

    permission_classes = [permissions.AllowAny]
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = "auth"

    def post(self, request):
        serializer = GoogleAuthSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        token = serializer.validated_data["id_token"]

        if not settings.GOOGLE_OAUTH_CLIENT_ID:
            raise AuthenticationFailed("Google sign-in is not configured on this server.")

        try:
            payload = google_id_token.verify_oauth2_token(
                token, google_requests.Request(), settings.GOOGLE_OAUTH_CLIENT_ID
            )
        except ValueError as exc:
            raise AuthenticationFailed(f"Invalid Google token: {exc}")

        sub = payload["sub"]
        email = payload.get("email")
        if not email:
            raise AuthenticationFailed("Google account has no email.")
        # Matching on email (below) is only safe if Google vouches the
        # signer actually owns that address -- otherwise an unverified
        # Google account claiming someone else's email logs in as them.
        if payload.get("email_verified") not in (True, "true"):
            raise AuthenticationFailed("Google account email is not verified.")
        name = payload.get("name", "")

        user = User.objects.filter(google_sub=sub).first()
        if user is None:
            # Gate NEW account creation only -- an email that already has an
            # account always gets linked/logged in below regardless of the
            # allowlist; removing an AllowedEmail row later must never lock
            # out someone who already signed up.
            is_new_email = not User.objects.filter(email=email).exists()
            if is_new_email and not AllowedEmail.objects.filter(email=email.strip().lower()).exists():
                raise AuthenticationFailed("This email isn't authorized to sign up.")

            user, created = User.objects.get_or_create(
                email=email, defaults={"name": name, "google_sub": sub}
            )
            # Already linked to a DIFFERENT Google identity with the same
            # email -- e.g. a lapsed custom domain re-registered and its
            # Google account recreated (new sub, same address). Never hand
            # that newcomer the existing account.
            if not created and user.google_sub and user.google_sub != sub:
                raise AuthenticationFailed("This email is linked to a different Google account.")
            if not created and not user.google_sub:
                user.google_sub = sub
                if name and not user.name:
                    user.name = name
                user.save(update_fields=["google_sub", "name"])

        return _token_pair_response(user)


class LogoutView(APIView):
    """Blacklists the given refresh token so it can't be used again."""

    permission_classes = [permissions.IsAuthenticated]

    def post(self, request):
        serializer = LogoutSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        try:
            RefreshToken(serializer.validated_data["refresh"]).blacklist()
        except TokenError as exc:
            raise AuthenticationFailed(f"Invalid refresh token: {exc}")
        return Response(status=status.HTTP_205_RESET_CONTENT)


class MeView(generics.RetrieveAPIView):
    serializer_class = UserSerializer
    permission_classes = [permissions.IsAuthenticated]

    def get_object(self):
        return self.request.user
