# Security & Compliance Atomic Audit — HEAD 0a0dec170 (2026-07-13)

## Confirmed findings

### SSRF — web_fetch (P0 candidate)
- `backend/app/services/agent_tool_domains/web_mcp.py:196` `_looks_like_url` only checks `parsed.netloc and "." in parsed.netloc`
- `_normalize_url:204` uses `_looks_like_url` — no private-IP / metadata block
- `_web_fetch:1260` → `_normalize_url` → `httpx.AsyncClient(follow_redirects=True, timeout=20).get(normalized_url)` at :1295-1296. NO SSRF guard.
- follow_redirects=True → even a domain-fronted redirect to 169.254.169.254 passes
- Attack input: `web_fetch(url="http://169.254.169.254/latest/meta-data/iam/security-credentials/")` — has dots → passes; `http://metadata.google.internal/computeMetadata/v1/` also passes
- Also _advanced_web_fetch:1047, _firecrawl_fetch, _exa_fetch, _xcrawl — but those go through external providers (less direct). Direct httpx.get is _web_fetch.
- STATUS: confirmed open in source. Need to confirm web_fetch is registered/reachable by agents.

## To investigate
- path traversal (workspace.py + filesystem handler)
- secrets (channel_secret_storage, config SECRETS_MASTER_KEY, plaintext creds in DB/logs)
- audit immutability (migrations TRIGGER)
- MCP trust (mcp_authz)
- sandbox/code_execution raw subprocess fallback
- tenant isolation cross-tenant model
- rate limit/quota/budget
- prompt injection
