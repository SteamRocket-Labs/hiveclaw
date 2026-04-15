# Security Governance Design: Four-Layer Permission Model

> Status: Technical Design
> Date: 2026-03-20
> Author: Security Governance Expert
> Based on: AGENT_NATIVE_EXECUTION_PERMISSION_PROPOSAL.md

---

## 1. Executive Summary

This document translates the four-layer permission model (Binding / Access / Capability / Execution Identity) from the proposal into concrete PostgreSQL schemas, migration paths, and operational flows. The design preserves full backward compatibility with the existing `autonomy_policy` L1/L2/L3 system while establishing the foundation for enterprise-grade agent governance.

**Design Principles:**
- Zero-downtime migration: old `autonomy_policy` continues to work during transition
- Tenant-scoped everything: every new table includes `tenant_id`
- Audit-first: every permission change and execution produces an immutable audit record
- Credential separation: secrets never stored alongside configuration

---

## 2. PostgreSQL Schema Design (SQL DDL)

### 2.1 Layer 1: Agent Bindings

Replaces the implicit "creator = owner" model with explicit role-based relationships between users and agents.

```sql
-- Layer 1: Binding — who has what relationship with this agent
CREATE TABLE agent_bindings (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id       UUID NOT NULL REFERENCES tenants(id),
    agent_id        UUID NOT NULL REFERENCES agents(id) ON DELETE CASCADE,
    user_id         UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    binding_role    VARCHAR(20) NOT NULL CHECK (binding_role IN (
                        'owner',        -- Full lifecycle control, can delete agent
                        'operator',     -- Day-to-day operation, can invoke and configure
                        'delegatee',    -- Agent may act on behalf of this user
                        'viewer'        -- Read-only access, audit trail visibility
                    )),
    status          VARCHAR(20) NOT NULL DEFAULT 'active' CHECK (status IN (
                        'active', 'suspended', 'revoked'
                    )),
    granted_by      UUID NOT NULL REFERENCES users(id),
    granted_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    revoked_at      TIMESTAMPTZ,
    revoked_by      UUID REFERENCES users(id),
    metadata        JSONB NOT NULL DEFAULT '{}',

    CONSTRAINT uq_agent_binding UNIQUE (agent_id, user_id, binding_role),
    CONSTRAINT fk_binding_tenant FOREIGN KEY (tenant_id) REFERENCES tenants(id)
);

CREATE INDEX idx_agent_bindings_agent ON agent_bindings(agent_id) WHERE status = 'active';
CREATE INDEX idx_agent_bindings_user ON agent_bindings(user_id) WHERE status = 'active';
CREATE INDEX idx_agent_bindings_tenant ON agent_bindings(tenant_id);
```

**Key decisions:**
- A user can hold multiple roles on the same agent (e.g., both `owner` and `delegatee`)
- `delegatee` is the critical role for Execution Identity: it means the agent may act *as* this user in external systems
- `granted_by` creates an approval chain: who authorized this binding
- Soft-delete via `status = 'revoked'` preserves audit history

### 2.2 Layer 2: Access Control (extends existing AgentPermission)

The existing `agent_permissions` table handles "who can see/use/manage an agent." We extend it with finer-grained actions rather than replacing it.

```sql
-- Layer 2: Access — what management operations are permitted
-- Extends existing agent_permissions with more granular action types
CREATE TABLE agent_access_grants (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id       UUID NOT NULL REFERENCES tenants(id),
    agent_id        UUID NOT NULL REFERENCES agents(id) ON DELETE CASCADE,
    principal_type  VARCHAR(20) NOT NULL CHECK (principal_type IN (
                        'user', 'department', 'role', 'company'
                    )),
    principal_id    UUID,  -- NULL for company-wide grants
    actions         TEXT[] NOT NULL CHECK (array_length(actions, 1) > 0),
    -- Supported actions:
    --   'view'               — see agent exists and its status
    --   'invoke'             — send tasks/messages to agent
    --   'manage_config'      — edit agent settings, soul, skills
    --   'manage_permissions' — modify access grants and bindings
    --   'manage_credentials' — bind/unbind external credentials
    --   'view_audit'         — read audit trail
    conditions      JSONB NOT NULL DEFAULT '{}',
    granted_by      UUID NOT NULL REFERENCES users(id),
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    expires_at      TIMESTAMPTZ,

    CONSTRAINT uq_access_grant UNIQUE (agent_id, principal_type, principal_id)
);

CREATE INDEX idx_access_grants_agent ON agent_access_grants(agent_id);
CREATE INDEX idx_access_grants_principal ON agent_access_grants(principal_type, principal_id);
```

