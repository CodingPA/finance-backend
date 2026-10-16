from decimal import Decimal

from django.db import migrations

# Sonnet 5's $2/$10 is introductory pricing through 2026-08-31 (base rate is
# $3/$15) -- whoever's maintaining this after that date should update the
# row in /admin/ rather than relying on this migration re-running.
PRICING = {
    "claude-sonnet-5": {"input": Decimal("2.00"), "output": Decimal("10.00"), "max_input_tokens": 1_000_000},
    "claude-opus-5": {"input": Decimal("5.00"), "output": Decimal("25.00"), "max_input_tokens": 1_000_000},
}


def seed_pricing(apps, schema_editor):
    AssistantModel = apps.get_model("assistant", "AssistantModel")
    for model_id, prices in PRICING.items():
        AssistantModel.objects.filter(model_id=model_id).update(
            input_price_per_million=prices["input"],
            output_price_per_million=prices["output"],
            max_input_tokens=prices["max_input_tokens"],
        )


def unseed_pricing(apps, schema_editor):
    AssistantModel = apps.get_model("assistant", "AssistantModel")
    AssistantModel.objects.filter(model_id__in=PRICING.keys()).update(
        input_price_per_million=Decimal("0"),
        output_price_per_million=Decimal("0"),
        max_input_tokens=1_000_000,
    )


class Migration(migrations.Migration):

    dependencies = [
        ("assistant", "0003_assistantmodel_input_price_per_million_and_more"),
    ]

    operations = [
        migrations.RunPython(seed_pricing, unseed_pricing),
    ]
