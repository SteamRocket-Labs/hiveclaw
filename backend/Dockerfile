# ─── Backend Dockerfile (Optimized) ─────────────────────
# Multi-stage: deps → production
FROM python:3.12-slim AS deps

WORKDIR /app
RUN apt-get update && \
    apt-get install -y --no-install-recommends gcc libpq-dev && \
    rm -rf /var/lib/apt/lists/*

COPY pyproject.toml ./
ARG CLAWITH_PIP_INDEX_URL
ARG CLAWITH_PIP_TRUSTED_HOST
RUN if [ -n "$CLAWITH_PIP_INDEX_URL" ] && [ -n "$CLAWITH_PIP_TRUSTED_HOST" ]; then \
        pip install --no-cache-dir --index-url "$CLAWITH_PIP_INDEX_URL" --trusted-host "$CLAWITH_PIP_TRUSTED_HOST" .; \
    elif [ -n "$CLAWITH_PIP_INDEX_URL" ]; then \
        pip install --no-cache-dir --index-url "$CLAWITH_PIP_INDEX_URL" .; \
    else \
        pip install --no-cache-dir .; \
    fi

# ─── Production ─────────────────────────────────────────
FROM python:3.12-slim AS production

WORKDIR /app
RUN apt-get update && \
    apt-get install -y --no-install-recommends libpq5 curl bubblewrap libicu76 && \
    rm -rf /var/lib/apt/lists/*

ARG OFFICECLI_DOWNLOAD_URL
ARG OFFICECLI_SHA256
RUN if [ -n "$OFFICECLI_DOWNLOAD_URL" ]; then \
        if [ -z "$OFFICECLI_SHA256" ]; then \
            echo "OFFICECLI_SHA256 is required when OFFICECLI_DOWNLOAD_URL is set" >&2; \
            exit 1; \
        fi; \
        curl -fsSL "$OFFICECLI_DOWNLOAD_URL" -o /usr/local/bin/officecli; \
        echo "$OFFICECLI_SHA256  /usr/local/bin/officecli" | sha256sum -c -; \
        chmod 0755 /usr/local/bin/officecli; \
    fi
ENV OFFICECLI_SKIP_UPDATE=1

# Copy installed packages from deps stage
COPY --from=deps /usr/local/lib/python3.12/site-packages/ /usr/local/lib/python3.12/site-packages/
COPY --from=deps /usr/local/bin/ /usr/local/bin/

# Copy application code
COPY . .

RUN useradd --create-home hive && \
    mkdir -p /data/agents && \
    chmod +x /app/entrypoint.sh && \
    chown -R hive:hive /app /data

# Health check
HEALTHCHECK --interval=30s --timeout=5s --retries=3 \
    CMD curl -f http://localhost:8000/api/health || exit 1

EXPOSE 8000
# entrypoint.sh runs `alembic upgrade head` then `uvicorn`
ENTRYPOINT ["/bin/bash", "/app/entrypoint.sh"]
