"""Backfill: give every Competition that existed before ForcedCheckin was
introduced the new default (single 8am news scan) floor wake-up, so an
already-running competition doesn't silently drop from 3 guaranteed daily
wake-ups to zero the moment this deploys -- see league/models.py's
DEFAULT_FORCED_CHECKINS/ForcedCheckin docstrings for the strategy change
this is part of. Historical model state only (apps.get_model), not the
live DEFAULT_FORCED_CHECKINS constant -- migrations must stay correct
even if that constant's wording changes later."""

from django.db import migrations

DEFAULT_FORCED_CHECKINS = [
    dict(
        hour=8, minute=0, label="8am news scan",
        prompt=(
            "Nothing else is scheduled before the open. Read overnight/premarket news and macro "
            "data and flag anything that changes your thesis on open positions or your watchlist."
        ),
        inject_news=True,
    ),
]


def seed_defaults(apps, schema_editor):
    Competition = apps.get_model("league", "Competition")
    ForcedCheckin = apps.get_model("league", "ForcedCheckin")
    for competition in Competition.objects.all():
        ForcedCheckin.objects.bulk_create(
            ForcedCheckin(competition=competition, **kwargs) for kwargs in DEFAULT_FORCED_CHECKINS
        )


def unseed_defaults(apps, schema_editor):
    ForcedCheckin = apps.get_model("league", "ForcedCheckin")
    ForcedCheckin.objects.filter(label="8am news scan").delete()


class Migration(migrations.Migration):

    dependencies = [
        ('league', '0005_forcedcheckin'),
    ]

    operations = [
        migrations.RunPython(seed_defaults, unseed_defaults),
    ]
