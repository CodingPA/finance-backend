from unittest.mock import patch

from django.core.cache import cache
from django.test import TestCase
from rest_framework.throttling import ScopedRateThrottle

from .models import AllowedEmail, User


class AuthTestCase(TestCase):
    """Auth endpoints are rate-limited per client IP (ScopedRateThrottle,
    scope "auth"), and every test client request comes from the same
    127.0.0.1 -- clearing the cache keeps one test's requests from
    counting against the next one's limit."""

    def setUp(self):
        cache.clear()


class RegisterAllowlistTests(AuthTestCase):
    """POST /api/auth/register/ must reject an email with no AllowedEmail
    row -- the sign-up allowlist (accounts.models.AllowedEmail)."""

    def test_non_allowlisted_email_rejected(self):
        response = self.client.post(
            "/api/auth/register/",
            {"email": "stranger@example.com", "name": "Stranger", "password": "a-strong-password-1"},
        )
        self.assertEqual(response.status_code, 400)
        self.assertIn("email", response.json())
        self.assertFalse(User.objects.filter(email="stranger@example.com").exists())

    def test_allowlisted_email_can_register(self):
        AllowedEmail.objects.create(email="friend@example.com")
        response = self.client.post(
            "/api/auth/register/",
            {"email": "friend@example.com", "name": "Friend", "password": "a-strong-password-1"},
        )
        self.assertEqual(response.status_code, 201)
        self.assertTrue(User.objects.filter(email="friend@example.com").exists())

    def test_allowlist_check_is_case_insensitive(self):
        AllowedEmail.objects.create(email="Friend@Example.com")
        response = self.client.post(
            "/api/auth/register/",
            {"email": "friend@example.com", "name": "Friend", "password": "a-strong-password-1"},
        )
        self.assertEqual(response.status_code, 201)


class GoogleAuthAllowlistTests(AuthTestCase):
    """POST /api/auth/google/ must apply the same allowlist to brand-new
    accounts, but never block an email that already has one -- login and
    Google-account-linking for an existing user must keep working even if
    that email isn't (or is no longer) on the allowlist."""

    def _mock_payload(self, email, sub="google-sub-1", name="Google User", email_verified=True):
        return {"sub": sub, "email": email, "name": name, "email_verified": email_verified}

    @patch("accounts.views.google_id_token.verify_oauth2_token")
    def test_non_allowlisted_new_email_rejected(self, mock_verify):
        mock_verify.return_value = self._mock_payload("stranger@example.com")
        with self.settings(GOOGLE_OAUTH_CLIENT_ID="test-client-id"):
            response = self.client.post("/api/auth/google/", {"id_token": "fake-token"})
        self.assertEqual(response.status_code, 401)
        self.assertFalse(User.objects.filter(email="stranger@example.com").exists())

    @patch("accounts.views.google_id_token.verify_oauth2_token")
    def test_allowlisted_new_email_creates_account(self, mock_verify):
        AllowedEmail.objects.create(email="friend@example.com")
        mock_verify.return_value = self._mock_payload("friend@example.com")
        with self.settings(GOOGLE_OAUTH_CLIENT_ID="test-client-id"):
            response = self.client.post("/api/auth/google/", {"id_token": "fake-token"})
        self.assertEqual(response.status_code, 200)
        self.assertTrue(User.objects.filter(email="friend@example.com").exists())

    @patch("accounts.views.google_id_token.verify_oauth2_token")
    def test_existing_email_password_account_can_still_link_google(self, mock_verify):
        # Not on the allowlist -- simulates a row removed after the account
        # already existed. Linking must still succeed; the allowlist only
        # gates brand-new account creation.
        User.objects.create_user(email="existing@example.com", password="whatever-123")
        mock_verify.return_value = self._mock_payload("existing@example.com")
        with self.settings(GOOGLE_OAUTH_CLIENT_ID="test-client-id"):
            response = self.client.post("/api/auth/google/", {"id_token": "fake-token"})
        self.assertEqual(response.status_code, 200)
        user = User.objects.get(email="existing@example.com")
        self.assertEqual(user.google_sub, "google-sub-1")

    @patch("accounts.views.google_id_token.verify_oauth2_token")
    def test_existing_google_linked_account_can_still_log_in(self, mock_verify):
        # Same "removed from allowlist after the fact" scenario, but for a
        # user who already has this google_sub -- must never hit the
        # allowlist check at all (found on the first lookup).
        user = User.objects.create_user(email="existing@example.com", password="whatever-123")
        user.google_sub = "google-sub-1"
        user.save(update_fields=["google_sub"])
        mock_verify.return_value = self._mock_payload("existing@example.com")
        with self.settings(GOOGLE_OAUTH_CLIENT_ID="test-client-id"):
            response = self.client.post("/api/auth/google/", {"id_token": "fake-token"})
        self.assertEqual(response.status_code, 200)