**Migration from existing `agent_permissions`:**
- `access_level = 'use'` maps to `actions = ['view', 'invoke']`
- `access_level = 'manage'` maps to `actions = ['view', 'invoke', 'manage_config', 'manage_permissions', 'manage_credentials', 'view_audit']`
- `scope_type = 'company'` maps to `principal_type = 'company', principal_id = NULL`
- `scope_type = 'user'` maps to `principal_type = 'user', principal_id = scope_id`
- `scope_type = 'department'` maps to `principal_type = 'department', principal_id = scope_id`

### 2.3 Layer 3: Capability Grants

This is the core new abstraction. Replaces the flat `autonomy_policy` JSON with a relational, auditable capability system.

```sql
-- Layer 3: Capability — what runtime actions the agent is allowed to perform
CREATE TABLE capability_grants (
    id                          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id                   UUID NOT NULL REFERENCES tenants(id),
    agent_id                    UUID NOT NULL REFERENCES agents(id) ON DELETE CASCADE,
    capability_key              VARCHAR(100) NOT NULL,
    -- Hierarchical capability keys:
    --   workspace.file.read
    --   workspace.file.write
    --   workspace.file.delete
    --   channel.feishu.message.send
    --   channel.feishu.calendar.create
    --   channel.slack.message.send
    --   channel.discord.message.send
    --   channel.email.send
    --   external.api.call
    --   local.fs.read
    --   local.fs.write
    --   local.process.exec
    --   business.crm.read
    --   business.crm.write
    --   business.financial.read
    --   business.financial.write
    --   agent.soul.modify
    --   agent.memory.write
    scope                       JSONB NOT NULL DEFAULT '{}',
    -- Scope restricts WHERE the capability applies:
    --   {"paths": ["workspace/*", "skills/*"]}
    --   {"channels": ["feishu"]}
    --   {"groups": ["group-uuid-1"]}
    --   {"api_endpoints": ["https://api.example.com/*"]}
    approval_level              VARCHAR(5) NOT NULL DEFAULT 'L2' CHECK (approval_level IN (
                                    'L1',   -- Auto-execute, log only
                                    'L2',   -- Auto-execute, notify approver
                                    'L3'    -- Block until explicit approval
                                )),
    allowed_identity_types      TEXT[] NOT NULL DEFAULT '{agent_bot}',
    -- Which execution identities may be used for this capability:
    --   '{agent_bot}'
    --   '{delegated_user}'
    --   '{tenant_service_account}'
    --   '{agent_bot,delegated_user}'
    conditions                  JSONB NOT NULL DEFAULT '{}',
    -- ABAC conditions:
    --   {"time_range": {"start": "09:00", "end": "18:00"}}
    --   {"max_per_day": 100}
    --   {"require_mfa": true}
    --   {"environment": "production"}
    is_default                  BOOLEAN NOT NULL DEFAULT false,
    -- true = came from autonomy_policy migration or template; false = explicitly configured
    granted_by                  UUID NOT NULL REFERENCES users(id),
    created_at                  TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at                  TIMESTAMPTZ NOT NULL DEFAULT now(),
    expires_at                  TIMESTAMPTZ,

    CONSTRAINT uq_capability_grant UNIQUE (agent_id, capability_key)
);

CREATE INDEX idx_capability_agent ON capability_grants(agent_id);
CREATE INDEX idx_capability_key ON capability_grants(capability_key);
CREATE INDEX idx_capability_tenant ON capability_grants(tenant_id);

-- Capability usage counter (for rate limiting via conditions.max_per_day)
CREATE TABLE capability_usage (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    capability_id   UUID NOT NULL REFERENCES capability_grants(id) ON DELETE CASCADE,
    agent_id        UUID NOT NULL REFERENCES agents(id) ON DELETE CASCADE,
    period_key      VARCHAR(20) NOT NULL,  -- e.g., '2026-03-20' for daily
    usage_count     INTEGER NOT NULL DEFAULT 0,
    last_used_at    TIMESTAMPTZ NOT NULL DEFAULT now(),

    CONSTRAINT uq_capability_usage UNIQUE (capability_id, period_key)
);
```

