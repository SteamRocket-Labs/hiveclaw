---
name: Hiveclaw Channel Patterns
description: Key patterns for channel integrations in Hiveclaw -- ChannelConfig model lacks tenant_id, stream managers are singletons, _call_agent_llm lives in feishu.py, secrets use get_secrets_provider, rate limiting only on webhooks
type: project
---

Channel integrations follow a consistent pattern:
- ChannelConfig model stores per-agent config in extra_config JSON, scoped by agent_id + channel_type unique constraint
- ChannelConfig has NO direct tenant_id column; tenant scoping relies on agent_id -> agents.tenant_id FK
- Stream managers (WeComStreamManager, FeishuWSManager, DingtalkStreamManager) are singletons started in main.py lifespan
- _call_agent_llm() is defined in app/api/feishu.py and reused by other channels -- it has session_source and session_channel params
- Secrets are encrypted via get_secrets_provider().encrypt/decrypt from app/services/secrets_provider.py
- Rate limiting exists only on webhook endpoints (app/core/rate_limiter.py), not on channel-specific API routes
- check_agent_access() from app/core/permissions.py enforces tenant scoping at the API layer

**Why:** Understanding these patterns prevents false positives in code reviews and ensures new channels follow existing conventions.

**How to apply:** Compare new channel code against these established patterns when reviewing. Flag deviations that weaken security or consistency.