class GoogleAuthIdentityTests(AuthTestCase):
    """POST /api/auth/google/ links to an existing account by email only
    when Google vouches for that email, and never hands an account that's
    already linked to one Google identity to a different one."""

    def _mock_payload(self, email, sub="google-sub-1", email_verified=True):
        return {"sub": sub, "email": email, "name": "Google User", "email_verified": email_verified}

    @patch("accounts.views.google_id_token.verify_oauth2_token")
    def test_unverified_email_cannot_take_over_existing_account(self, mock_verify):
        User.objects.create_user(email="existing@example.com", password="whatever-123")
        mock_verify.return_value = self._mock_payload("existing@example.com", email_verified=False)
        with self.settings(GOOGLE_OAUTH_CLIENT_ID="test-client-id"):
            response = self.client.post("/api/auth/google/", {"id_token": "fake-token"})
        self.assertEqual(response.status_code, 401)
        self.assertNotIn("access", response.json())
        self.assertIsNone(User.objects.get(email="existing@example.com").google_sub)

    @patch("accounts.views.google_id_token.verify_oauth2_token")
    def test_unverified_email_cannot_sign_up(self, mock_verify):
        AllowedEmail.objects.create(email="friend@example.com")
        mock_verify.return_value = self._mock_payload("friend@example.com", email_verified=False)
        with self.settings(GOOGLE_OAUTH_CLIENT_ID="test-client-id"):
            response = self.client.post("/api/auth/google/", {"id_token": "fake-token"})
        self.assertEqual(response.status_code, 401)
        self.assertFalse(User.objects.filter(email="friend@example.com").exists())

    @patch("accounts.views.google_id_token.verify_oauth2_token")
    def test_different_google_identity_cannot_log_into_linked_account(self, mock_verify):
        user = User.objects.create_user(email="existing@example.com", password="whatever-123")
        user.google_sub = "google-sub-original"
        user.save(update_fields=["google_sub"])
        mock_verify.return_value = self._mock_payload("existing@example.com", sub="google-sub-newcomer")
        with self.settings(GOOGLE_OAUTH_CLIENT_ID="test-client-id"):
            response = self.client.post("/api/auth/google/", {"id_token": "fake-token"})
        self.assertEqual(response.status_code, 401)
        self.assertNotIn("access", response.json())
        user.refresh_from_db()
        self.assertEqual(user.google_sub, "google-sub-original")


class AuthThrottleTests(AuthTestCase):
    """Login/register/Google sign-in share one per-IP rate limit (scope
    "auth"). The rate is patched low here rather than overridden via
    settings: DRF copies DEFAULT_THROTTLE_RATES onto the throttle class at
    import time, so override_settings wouldn't reach it."""

    @patch.object(ScopedRateThrottle, "THROTTLE_RATES", {"auth": "2/min"})
    def test_login_is_rate_limited(self):
        User.objects.create_user(email="existing@example.com", password="whatever-123")
        bad_login = {"email": "existing@example.com", "password": "wrong-password"}
        for _ in range(2):
            self.assertEqual(self.client.post("/api/auth/login/", bad_login).status_code, 401)
        self.assertEqual(self.client.post("/api/auth/login/", bad_login).status_code, 429)

    @patch.object(ScopedRateThrottle, "THROTTLE_RATES", {"auth": "2/min"})
    def test_limit_is_shared_across_auth_endpoints(self):
        bad_login = {"email": "nobody@example.com", "password": "wrong-password"}
        self.client.post("/api/auth/login/", bad_login)
        self.client.post("/api/auth/login/", bad_login)
        response = self.client.post(
            "/api/auth/register/",
            {"email": "stranger@example.com", "name": "Stranger", "password": "a-strong-password-1"},
        )
        self.assertEqual(response.status_code, 429)

    @patch.object(ScopedRateThrottle, "THROTTLE_RATES", {"auth": "2/min"})
    def test_spoofed_forwarded_for_does_not_reset_the_limit(self):
        bad_login = {"email": "nobody@example.com", "password": "wrong-password"}
        for i in range(2):
            self.client.post("/api/auth/login/", bad_login, HTTP_X_FORWARDED_FOR=f"10.0.0.{i}")
        response = self.client.post("/api/auth/login/", bad_login, HTTP_X_FORWARDED_FOR="10.0.0.99")
        self.assertEqual(response.status_code, 429)