### 2.4 Layer 4: Execution Identity and Credential Binding

```sql
-- Layer 4a: Credential bindings — external system credentials bound to agents/users/tenants
CREATE TABLE credential_bindings (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id       UUID NOT NULL REFERENCES tenants(id),
    provider        VARCHAR(50) NOT NULL CHECK (provider IN (
                        'feishu', 'slack', 'discord', 'dingtalk', 'wecom',
                        'microsoft_teams', 'google', 'github', 'email',
                        'custom_oauth', 'custom_api_key'
                    )),
    credential_type VARCHAR(30) NOT NULL CHECK (credential_type IN (
                        'bot_token',            -- Platform bot/app credentials
                        'oauth_user_token',     -- User-delegated OAuth token
                        'service_account',      -- Enterprise service account
                        'api_key',              -- Static API key
                        'webhook_secret'        -- Webhook verification
                    )),
    owner_type      VARCHAR(20) NOT NULL CHECK (owner_type IN (
                        'agent',    -- Credential belongs to a specific agent
                        'user',     -- Credential belongs to a user (delegation)
                        'tenant'    -- Credential belongs to the organization
                    )),
    owner_id        UUID NOT NULL,
    -- Secret storage: encrypted at rest, never returned in API responses
    -- In production, use a vault reference instead of direct storage
    credential_ref  VARCHAR(500) NOT NULL,
    -- What scopes/permissions this credential grants in the external system
    scope_set       TEXT[] NOT NULL DEFAULT '{}',
    -- e.g., for Feishu: '{im:message,calendar:event.create,contact:user.read}'
    -- e.g., for Slack: '{chat:write,channels:read}'
    display_name    VARCHAR(200),
    status          VARCHAR(20) NOT NULL DEFAULT 'active' CHECK (status IN (
                        'active', 'expired', 'revoked', 'rotation_pending'
                    )),
    issued_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
    expires_at      TIMESTAMPTZ,
    last_used_at    TIMESTAMPTZ,
    last_rotated_at TIMESTAMPTZ,
    rotation_policy JSONB NOT NULL DEFAULT '{}',
    -- e.g., {"auto_rotate_days": 90, "notify_before_days": 7}
    created_by      UUID NOT NULL REFERENCES users(id),
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    revoked_at      TIMESTAMPTZ,
    revoked_by      UUID REFERENCES users(id),

    CONSTRAINT uq_credential_binding UNIQUE (provider, credential_type, owner_type, owner_id)
);

CREATE INDEX idx_credential_provider ON credential_bindings(provider, status);
CREATE INDEX idx_credential_owner ON credential_bindings(owner_type, owner_id) WHERE status = 'active';
CREATE INDEX idx_credential_expiry ON credential_bindings(expires_at) WHERE status = 'active';
CREATE INDEX idx_credential_tenant ON credential_bindings(tenant_id);

-- Layer 4b: Execution records — immutable log of every external action with identity resolution
CREATE TABLE execution_records (
    id                      UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id               UUID NOT NULL REFERENCES tenants(id),
    trace_id                UUID NOT NULL,
    agent_id                UUID NOT NULL REFERENCES agents(id),
    capability_key          VARCHAR(100) NOT NULL,
    -- Who triggered this execution chain
    requested_by_user_id    UUID REFERENCES users(id),
    requested_by_trigger_id UUID REFERENCES triggers(id),
    -- Who approved (for L3 capabilities)
    approved_by_user_id     UUID REFERENCES users(id),
    approval_id             UUID REFERENCES approval_requests(id),
    -- Resolved execution identity
    execution_identity_type VARCHAR(30) NOT NULL CHECK (execution_identity_type IN (
                                'agent_bot', 'delegated_user', 'tenant_service_account'
                            )),
    execution_identity_ref  VARCHAR(200),
    -- e.g., "feishu:bot:cli_xxxxx" or "feishu:user:ou_xxxxx" or "slack:bot:xoxb-xxx"
    credential_id           UUID REFERENCES credential_bindings(id),
    -- External system details
    external_provider       VARCHAR(50),
    external_action         VARCHAR(200),
    external_request        JSONB,  -- Sanitized request (no secrets)
    external_response_code  INTEGER,
    external_response       JSONB,  -- Sanitized response
    -- Result
    status                  VARCHAR(20) NOT NULL CHECK (status IN (
                                'success', 'failed', 'denied', 'timeout', 'pending_approval'
                            )),
    error_message           TEXT,
    duration_ms             INTEGER,
    created_at              TIMESTAMPTZ NOT NULL DEFAULT now(),

    -- Append-only: no UPDATE/DELETE allowed (enforced by application layer)
    CONSTRAINT chk_execution_identity CHECK (
        execution_identity_type IN ('agent_bot', 'delegated_user', 'tenant_service_account')
    )
);

CREATE INDEX idx_execution_trace ON execution_records(trace_id);
CREATE INDEX idx_execution_agent ON execution_records(agent_id, created_at DESC);
CREATE INDEX idx_execution_tenant ON execution_records(tenant_id, created_at DESC);
CREATE INDEX idx_execution_identity ON execution_records(execution_identity_type, created_at DESC);
CREATE INDEX idx_execution_status ON execution_records(status) WHERE status != 'success';
```

