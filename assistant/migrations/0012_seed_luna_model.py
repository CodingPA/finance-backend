from decimal import Decimal

from django.db import migrations

# GPT-5.6 Luna -- OpenAI's cheap/mini tier, for the reddit_tickers.py
# structured-extraction utility (companies/services/reddit_tickers.py)
# only, never for a chat conversation or a league AI competitor's own
# wake-up loop. is_active=False deliberately -- both GET /api/league/
# models/ and any assistant-side model picker filter on is_active, and
# this row existing there would let someone pick it for a full agentic
# turn it was never evaluated for. The extraction utility looks it up
# directly by model_id (a plain .get(), not an is_active-filtered
# picker query), so is_active=False doesn't block its own use.
def seed_luna_model(apps, schema_editor):
    AssistantModel = apps.get_model("assistant", "AssistantModel")
    AssistantModel.objects.get_or_create(
        model_id="gpt-5.6-luna",
        defaults={
            "display_name": "GPT-5.6 Luna",
            "provider": "openai",
            "is_active": False,
            "is_default": False,
            "input_price_per_million": Decimal("0.20"),
            "output_price_per_million": Decimal("1.20"),
        },
    )


def unseed_luna_model(apps, schema_editor):
    AssistantModel = apps.get_model("assistant", "AssistantModel")
    AssistantModel.objects.filter(model_id="gpt-5.6-luna").delete()


class Migration(migrations.Migration):

    dependencies = [
        ("assistant", "0011_seed_gemini_model"),
    ]

    operations = [
        migrations.RunPython(seed_luna_model, unseed_luna_model),
    ]
