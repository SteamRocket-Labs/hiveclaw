"""Pydantic schemas for request/response validation."""

import uuid
from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, EmailStr, Field, model_validator

from app.services.llm_client import ABSOLUTE_MAX_OUTPUT_TOKENS

AGENT_ROLE_DESCRIPTION_MAX_CHARS = 4000


# ─── Auth ───────────────────────────────────────────────


class UserRegister(BaseModel):
    username: str = Field(min_length=3, max_length=100)
    email: EmailStr
    password: str = Field(min_length=6, max_length=128)
    display_name: str | None = None
    invitation_code: str | None = None


class UserLogin(BaseModel):
    username: str
    password: str


class TokenResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"
    user: "UserOut"
    needs_company_setup: bool = False
    # True when the user logged in with the shared default password from an SSO
    # import and hasn't rotated it yet. UI prompts them to change it.
    needs_password_change: bool = False


class UserOut(BaseModel):
    id: uuid.UUID
    username: str
    email: str
    display_name: str
    avatar_url: str | None = None
    role: str
    tenant_id: uuid.UUID | None = None
    department_id: uuid.UUID | None = None
    title: str | None = None
    feishu_open_id: str | None = None
    oidc_sub: str | None = None
    is_active: bool
    quota_tokens_per_day: int | None = None
    quota_tokens_per_month: int | None = None
    tokens_used_today: int = 0
    tokens_used_month: int = 0
    tokens_used_total: int = 0
    created_at: datetime

    model_config = {"from_attributes": True}


class UserUpdate(BaseModel):
    username: str | None = None
    display_name: str | None = None
    avatar_url: str | None = None
    title: str | None = None
    department_id: uuid.UUID | None = None


# ─── Agent ──────────────────────────────────────────────

AgentClass = Literal["internal_system", "internal_tenant", "external_gateway", "external_api"]
AgentExecutionMode = Literal["standard", "coordinator"]


class SmartModelRoutingConfig(BaseModel):
    enabled: bool = False
    max_simple_chars: int = Field(default=160, ge=32, le=500)
    max_simple_words: int = Field(default=28, ge=4, le=80)


class AgentCreate(BaseModel):
    name: str = Field(min_length=2, max_length=100, description="Agent name, 2-100 characters")
    role_description: str = Field(
        default="",
        max_length=AGENT_ROLE_DESCRIPTION_MAX_CHARS,
        description="Role description, max 4000 characters",
    )
    bio: str | None = None
    welcome_message: str | None = None
    avatar_url: str | None = None
    # Soul
    personality: str = ""
    boundaries: str = ""
    # Model
    primary_model_id: uuid.UUID | None = None
    fallback_model_id: uuid.UUID | None = None
    # Permissions
    permission_scope_type: str = "company"  # company | user
    permission_scope_ids: list[uuid.UUID] = []
    permission_access_level: str = "use"  # use | manage
    # Target tenant (admin-only override; otherwise ignored)
    tenant_id: uuid.UUID | None = None
    # Classification
    agent_class: AgentClass = "internal_tenant"
    security_zone: str = "standard"  # public | standard | restricted
    execution_mode: AgentExecutionMode = "standard"
    smart_model_routing: SmartModelRoutingConfig | None = None
    # Skills to copy into agent workspace
    skill_ids: list[uuid.UUID] = []


class AgentOut(BaseModel):
    id: uuid.UUID
    name: str
    avatar_url: str | None = None
    role_description: str
    bio: str | None = None
    welcome_message: str | None = None
    status: str
    creator_id: uuid.UUID
    sponsor_user_id: uuid.UUID
    participant_id: uuid.UUID
    owner_user_id: uuid.UUID | None = None
    tenant_id: uuid.UUID | None = None
    creator_username: str | None = None  # Populated by API layer; not in ORM model directly
    primary_model_id: uuid.UUID | None = None
    fallback_model_id: uuid.UUID | None = None
    tokens_used_today: int
    tokens_used_month: int
    tokens_used_total: int = 0
    max_tool_rounds: int = 200
    max_triggers: int = 20
    min_poll_interval_min: int = 5
    webhook_rate_limit: int = 5
    heartbeat_enabled: bool = True
    heartbeat_interval_minutes: int = 45
    heartbeat_active_hours: str = "00:00-23:59"
    last_heartbeat_at: datetime | None = None
    timezone: str | None = None
    agent_type: str = "native"
    agent_class: AgentClass = "internal_tenant"
    security_zone: str = "standard"
    execution_mode: AgentExecutionMode = "standard"
    smart_model_routing: SmartModelRoutingConfig | None = None
    created_at: datetime
    last_active_at: datetime | None = None
    deleted_at: datetime | None = None
    deactivated_at: datetime | None = None
    deactivation_reason: str | None = None

    model_config = {"from_attributes": True}