---

## 3. Autonomy Policy L1/L2/L3 to Capability Migration

### 3.1 Mapping Table

The existing `autonomy_policy` JSON field on the Agent model maps to `capability_grants` rows:

| autonomy_policy key | capability_key | Default identity |
|---------------------|----------------|------------------|
| `read_files` | `workspace.file.read` | `agent_bot` |
| `write_workspace_files` | `workspace.file.write` | `agent_bot` |
| `delete_files` | `workspace.file.delete` | `agent_bot` |
| `send_feishu_message` | `channel.feishu.message.send` | `agent_bot` |
| `send_external_message` | `channel.*.message.send` | `agent_bot` |
| `modify_soul` | `agent.soul.modify` | `agent_bot` |
| `access_business_system_read` | `business.*.read` | `tenant_service_account` |
| `access_business_system_write` | `business.*.write` | `tenant_service_account` |
| `create_calendar_event` | `channel.feishu.calendar.create` | `delegated_user` |
| `financial_operations` | `business.financial.write` | `tenant_service_account` |

### 3.2 Migration Function (Python)

```python
async def migrate_autonomy_to_capabilities(
    db: AsyncSession, agent: Agent, migrated_by: uuid.UUID
) -> list[CapabilityGrant]:
    """Convert agent.autonomy_policy JSON into capability_grants rows.

    Called once per agent during Phase 1 migration.
    The original autonomy_policy field is preserved for backward compatibility.
    """
    POLICY_TO_CAPABILITY = {
        "read_files": ("workspace.file.read", ["agent_bot"]),
        "write_workspace_files": ("workspace.file.write", ["agent_bot"]),
        "delete_files": ("workspace.file.delete", ["agent_bot"]),
        "send_feishu_message": ("channel.feishu.message.send", ["agent_bot"]),
        "send_external_message": ("channel.external.message.send", ["agent_bot"]),
        "modify_soul": ("agent.soul.modify", ["agent_bot"]),
        "access_business_system_read": ("business.system.read", ["tenant_service_account"]),
        "access_business_system_write": ("business.system.write", ["tenant_service_account"]),
        "create_calendar_event": ("channel.feishu.calendar.create", ["delegated_user", "agent_bot"]),
        "financial_operations": ("business.financial.write", ["tenant_service_account"]),
    }

    grants = []
    policy = agent.autonomy_policy or {}

    for action_key, level in policy.items():
        if action_key not in POLICY_TO_CAPABILITY:
            continue
        cap_key, default_identities = POLICY_TO_CAPABILITY[action_key]
        grant = CapabilityGrant(
            tenant_id=agent.tenant_id,
            agent_id=agent.id,
            capability_key=cap_key,
            approval_level=level,  # L1/L2/L3 maps directly
            allowed_identity_types=default_identities,
            is_default=True,
            granted_by=migrated_by,
        )
        grants.append(grant)
        db.add(grant)

    await db.flush()
    return grants
```

