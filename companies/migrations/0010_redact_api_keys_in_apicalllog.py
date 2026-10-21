import re

from django.db import migrations
from django.db.models import Q

# Copy of companies.services.http._SECRET_QUERY_PARAM -- migrations don't
# import app code, which can change after they're written. Rows logged
# before http.get() started masking URLs still hold FMP/Polygon keys.
_SECRET_QUERY_PARAM = re.compile(r"(?<!\w)(api_?key|access_token|token)=[^&#\s'\")]+", re.IGNORECASE)
BATCH_SIZE = 2000


def redact_logged_urls(apps, schema_editor):
    ApiCallLog = apps.get_model("companies", "ApiCallLog")
    candidates = ApiCallLog.objects.filter(Q(url__icontains="key=") | Q(url__icontains="token=")).only("id", "url")
    batch = []
    for log in candidates.iterator(chunk_size=BATCH_SIZE):
        redacted = _SECRET_QUERY_PARAM.sub(r"\1=***", log.url)
        if redacted != log.url:
            log.url = redacted
            batch.append(log)
        if len(batch) >= BATCH_SIZE:
            ApiCallLog.objects.bulk_update(batch, ["url"])
            batch = []
    if batch:
        ApiCallLog.objects.bulk_update(batch, ["url"])


class Migration(migrations.Migration):

    dependencies = [
        ("companies", "0009_dailybar"),
    ]

    operations = [
        # Not reversible in any meaningful sense -- the keys are gone.
        migrations.RunPython(redact_logged_urls, migrations.RunPython.noop),
    ]