class AgentUpdate(BaseModel):
    name: str | None = None
    role_description: str | None = Field(default=None, max_length=AGENT_ROLE_DESCRIPTION_MAX_CHARS)
    bio: str | None = None
    welcome_message: str | None = None
    avatar_url: str | None = None
    primary_model_id: uuid.UUID | None = None
    fallback_model_id: uuid.UUID | None = None
    max_tool_rounds: int | None = None
    max_triggers: int | None = None
    min_poll_interval_min: int | None = None
    webhook_rate_limit: int | None = None
    timezone: str | None = None
    agent_class: AgentClass | None = None
    security_zone: str | None = None
    execution_mode: AgentExecutionMode | None = None
    smart_model_routing: SmartModelRoutingConfig | None = None


class AgentStatusOut(BaseModel):
    """Legacy agent status payload shape. The state.json file path is retired."""

    agent_id: uuid.UUID
    name: str
    status: str
    current_task: str | None = None
    last_active: datetime | None = None
    channel_status: dict = {}
    stats: dict = {}


# ─── Task ───────────────────────────────────────────────


class TaskCreate(BaseModel):
    title: str = Field(min_length=1, max_length=500)
    description: str | None = None
    type: str = "todo"  # todo
    priority: str = "medium"
    due_date: datetime | None = None
    # Plan Mode (§9.3): a confirmed plan authorising an auto-executing todo task.
    confirmed_plan_id: str | None = None
    confirmed_plan_version: int | None = None
    confirmed_plan_hash: str | None = None


class TaskOut(BaseModel):
    id: uuid.UUID
    agent_id: uuid.UUID
    title: str
    description: str | None = None
    type: str
    status: str
    priority: str
    assignee: str
    created_by: uuid.UUID
    creator_username: str | None = None
    due_date: datetime | None = None
    plan_id: uuid.UUID | None = None
    plan_version: int | None = None
    plan_hash: str | None = None
    plan_exempt_reason: str | None = None
    created_at: datetime
    updated_at: datetime
    completed_at: datetime | None = None

    model_config = {"from_attributes": True}


class TaskUpdate(BaseModel):
    title: str | None = None
    description: str | None = None
    status: str | None = None
    priority: str | None = None
    due_date: datetime | None = None


class TaskLogCreate(BaseModel):
    content: str


class TaskLogOut(BaseModel):
    id: uuid.UUID
    task_id: uuid.UUID
    content: str
    created_at: datetime

    model_config = {"from_attributes": True}


# ─── Department ─────────────────────────────────────────


