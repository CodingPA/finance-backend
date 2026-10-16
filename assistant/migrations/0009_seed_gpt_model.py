from decimal import Decimal

from django.db import migrations

# GPT-5.6 Sol -- OpenAI's flagship as of 2026-08-12 (released 2026-07-09).
# Standard short-context pricing ($5/$30 per million); above 272K input
# tokens in a single request OpenAI bills $10/$45 instead -- not modeled
# here (cost_for() has no tiered-pricing concept, matching how the
# Anthropic rows already don't model cache-tier pricing either), and this
# app's wake-up prompts are nowhere near that threshold. is_default stays
# False deliberately -- see AssistantModel's docstring on why an OpenAI
# row must never become the chat assistant's default.
def seed_gpt_model(apps, schema_editor):
    AssistantModel = apps.get_model("assistant", "AssistantModel")
    AssistantModel.objects.get_or_create(
        model_id="gpt-5.6-sol",
        defaults={
            "display_name": "GPT-5.6 Sol",
            "provider": "openai",
            "is_active": True,
            "is_default": False,
            "max_input_tokens": 1_050_000,
            "input_price_per_million": Decimal("5.00"),
            "output_price_per_million": Decimal("30.00"),
        },
    )


def unseed_gpt_model(apps, schema_editor):
    AssistantModel = apps.get_model("assistant", "AssistantModel")
    AssistantModel.objects.filter(model_id="gpt-5.6-sol").delete()


class Migration(migrations.Migration):

    dependencies = [
        ("assistant", "0008_assistantmodel_provider"),
    ]

    operations = [
        migrations.RunPython(seed_gpt_model, unseed_gpt_model),
    ]
