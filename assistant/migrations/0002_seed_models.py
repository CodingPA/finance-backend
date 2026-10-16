from django.db import migrations


def seed_models(apps, schema_editor):
    AssistantModel = apps.get_model("assistant", "AssistantModel")
    AssistantModel.objects.get_or_create(
        model_id="claude-sonnet-5",
        defaults={"display_name": "Claude Sonnet 5", "is_active": True, "is_default": True},
    )
    AssistantModel.objects.get_or_create(
        model_id="claude-opus-5",
        defaults={"display_name": "Claude Opus 5", "is_active": True, "is_default": False},
    )


def unseed_models(apps, schema_editor):
    AssistantModel = apps.get_model("assistant", "AssistantModel")
    AssistantModel.objects.filter(model_id__in=["claude-sonnet-5", "claude-opus-5"]).delete()


class Migration(migrations.Migration):

    dependencies = [
        ("assistant", "0001_initial"),
    ]

    operations = [
        migrations.RunPython(seed_models, unseed_models),
    ]