class DepartmentCreate(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    parent_id: uuid.UUID | None = None
    manager_id: uuid.UUID | None = None


class DepartmentOut(BaseModel):
    id: uuid.UUID
    name: str
    parent_id: uuid.UUID | None = None
    manager_id: uuid.UUID | None = None
    sort_order: int
    created_at: datetime

    model_config = {"from_attributes": True}


class DepartmentTree(DepartmentOut):
    children: list["DepartmentTree"] = []
    member_count: int = 0


# ─── LLM ────────────────────────────────────────────────


class LLMModelCreate(BaseModel):
    provider: str
    model: str
    api_key: str
    base_url: str | None = None
    label: str
    max_tokens_per_day: int | None = None
    enabled: bool = True
    supports_vision: bool = False
    max_output_tokens: int | None = Field(default=None, ge=1, le=ABSOLUTE_MAX_OUTPUT_TOKENS)
    max_input_tokens: int | None = None
    temperature: float | None = Field(default=None, ge=0, le=2)
    reasoning_mode: str | None = None
    reasoning_effort: str | None = None
    reasoning_budget_tokens: int | None = Field(default=None, ge=1, le=200000)
    reasoning_display: str | None = None
    preserve_reasoning: bool | None = None
    text_verbosity: str | None = None
    provider_options: dict | None = None

    @model_validator(mode="after")
    def validate_model_identifier(self) -> "LLMModelCreate":
        from app.services.llm_client import get_llm_model_identifier_error

        error = get_llm_model_identifier_error(self.provider, self.model)
        if error:
            raise ValueError(error)
        return self


class LLMModelUpdate(BaseModel):
    provider: str | None = None
    model: str | None = None
    api_key: str | None = None
    base_url: str | None = None
    label: str | None = None
    max_tokens_per_day: int | None = None
    enabled: bool | None = None
    supports_vision: bool | None = None
    max_output_tokens: int | None = Field(default=None, ge=1, le=ABSOLUTE_MAX_OUTPUT_TOKENS)
    max_input_tokens: int | None = None
    temperature: float | None = Field(default=None, ge=0, le=2)
    reasoning_mode: str | None = None
    reasoning_effort: str | None = None
    reasoning_budget_tokens: int | None = Field(default=None, ge=1, le=200000)
    reasoning_display: str | None = None
    preserve_reasoning: bool | None = None
    text_verbosity: str | None = None
    provider_options: dict | None = None

    @model_validator(mode="after")
    def validate_model_identifier(self) -> "LLMModelUpdate":
        if not self.provider or not self.model:
            return self
        from app.services.llm_client import get_llm_model_identifier_error

        error = get_llm_model_identifier_error(self.provider, self.model)
        if error:
            raise ValueError(error)
        return self


class LLMModelOut(BaseModel):
    id: uuid.UUID
    provider: str
    model: str
    base_url: str | None = None
    label: str
    api_key_masked: str = ""
    max_tokens_per_day: int | None = None
    enabled: bool
    supports_vision: bool = False
    max_output_tokens: int | None = None
    max_input_tokens: int | None = None
    temperature: float | None = None
    reasoning_mode: str | None = None
    reasoning_effort: str | None = None
    reasoning_budget_tokens: int | None = None
    reasoning_display: str | None = None
    preserve_reasoning: bool | None = None
    text_verbosity: str | None = None
    provider_options: dict | None = None
    created_at: datetime
    is_default: bool = False

    model_config = {"from_attributes": True}


# ─── Channel Config ─────────────────────────────────────


class ChannelConfigCreate(BaseModel):
    channel_type: str = "feishu"
    app_id: str
    app_secret: str | None = None
    encrypt_key: str | None = None
    verification_token: str | None = None
    extra_config: dict | None = None


def _mask_secret(value: str | None) -> str | None:
    """Mask a secret value, showing only last 4 characters."""
    if not value:
        return None
    return f"****{value[-4:]}" if len(value) > 4 else "****"


class ChannelConfigOut(BaseModel):
    id: uuid.UUID
    agent_id: uuid.UUID
    channel_type: str
    app_id: str | None = None
    app_secret: str | None = None
    encrypt_key: str | None = None
    verification_token: str | None = None
    is_configured: bool
    is_connected: bool
    last_tested_at: datetime | None = None
    extra_config: dict | None = None
    created_at: datetime

    model_config = {"from_attributes": True}

    def to_safe(self) -> "ChannelConfigOut":
        """Return a copy with secrets masked for non-admin users."""
        data = self.model_dump()
        data["app_secret"] = _mask_secret(self.app_secret)
        data["encrypt_key"] = _mask_secret(self.encrypt_key)
        data["verification_token"] = _mask_secret(self.verification_token)
        if data.get("extra_config"):
            safe_extra = dict(data["extra_config"])
            for key in (
                "app_secret",
                "bot_token",
                "signing_secret",
                "client_secret",
                "api_key",
                "bot_secret",
                "ilink_bot_token",
            ):
                if key in safe_extra:
                    safe_extra[key] = _mask_secret(safe_extra[key])
            data["extra_config"] = safe_extra
        return ChannelConfigOut(**data)


# ─── Approval ───────────────────────────────────────────


class ApprovalRequestOut(BaseModel):
    id: uuid.UUID
    agent_id: uuid.UUID
    agent_name: str | None = None
    action_type: str
    details: dict
    status: str
    created_at: datetime
    resolved_at: datetime | None = None
    resolved_by: uuid.UUID | None = None
    decision_id: str | None = None
    tool_name: str | None = None
    normalized_arguments: dict | None = None
    input_hash: str | None = None
    policy_snapshot_hash: str | None = None
    expires_at: datetime | None = None
    consumed_at: datetime | None = None
    execution_status: str = "pending"
    execution_receipt: dict | None = None

    model_config = {"from_attributes": True}


class ApprovalAction(BaseModel):
    action: str  # "approve" | "reject"


# ─── Enterprise Info ────────────────────────────────────


class EnterpriseInfoUpdate(BaseModel):
    content: dict
    visible_roles: list[str] = []


class EnterpriseInfoOut(BaseModel):
    id: uuid.UUID
    info_type: str
    content: dict
    version: int
    visible_roles: list
    updated_at: datetime

    model_config = {"from_attributes": True}


# ─── Chat ───────────────────────────────────────────────


class ChatMessageOut(BaseModel):
    id: uuid.UUID
    agent_id: uuid.UUID
    user_id: uuid.UUID
    role: str
    content: str
    thinking: str | None = None
    created_at: datetime

    model_config = {"from_attributes": True}


class ChatSend(BaseModel):
    content: str = Field(min_length=1)


# ─── Audit Log ──────────────────────────────────────────


class AuditLogOut(BaseModel):
    id: uuid.UUID
    user_id: uuid.UUID | None = None
    agent_id: uuid.UUID | None = None
    action: str
    details: dict
    ip_address: str | None = None
    created_at: datetime

    model_config = {"from_attributes": True}


# ─── Generic ────────────────────────────────────────────


class PaginatedResponse(BaseModel):
    items: list
    total: int
    page: int = 1
    page_size: int = 20


class HealthResponse(BaseModel):
    status: str = "ok"
    version: str
    components: dict[str, Any] = Field(default_factory=dict)
