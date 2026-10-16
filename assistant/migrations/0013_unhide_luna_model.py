from django.db import migrations

# Flips gpt-5.6-luna's is_active back to True now that assistant/tasks.py
# has a real OpenAI call loop (_run_openai_turn) -- see 0012_seed_luna_model
# for why it was seeded inactive in the first place (kept out of every
# model picker, chat and league both, since it had never been evaluated
# for a full agentic turn). That reasoning still applies to QUALITY, not
# availability -- it's a cheap structured-output model, never exercised
# against this app's ~20-tool agentic loop, so expect it to underperform
# Claude/GPT-5.6 Sol in actual chat use. Unhiding it is a deliberate
# product decision to make that tradeoff selectable, not a claim that it's
# been vetted as good for this.
#
# reddit_tickers.py's own extraction call looks this row up by exact
# model_id (a plain .get(), never through an is_active-filtered query),
# so this migration has no effect on that usage either way.
#
# .update(), not .save() on a fetched instance -- idempotent and
# side-effect-free if this ever needs to run again (e.g. reversed then
# re-applied), matching how 0012 itself used get_or_create rather than
# assuming a fresh insert.
def unhide_luna_model(apps, schema_editor):
    AssistantModel = apps.get_model("assistant", "AssistantModel")
    AssistantModel.objects.filter(model_id="gpt-5.6-luna").update(is_active=True)


def rehide_luna_model(apps, schema_editor):
    AssistantModel = apps.get_model("assistant", "AssistantModel")
    AssistantModel.objects.filter(model_id="gpt-5.6-luna").update(is_active=False)


class Migration(migrations.Migration):

    dependencies = [
        ("assistant", "0012_seed_luna_model"),
    ]

    operations = [
        migrations.RunPython(unhide_luna_model, rehide_luna_model),
    ]