### 3.3 Runtime Compatibility Layer

During migration, the capability check function falls back to `autonomy_policy` when no `capability_grants` rows exist:

```python
async def resolve_capability(
    db: AsyncSession, agent: Agent, tool_name: str
) -> CapabilityDecision:
    """Resolve capability for a tool call.

    Priority: capability_grants table > autonomy_policy JSON fallback
    """
    # 1. Try capability_grants table first
    grant = await db.execute(
        select(CapabilityGrant).where(
            CapabilityGrant.agent_id == agent.id,
            CapabilityGrant.capability_key == map_tool_to_capability(tool_name),
        )
    )
    cap = grant.scalar_one_or_none()

    if cap:
        return CapabilityDecision(
            allowed=True,
            approval_level=cap.approval_level,
            allowed_identities=cap.allowed_identity_types,
            source="capability_grants",
        )

    # 2. Fallback to autonomy_policy JSON
    policy = agent.autonomy_policy or {}
    level = policy.get(tool_name, "L2")
    return CapabilityDecision(
        allowed=True,
        approval_level=level,
        allowed_identities=["agent_bot"],
        source="autonomy_policy_fallback",
    )
```

---

## 4. Execution Identity Mapping Per Channel

### 4.1 Complete Channel-to-Identity Matrix

| Channel | Action Category | agent_bot | delegated_user | tenant_service_account |
|---------|----------------|-----------|----------------|----------------------|
| **Feishu/Lark** | Send group message | Bot App (app_id/app_secret) | user_access_token (OAuth) | tenant_access_token |
| | Send private message | Bot App | user_access_token | tenant_access_token |
| | Reply as user | -- | user_access_token (REQUIRED) | -- |
| | Create calendar event | Bot App (limited) | user_access_token (RECOMMENDED) | tenant_access_token |
| | Read contacts | Bot App | user_access_token | tenant_access_token |
| | Approve workflow | -- | user_access_token (REQUIRED) | -- |
| **Slack** | Post to channel | Bot Token (xoxb-) | User Token (xoxp-) | -- |
| | Send DM | Bot Token | User Token | -- |
| | React as user | -- | User Token (REQUIRED) | -- |
| | Manage channels | Bot Token (with scopes) | User Token | -- |
| **Discord** | Send channel message | Bot Token | -- (no delegation API) | -- |
| | Send DM | Bot Token | -- | -- |
| | Manage roles | Bot Token (with perms) | -- | -- |
| **DingTalk** | Send work notification | App credentials | -- | Corp credentials |
| | Send group message | App credentials | -- | Corp credentials |
| **WeChat Work** | Send app message | Corp App (secret) | -- | Corp-level token |
| | Send to user | Corp App | -- | Corp-level token |
| **MS Teams** | Send message | App registration | Delegated (OAuth 2.0) | Application permission |
| | Create meeting | App registration | Delegated (REQUIRED) | Application permission |
| **Email** | Send email | SMTP (agent mailbox) | SMTP (user mailbox) | SMTP (noreply@ org) |
| | Read inbox | IMAP (agent mailbox) | IMAP/OAuth (user) | -- |

### 4.2 Identity Resolution Algorithm

```
resolve_execution_identity(agent, capability, user_context):
    1. Load capability_grant for agent + capability_key
    2. Get allowed_identity_types from grant
    3. For each allowed type (in preference order):
       a. agent_bot:
          - Find credential_binding WHERE owner_type='agent' AND owner_id=agent_id
            AND provider=channel AND credential_type='bot_token' AND status='active'
          - Return if found
       b. delegated_user:
          - Require user_context.user_id is a delegatee of this agent (agent_bindings)
          - Find credential_binding WHERE owner_type='user' AND owner_id=user_id
            AND provider=channel AND credential_type='oauth_user_token' AND status='active'
          - Verify token not expired, scopes sufficient
          - Return if found
       c. tenant_service_account:
          - Find credential_binding WHERE owner_type='tenant' AND owner_id=tenant_id
            AND provider=channel AND credential_type='service_account' AND status='active'
          - Return if found
    4. If no identity resolved: DENY execution, log as 'no_valid_identity'
```

