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
ARG SUPERCRONIC_SHA256=4d31bbf3b66ac90d2d8cc44eb5e80930c4d3186c30d73b7b41b5fbd2058edc70

RUN apk add --no-cache curl tini ca-certificates \
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
RUN chmod +x /usr/local/bin/entrypoint.sh

USER rcd

ENV RCD_CONFIG=/etc/rcd/config.toml \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

ENTRYPOINT ["/sbin/tini", "--", "/usr/local/bin/entrypoint.sh"]
CMD ["scan"]
