# Backend image. Build context is backend/.
# Runtime dependencies come from uv.lock via uv 0.9.21.
# --locked fails when the lock is missing or no longer matches pyproject.toml.
# --frozen would install the lock without that drift check, so it is not used.
# This image does not install Node or lark-cli; those stay on the root image.
FROM python:3.12-slim AS deps

WORKDIR /app
RUN apt-get update && \
    apt-get install -y --no-install-recommends gcc libpq-dev && \
    rm -rf /var/lib/apt/lists/*

COPY pyproject.toml uv.lock ./
COPY scripts/patch_lark_oapi.py ./scripts/patch_lark_oapi.py
ARG HIVE_PIP_INDEX_URL
ARG HIVE_PIP_TRUSTED_HOST
ARG CLAWITH_PIP_INDEX_URL
ARG CLAWITH_PIP_TRUSTED_HOST
RUN EFFECTIVE_PIP_INDEX_URL="${HIVE_PIP_INDEX_URL:-$CLAWITH_PIP_INDEX_URL}"; \
    EFFECTIVE_PIP_TRUSTED_HOST="${HIVE_PIP_TRUSTED_HOST:-$CLAWITH_PIP_TRUSTED_HOST}"; \
    set -eu; \
    if [ -n "$EFFECTIVE_PIP_INDEX_URL" ] && [ -n "$EFFECTIVE_PIP_TRUSTED_HOST" ]; then \
        pip install --no-cache-dir --index-url "$EFFECTIVE_PIP_INDEX_URL" --trusted-host "$EFFECTIVE_PIP_TRUSTED_HOST" "uv==0.9.21"; \
    elif [ -n "$EFFECTIVE_PIP_INDEX_URL" ]; then \
        pip install --no-cache-dir --index-url "$EFFECTIVE_PIP_INDEX_URL" "uv==0.9.21"; \
    else \
        pip install --no-cache-dir "uv==0.9.21"; \
    fi; \
    export UV_PYTHON_DOWNLOADS=never UV_LINK_MODE=copy; \
    uv export --python /usr/local/bin/python --locked --no-dev --no-emit-project \
        --format requirements.txt --output-file /tmp/requirements.lock.txt; \
    if [ -n "$EFFECTIVE_PIP_INDEX_URL" ] && [ -n "$EFFECTIVE_PIP_TRUSTED_HOST" ]; then \
        uv pip install --python /usr/local/bin/python --system --require-hashes --no-cache \
            --default-index "$EFFECTIVE_PIP_INDEX_URL" --allow-insecure-host "$EFFECTIVE_PIP_TRUSTED_HOST" \
            -r /tmp/requirements.lock.txt; \
    elif [ -n "$EFFECTIVE_PIP_INDEX_URL" ]; then \
        uv pip install --python /usr/local/bin/python --system --require-hashes --no-cache \
            --default-index "$EFFECTIVE_PIP_INDEX_URL" \
            -r /tmp/requirements.lock.txt; \
    else \
        uv pip install --python /usr/local/bin/python --system --require-hashes --no-cache \
            -r /tmp/requirements.lock.txt; \
    fi; \
    rm -f /tmp/requirements.lock.txt
RUN python scripts/patch_lark_oapi.py

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