### 4.3 Default Identity Rules

| Action Pattern | Required Identity | Rationale |
|---------------|-------------------|-----------|
| `*.message.send` (to group) | `agent_bot` preferred | Bot identity is transparent |
| `*.message.reply_as_user` | `delegated_user` REQUIRED | Impersonation must be explicit |
| `*.calendar.create` | `delegated_user` RECOMMENDED | Calendar events are personal |
| `*.approval.*` | `delegated_user` REQUIRED | Cannot approve as bot |
| `*.notification.send` | `agent_bot` or `tenant_service_account` | System notifications |
| `business.*.read` | `tenant_service_account` preferred | Enterprise data access |
| `business.*.write` | `tenant_service_account` REQUIRED | Must audit under org identity |
| `workspace.*` | `agent_bot` only | Internal workspace, no external identity needed |

---

## 5. Audit Chain Complete Flow

### 5.1 End-to-End Audit Flow Diagram

```
User/Trigger Action
    |
    v
[1] Access Check (agent_access_grants)
    |  -> SecurityAuditEvent: event_type=access_check
    v
[2] Binding Verification (agent_bindings)
    |  -> SecurityAuditEvent: event_type=binding_check
    v
[3] Capability Resolution (capability_grants)
    |  -> SecurityAuditEvent: event_type=capability_check
    |
    +-- L1: Auto-execute ------+
    |                          |
    +-- L2: Execute + Notify --+
    |                          |
    +-- L3: Block for Approval |
    |       |                  |
    |       v                  |
    |   [3a] ApprovalRequest   |
    |       |                  |
    |       v                  |
    |   [3b] User Approves     |
    |       |                  |
    |       +------------------+
    |                          |
    v                          v
[4] Identity Resolution (credential_bindings)
    |  -> SecurityAuditEvent: event_type=identity_resolved
    |  -> Records: identity_type, credential_id, provider
    v
[5] Execution (external system call)
    |  -> execution_records: full request/response trace
    v
[6] Result Logging
    |  -> SecurityAuditEvent: event_type=execution_complete
    |  -> AuditLog: backward-compatible entry
    v
[Done]

Every step writes to the hash-chained SecurityAuditEvent table.
execution_records provides the full forensic trace for any external action.
```

### 5.2 Audit Record Structure Per Step

```python
# Step 1: Access check
await write_audit_event(db,
    event_type="access_check",
    severity="info",
    actor_type="user",
    actor_id=user.id,
    tenant_id=tenant_id,
    action="invoke",
    resource_type="agent",
    resource_id=agent.id,
    details={"result": "granted", "actions": ["invoke"]},
)

# Step 3: Capability check
await write_audit_event(db,
    event_type="capability_check",
    severity="info",
    actor_type="agent",
    actor_id=agent.id,
    tenant_id=tenant_id,
    action=capability_key,
    details={
        "approval_level": "L2",
        "source": "capability_grants",
        "decision": "auto_execute_notify",
    },
)

# Step 4: Identity resolution
await write_audit_event(db,
    event_type="identity_resolved",
    severity="info",
    actor_type="agent",
    actor_id=agent.id,
    tenant_id=tenant_id,
    action=capability_key,
    details={
        "identity_type": "agent_bot",
        "credential_id": str(credential.id),
        "provider": "feishu",
        "delegated_from": str(user.id) if delegated else None,
    },
)

# Step 5+6: Execution record (separate table, immutable)
execution_record = ExecutionRecord(
    tenant_id=tenant_id,
    trace_id=trace_id,
    agent_id=agent.id,
    capability_key=capability_key,
    requested_by_user_id=user.id,
    execution_identity_type="agent_bot",
    execution_identity_ref="feishu:bot:cli_xxxxx",
    credential_id=credential.id,
    external_provider="feishu",
    external_action="im.message.create",
    status="success",
    duration_ms=142,
)
```

### 5.3 Hash Chain Integrity

The existing `SecurityAuditEvent` model already implements hash chaining (`prev_hash` -> `event_hash`). This design reuses that mechanism. For execution records, integrity is enforced by:

