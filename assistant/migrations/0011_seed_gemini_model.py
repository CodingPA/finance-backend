from decimal import Decimal

from django.db import migrations

# Gemini 3.1 Pro Preview -- released 2026-02-19. The "-customtools" model
# variant (same price as the base gemini-3.1-pro-preview) is purpose-built
# for exactly this app's shape: an agentic loop with several custom
# function tools and no bash tool -- Google's own guidance is to use it
# when "the model ignores your custom tools in favor of bash commands" on
# the base variant. Pricing is short-context tier (prompts <=200K tokens,
# which every wake-up prompt in this app is nowhere close to); above 200K
# Google charges $4/$18 instead -- not modeled here, same precedent as
# GPT-5.6 Sol's tiered pricing not being modeled. is_default stays False
# deliberately -- see AssistantModel's docstring on why a non-Anthropic
# row must never become the chat assistant's default.
def seed_gemini_model(apps, schema_editor):
    AssistantModel = apps.get_model("assistant", "AssistantModel")
    AssistantModel.objects.get_or_create(
        model_id="gemini-3.1-pro-preview-customtools",
        defaults={
            "display_name": "Gemini 3.1 Pro Preview",
            "provider": "gemini",
            "is_active": True,
            "is_default": False,
            "max_input_tokens": 1_000_000,
            "input_price_per_million": Decimal("2.00"),
            "output_price_per_million": Decimal("12.00"),
        },
    )


def unseed_gemini_model(apps, schema_editor):
    AssistantModel = apps.get_model("assistant", "AssistantModel")
    AssistantModel.objects.filter(model_id="gemini-3.1-pro-preview-customtools").delete()


class Migration(migrations.Migration):

    dependencies = [
        ("assistant", "0010_alter_assistantmodel_provider"),
    ]

    operations = [
        migrations.RunPython(seed_gemini_model, unseed_gemini_model),
    ]
