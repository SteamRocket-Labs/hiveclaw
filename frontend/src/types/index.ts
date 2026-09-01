/** Shared TypeScript types */

export interface User {
    id: string;
    username: string;
    email: string;
    display_name: string;
    avatar_url?: string;
    role: 'platform_admin' | 'org_admin' | 'member';
    tenant_id?: string;
    department_id?: string;
    title?: string;
    feishu_user_id?: string;
    feishu_open_id?: string;
    is_active: boolean;
    created_at: string;
}

export interface Agent {
    id: string;
    name: string;
    avatar_url?: string;
    role_description: string;
    bio?: string;
    status: 'creating' | 'running' | 'idle' | 'stopped' | 'error';
    creator_id: string;
    owner_user_id?: string;
    access_level?: 'use' | 'manage' | 'operator';
    is_owner?: boolean;
    action_capabilities?: {
        can_use: boolean;
        can_manage: boolean;
        can_manage_schedule: boolean;
        can_manage_channel: boolean;
        can_manage_permissions: boolean;
        can_operator_inspect?: boolean;
        can_transfer_ownership: boolean;
    };
    primary_model_id?: string;
    fallback_model_id?: string;
    tokens_used_today: number;
    tokens_used_month: number;
    max_tokens_per_day?: number;
    max_tokens_per_month?: number;
    heartbeat_enabled: boolean;
    heartbeat_interval_minutes: number;
    heartbeat_active_hours: string;
    last_heartbeat_at?: string;
    timezone?: string;
    execution_mode?: 'standard' | 'coordinator' | 'coordinator_strict';
    default_session_permission_mode?: 'default' | 'auto' | 'bypassPermissions';
    smart_model_routing?: { enabled: boolean; max_simple_chars: number; max_simple_words: number } | null;
    context_window_size?: number;
    agent_type?: 'native' | 'local_agent';
    created_at: string;
    last_active_at?: string;
}

export interface Task {
    id: string;
    agent_id: string;
    title: string;
    description?: string;
    type: 'todo';
    status: 'pending' | 'doing' | 'done' | 'blocked' | 'failed' | 'cancelled' | 'needs_reconciliation';
    priority: 'low' | 'medium' | 'high' | 'urgent';
    assignee: string;
    created_by: string;
    request_id: string;
    request_hash: string;
    active_runtime_task_id?: string;
    execution_attempt: number;
    last_execution_status?: string;
    last_error?: string;
    last_result?: string;
    creator_username?: string;
    due_date?: string;
    remind_schedule?: string;
    created_at: string;
    updated_at: string;
    completed_at?: string;
    runtime_status?: string;
    runtime_phase?: string;
    runtime_summary?: string;
    runtime_request_id?: string;
    reflection_session_id?: string;
    recovery_state: 'none' | 'retry_available' | 'needs_review' | 'complete' | 'cancelled' | 'runtime_evidence_missing' | string;
    recovery_message?: string;
    actions: {
        can_cancel: boolean;
        can_retry: boolean;
        can_reconcile: boolean;
    };
    dependencies: Array<{
        id: string;
        label: string;
        status: 'satisfied' | 'missing' | string;
    }>;
    stages: Array<{
        id: string;
        label: string;
        status: 'pending' | 'current' | 'complete' | 'failed' | 'blocked' | 'cancelled' | 'warning' | string;
    }>;
}

export interface ChatMessage {
    id: string;
    agent_id: string;
    user_id: string;
    role: 'user' | 'assistant' | 'system';
    content: string;
    created_at: string;
}

export interface TokenResponse {
    access_token: string;
    token_type: string;
    user: User;
    needs_company_setup?: boolean;
    /** True for SSO-imported users who still have the shared default password (123456). */
    needs_password_change?: boolean;
}