1. Application-level INSERT-only policy (no UPDATE/DELETE on `execution_records`)
2. Periodic integrity verification job that validates the hash chain in `security_audit_events`
3. Optional: database-level trigger to prevent UPDATE/DELETE on `execution_records`

```sql
-- Optional: enforce append-only on execution_records
CREATE OR REPLACE FUNCTION prevent_execution_record_mutation()
RETURNS TRIGGER AS $$
BEGIN
    RAISE EXCEPTION 'execution_records is append-only: UPDATE and DELETE are prohibited';
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER trg_execution_records_immutable
    BEFORE UPDATE OR DELETE ON execution_records
    FOR EACH ROW
    EXECUTE FUNCTION prevent_execution_record_mutation();
```

---

## 6. Credential Lifecycle Management

### 6.1 State Machine

```
                   issue/create
                       |
                       v
    +----------> [active] <----------+
    |              |    |            |
    |    rotation   |    |  expire    |  re-activate
    |    requested  |    |            |  (re-auth)
    |              v    v            |
    |  [rotation_pending] [expired] -+
    |        |
    |        | rotation complete
    |        v
    +---[active] (new credential_ref)

    Any state --> [revoked] (terminal, irreversible)
```

### 6.2 Lifecycle Operations

| Operation | Trigger | Action |
|-----------|---------|--------|
| **Issue** | User configures channel / OAuth flow completes | Insert `credential_bindings` row with `status=active` |
| **Use** | Agent executes capability requiring this credential | Update `last_used_at`, check `expires_at` |
| **Rotate** | Cron job detects `rotation_policy.auto_rotate_days` threshold | Set `status=rotation_pending`, issue new credential, swap `credential_ref`, set `status=active`, update `last_rotated_at` |
| **Expire** | `expires_at` passes or OAuth token expires | Set `status=expired`, notify owner, agent falls back to alternative identity |
| **Revoke** | Admin action, security incident, user unbind | Set `status=revoked`, `revoked_at`, `revoked_by`. All capabilities using this credential fail closed |
| **Re-issue** | User re-authenticates OAuth flow | New `credential_bindings` row, old remains `expired`/`revoked` for audit |

### 6.3 Rotation Policy

```json
{
    "auto_rotate_days": 90,
    "notify_before_days": 7,
    "max_lifetime_days": 365,
    "require_manual_approval": false
}
```

### 6.4 Security Constraints

1. **Encryption**: `credential_ref` is encrypted at rest using AES-256-GCM. The encryption key is derived from `SECRET_KEY` environment variable via HKDF.
2. **Never in API responses**: Credential values are never returned in any API response. Only `display_name`, `provider`, `status`, `scope_set`, and timestamps are visible.
3. **Scope minimization**: When binding credentials, the system validates that requested `scope_set` is a subset of what the credential actually grants.
4. **Automatic expiry notification**: 7 days before expiry, send notification to all `owner` and `operator` bindings of the agent.
5. **Revocation cascade**: When a credential is revoked, all `capability_grants` that depend on it are suspended (but not deleted).

---

## 7. Gradual Migration Path

### Phase 0: Schema Addition (Non-Breaking)

**Goal**: Add new tables alongside existing ones. Zero behavioral change.

**Steps**:
1. Create Alembic migration adding all 5 new tables (`agent_bindings`, `agent_access_grants`, `capability_grants`, `capability_usage`, `credential_bindings`, `execution_records`)
2. Add append-only trigger on `execution_records`
3. No code changes to existing flows

**Verification**: `alembic upgrade head` succeeds; existing tests pass unchanged.

**Duration estimate**: 1 sprint

### Phase 1: Data Backfill + Dual-Write

**Goal**: Populate new tables from existing data. Begin writing to both old and new systems.

**Steps**:
1. Write migration script that for each agent:
   - Creates `agent_bindings` row: `creator_id` -> `binding_role='owner'`
   - Converts `agent_permissions` rows -> `agent_access_grants` rows
   - Converts `autonomy_policy` JSON -> `capability_grants` rows (with `is_default=true`)
   - Converts `channel_configs` credentials -> `credential_bindings` rows
2. Modify `AutonomyService.check_and_enforce` to dual-write:
   - Still reads from `autonomy_policy` (source of truth)
   - Also writes result to `execution_records`
