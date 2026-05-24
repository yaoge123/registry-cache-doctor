# syntax=docker/dockerfile:1.7

# Stage 1: build wheel ---------------------------------------------------------
FROM python:3.13-alpine AS builder

WORKDIR /build
RUN pip install --no-cache-dir build hatchling

COPY pyproject.toml README.md LICENSE ./
COPY src ./src
RUN python -m build --wheel --outdir /dist .

# Stage 2: runtime -------------------------------------------------------------
FROM python:3.13-alpine AS runtime

# supercronic provides the daemon entrypoint when invoked as `daemon`.
# Pin a known-good release for reproducibility.
ARG SUPERCRONIC_VERSION=v0.2.30
ARG SUPERCRONIC_SHA256=55f3a65b6ef29856d948230a96448f6ec7376d39fca367fae49d2512167e29e5

# tzdata ships /usr/share/zoneinfo so the standard TZ env var works without
# needing the host to bind-mount its zoneinfo tree into the container.
RUN apk add --no-cache curl tini ca-certificates tzdata \
    && curl -fsSL -o /usr/local/bin/supercronic \
        "https://github.com/aptible/supercronic/releases/download/${SUPERCRONIC_VERSION}/supercronic-linux-amd64" \
    && echo "${SUPERCRONIC_SHA256}  /usr/local/bin/supercronic" | sha256sum -c - \
    && chmod +x /usr/local/bin/supercronic \
    && apk del curl

# Non-root runtime user
RUN addgroup -S rcd && adduser -S -G rcd rcd

WORKDIR /app

COPY --from=builder /dist/*.whl /tmp/
RUN pip install --no-cache-dir /tmp/*.whl && rm -f /tmp/*.whl

COPY entrypoint.sh /usr/local/bin/entrypoint.sh
COPY scripts/rcd-cron-exec.sh /usr/local/bin/rcd-cron-exec
RUN chmod +x /usr/local/bin/entrypoint.sh /usr/local/bin/rcd-cron-exec

USER rcd

ENV RCD_CONFIG=/etc/rcd/config.toml \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

ENTRYPOINT ["/sbin/tini", "--", "/usr/local/bin/entrypoint.sh"]
CMD ["scan"]
