from unittest.mock import patch

from django.test import TestCase

from .models import AllowedEmail, User


class RegisterAllowlistTests(TestCase):
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


class GoogleAuthAllowlistTests(TestCase):
    """POST /api/auth/google/ must apply the same allowlist to brand-new
    accounts, but never block an email that already has one -- login and
    Google-account-linking for an existing user must keep working even if
    that email isn't (or is no longer) on the allowlist."""

    def _mock_payload(self, email, sub="google-sub-1", name="Google User"):
        return {"sub": sub, "email": email, "name": name}

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
