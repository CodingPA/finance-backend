from django.contrib.auth import get_user_model
from django.contrib.auth.password_validation import validate_password
from rest_framework import serializers

from .models import AllowedEmail

User = get_user_model()


class UserSerializer(serializers.ModelSerializer):
    initials = serializers.ReadOnlyField()

    class Meta:
        model = User
        fields = ("id", "email", "name", "initials", "date_joined")
        read_only_fields = fields


class RegisterSerializer(serializers.ModelSerializer):
    password = serializers.CharField(write_only=True, validators=[validate_password])

    class Meta:
        model = User
        fields = ("email", "name", "password")

    def validate_email(self, value):
        value = value.strip().lower()
        if not AllowedEmail.objects.filter(email=value).exists():
            raise serializers.ValidationError("This email isn't on the sign-up allowlist.")
        return value

    def create(self, validated_data):
        return User.objects.create_user(**validated_data)


class GoogleAuthSerializer(serializers.Serializer):
    id_token = serializers.CharField()


class LogoutSerializer(serializers.Serializer):
    refresh = serializers.CharField()