3. Modify `check_agent_access` to dual-read:
   - Primary: existing logic
   - Shadow: new `agent_bindings` + `agent_access_grants` (log discrepancies, don't enforce)

**Verification**: Shadow-mode discrepancy rate = 0% for 1 week.

**Duration estimate**: 2 sprints

### Phase 2: Switch Primary Read Path

**Goal**: New tables become source of truth for reads. Old fields become write-through cache.

**Steps**:
1. `resolve_capability()` reads from `capability_grants` first, falls back to `autonomy_policy`
2. `check_agent_access()` reads from `agent_bindings` + `agent_access_grants` first, falls back to `agent_permissions`
3. All credential lookups go through `credential_bindings` instead of `channel_configs` direct field access
4. UI: Agent settings page shows capability grants instead of raw L1/L2/L3 JSON
5. Keep writing to old fields for rollback safety

**Verification**: All integration tests pass with new read path. Feature flag to revert to old path.

**Duration estimate**: 2 sprints

### Phase 3: Deprecate Old Fields

**Goal**: Remove old code paths. Old columns remain for data safety but are no longer read.

**Steps**:
1. Remove `autonomy_policy` reads (field still exists in DB for rollback)
2. Remove `agent_permissions` reads (table still exists)
3. Remove `channel_configs.app_secret` / `app_id` direct reads (use `credential_bindings`)
4. `AutonomyService` refactored to `CapabilityService` using new tables exclusively
5. Add deprecation warnings in API responses for old field usage

**Verification**: `ruff` shows no imports of deprecated paths. All tests rewritten against new models.

**Duration estimate**: 1 sprint

### Phase 4: Cleanup (Optional, After Confidence Period)

**Goal**: Remove deprecated columns and tables after 2+ release cycles.

**Steps**:
1. Drop `agent.autonomy_policy` column
2. Drop `agent_permissions` table
3. Remove `channel_configs.app_id`, `app_secret`, `encrypt_key`, `verification_token` columns (data lives in `credential_bindings`)
4. Archive old `audit_logs` entries to cold storage

**Verification**: Clean schema, all tests green, production stable for 30+ days.

---

## 8. Key Architectural Decisions

### 8.1 Why Not Extend `resource_permissions`?

The existing `resource_permissions` table (used by `policy.py`) handles RBAC/ABAC for *management* operations. Capability grants are fundamentally different: they control what the *agent itself* can do at runtime, not what humans can do to the agent. Mixing these concerns in one table would create confusing query patterns and make it harder to reason about the permission model.

### 8.2 Why Separate `execution_records` from `security_audit_events`?

`security_audit_events` is a general-purpose audit log for all platform events. `execution_records` is specifically designed for external action forensics with structured fields for identity resolution, external system details, and request/response capture. Keeping them separate allows:
- Different retention policies (audit events: 1 year; execution records: 3 years for compliance)
- Different query patterns (audit: "what happened to agent X?" vs execution: "what did agent X do on Feishu between date A and B?")
- Different access controls (audit: view_audit permission; execution: compliance team access)

### 8.3 Why `capability_key` is Hierarchical

Using dotted hierarchical keys (e.g., `channel.feishu.message.send`) enables wildcard grants:
- `channel.feishu.*` grants all Feishu capabilities
- `workspace.*` grants all workspace operations
- `business.*` grants all business system access

This is resolved at query time with prefix matching, allowing both fine-grained and coarse-grained permission assignment.

---

## 9. Risk Assessment

| Risk | Severity | Mitigation |
|------|----------|------------|
| Migration corrupts existing permissions | High | Dual-write + shadow-read phase; feature flag rollback |
| Credential encryption key loss | Critical | Key derived from existing `SECRET_KEY`; backup via `CREDENTIAL_ENCRYPTION_KEY` env var |
| Performance impact of per-action capability check | Medium | Cache `capability_grants` per agent (invalidate on update); 95th percentile must be <5ms |
| Complexity overwhelming for single-agent users | Medium | `is_default=true` grants auto-created from templates; simple UI hides complexity |
| Hash chain breaks during concurrent writes | Low | Existing `SecurityAuditEvent` already handles this; `sequence_num` provides ordering |
