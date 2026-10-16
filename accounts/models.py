import uuid

from django.contrib.auth.models import AbstractBaseUser, PermissionsMixin
from django.db import models

from .managers import UserManager


class User(AbstractBaseUser, PermissionsMixin):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    email = models.EmailField(unique=True)
    name = models.CharField(max_length=150, blank=True)

    # Set when the account was created or linked via Google sign-in (Google's
    # stable per-user subject ID). Null for email/password-only accounts.
    google_sub = models.CharField(max_length=255, unique=True, null=True, blank=True)

    is_active = models.BooleanField(default=True)
    is_staff = models.BooleanField(default=False)
    date_joined = models.DateTimeField(auto_now_add=True)

    objects = UserManager()

    USERNAME_FIELD = "email"
    REQUIRED_FIELDS = []

    def __str__(self):
        return self.email

    @property
    def initials(self):
        parts = self.name.split()
        if not parts:
            return self.email[:2].upper()
        if len(parts) == 1:
            return parts[0][:2].upper()
        return (parts[0][0] + parts[-1][0]).upper()


class AllowedEmail(models.Model):
    """Sign-up allowlist -- only an email with a row here may create a NEW
    account, checked in RegisterSerializer.validate_email (email/password)
    and GoogleAuthView.post (Google) at the moment of account creation.
    Never gates anything for an email that already has an account (logging
    in, or linking Google to an existing email/password account) -- removing
    a row here doesn't lock out someone who already signed up. Managed
    entirely through /admin/ -- add a row, that person can sign up
    immediately, no deploy needed."""

    email = models.EmailField(unique=True)
    note = models.CharField(max_length=255, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return self.email

    def save(self, *args, **kwargs):
        self.email = self.email.strip().lower()
        super().save(*args, **kwargs)
