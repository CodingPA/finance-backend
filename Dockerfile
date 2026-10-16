# One image, three roles (web/worker/beat -- see docker-compose.yml's
# `command:` overrides), mirroring deploy/'s three systemd units. This is
# a Docker onboarding path for evaluating/developing the project, not a
# replacement for the native systemd deployment docs/deployment.md
# documents -- that stays the recommended way to run this in production.
#
# Known limitation: no GPU support. torch (pulled in only when building
# with INSTALL_FILINGS=true, see below) always installs CPU-only here --
# there's no device reservation in docker-compose.yml and no expectation
# that nvidia-container-toolkit is set up on the host. Given the actual
# workload (occasional docling parsing of SEC filings, not high-throughput
# ML inference), that's a deliberate scope call, not an oversight: making
# GPU access real needs a host-specific device reservation this Dockerfile
# can't respond to (and can't be verified working in the environment this
# was authored in). If you have a GPU and want to use it here, you'd need
# to drop the CPU-only pip install below, install a CUDA-matched torch
# build instead, and add a `deploy.resources.reservations.devices` block
# to docker-compose.yml's `worker` service yourself.
FROM python:3.13-slim

# Off by default so a plain `docker compose build` gives the smallest
# image. Match these to requirements.txt's own split (see that file's
# header comment): pass --build-arg INSTALL_FILINGS=true for SEC filing
# ingestion (pulls in docling-slim's ~1.2GB torch stack), or
# INSTALL_MEDIA=true for the media app's dependencies.
#
# Filing ingestion has no ENABLE_* runtime flag -- its heavy imports are
# already deferred to the one function that needs them (F-02), so an
# unconfigured deployment pays nothing regardless of this build arg; only
# actually calling propose_filing_ingestion needs INSTALL_FILINGS=true to
# have been used at build time.
#
# Media DOES have a separate runtime flag (ENABLE_MEDIA, core/
# settings.py) from this build arg -- one controls what's INSTALLED in
# the image, the other what's TURNED ON at runtime. Setting
# ENABLE_MEDIA=true without also building with INSTALL_MEDIA=true starts
# fine (the media app itself has no heavy import-time dependency), but
# actually processing an upload fails with a clear error the moment
# ffmpeg is needed and isn't found.
ARG INSTALL_FILINGS=false
ARG INSTALL_MEDIA=false

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

# ffmpeg/ffprobe (media/services/ffmpeg.py resolves them from PATH) --
# only installed when the media extras are, same reasoning as the
# requirements split: most deployments don't need this feature at all.
RUN if [ "$INSTALL_MEDIA" = "true" ]; then \
        apt-get update && \
        apt-get install -y --no-install-recommends ffmpeg && \
        rm -rf /var/lib/apt/lists/*; \
    fi

# Dependencies copied and installed before the rest of the source so
# this layer stays cached across code-only changes.
#
# INSTALL_FILINGS's torch install: deliberately CPU-only, always, via a
# separate `pip install torch torchvision --index-url .../cpu` BEFORE
# requirements-filings.txt -- same trick as docs/deployment.md's native
# setup (confirmed live there: ~4GB smaller, no functional difference,
# since torch runs on CPU automatically when no GPU is present either
# way). Installing CPU-only torch first means requirements-filings.txt
# finds torch already satisfied and never reaches PyPI's default (GPU)
# build. Comments deliberately kept OUT of this RUN instruction's own
# shell continuation lines -- a `#` inside a backslash-continued RUN
# block is a well-known Dockerfile footgun, and this was never actually
# build-tested in the environment that authored it.
COPY requirements.txt requirements-filings.txt requirements-media.txt ./
RUN pip install --no-cache-dir --upgrade pip && \
    pip install --no-cache-dir -r requirements.txt && \
    if [ "$INSTALL_FILINGS" = "true" ]; then \
        pip install --no-cache-dir torch torchvision --index-url https://download.pytorch.org/whl/cpu && \
        pip install --no-cache-dir -r requirements-filings.txt; \
    fi && \
    if [ "$INSTALL_MEDIA" = "true" ]; then pip install --no-cache-dir -r requirements-media.txt; fi

# Run as an unprivileged user, not root. /app itself is chowned too, not
# just what COPY --chown writes into it: beat writes celerybeat-schedule
# into the working directory, and the local-disk upload fallback
# (MEDIA_ROOT, core/settings.py) creates /app/uploads.
RUN useradd --create-home --shell /usr/sbin/nologin app && chown app:app /app
COPY --chown=app:app . .
USER app

# 8001 to match the port the README/docs already use for local (non-
# Docker) uvicorn -- one fewer thing to remember when moving between the
# two setups.
EXPOSE 8001

# No entrypoint script -- migrations run once, in their own compose
# service (docker-compose.yml's `migrate`), not redundantly in every
# container's own startup. An earlier version of this Dockerfile ran
# `manage.py migrate` from a shared entrypoint on all three services;
# confirmed live (a real docker compose up, not reasoned about) that
# this loses a real race the first time it runs against a brand-new
# database -- multiple containers cold-starting against the same empty
# Postgres all see "django_migrations doesn't exist" and race to CREATE
# TABLE it simultaneously, and only one wins; the others crash with
# `duplicate key value violates unique constraint
# "pg_class_relname_nsp_index"`. A dedicated one-shot service that the
# others wait to fully complete (docker-compose.yml's
# `service_completed_successfully` condition) is the actual fix, not
# something an entrypoint script can paper over.
CMD ["uvicorn", "core.asgi:application", "--host", "0.0.0.0", "--port", "8001"]
