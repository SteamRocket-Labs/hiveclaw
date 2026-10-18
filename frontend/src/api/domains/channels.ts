import { ApiError, get, post, put, del } from '../core';

export type FeishuPlatformRegion = 'feishu_cn' | 'lark_global';

export type FeishuAppRegistrationStatus =
  | 'initializing'
  | 'scanning'
  | 'polling'
  | 'slow_down'
  | 'domain_switched'
  | 'credentials_received'
  | 'connecting'
  | 'connected'
  | 'denied'
  | 'expired'
  | 'cancelled'
  | 'failed'
  | 'interrupted';

export interface FeishuAppRegistration {
  session_id: string;
  status: FeishuAppRegistrationStatus;
  platform_region: FeishuPlatformRegion;
  resolved_platform_region?: FeishuPlatformRegion | null;
  verification_url?: string | null;
  qr_expires_at?: string | null;
  connection_status?: string | null;
  message?: string | null;
  error_code?: string | null;
  connected: boolean;
  cancellable: boolean;
  created_at: string;
  updated_at: string;
}

export interface AgentChannelConfig {
  id?: string;
  agent_id?: string;
  channel_type?: string;
  app_id?: string | null;
  app_secret?: string | null;
  encrypt_key?: string | null;
  verification_token?: string | null;
  is_configured?: boolean;
  is_connected?: boolean;
  extra_config?: {
    connection_mode?: string;
    connection_status?: string;
    platform_region?: string;
    registration_session_id?: string;
    [key: string]: unknown;
  } | null;
}

export interface FeishuSharedGroupEntry {
  installation_ref: string;
  revision: string;
  chat_ids: string[];
}

export interface TenantFeishuGroupRoute { chat_id: string; agent_id: string }
export interface TenantFeishuGroupRoutes { tenant_id: string; installation_ref: string; revision: string; routes: TenantFeishuGroupRoute[] }

export interface TenantChannelScope { expected_tenant_id: string; expected_user_id: string }
export interface TenantChannelConfig {
  id: string; tenant_id: string; channel_type: string; app_id: string | null; is_active: boolean;
  revision: string; has_app_secret: boolean; has_encrypt_key: boolean; has_verification_token: boolean;
  extra_config: Record<string, unknown>;
}
export interface TenantChannelUpdate extends TenantChannelScope {
  app_id: string; expected_revision: string; is_active: boolean; platform_region?: FeishuPlatformRegion;
  app_secret?: string; encrypt_key?: string; verification_token?: string;
}
export type TenantFeishuIdentityState = 'company_bot_unconfigured' | 'company_bot_paused' | 'company_bot_setup_required'
  | 'identity_provider_required' | 'identity_app_mismatch' | 'identity_required' | 'identity_conflict'
  | 'installation_revoked' | 'awaiting_first_message' | 'reconnect_required' | 'bound' | 'assistant_required' | 'multiple_identities';
export interface TenantFeishuIdentityEntry { subject_id: string; principal_id: string | null; state: TenantFeishuIdentityState }
export interface TenantFeishuIdentity {
  user_id: string; tenant_id: string; state: TenantFeishuIdentityState; principal_id: string | null;
  assistant_required?: boolean; entries: TenantFeishuIdentityEntry[];
  disconnectable?: Array<{ principal_id: string; subject_id: string }>;
}

export interface WeChatPersonalStatus {
  connected: boolean;
  account_id?: string;
  transport_connected: boolean;
  identity_status: 'disconnected' | 'verified' | 'rebind_required' | 'access_denied';
  requires_rebind: boolean;
  requires_access_recovery: boolean;
}

export const channelApi = {
  tenantFeishuGroupRoutes: (scope: TenantChannelScope, signal?: AbortSignal) =>
    get<TenantFeishuGroupRoutes>(`/tenant-channels/feishu/shared-groups?${new URLSearchParams({ ...scope })}`, { signal }),
  saveTenantFeishuGroupRoutes: (scope: TenantChannelScope, revision: string, routes: TenantFeishuGroupRoute[]) =>
    put<TenantFeishuGroupRoutes>('/tenant-channels/feishu/shared-groups', { ...scope, expected_revision: revision, routes }),
  tenantChannels: (scope: TenantChannelScope, signal?: AbortSignal) =>
    get<TenantChannelConfig[]>(`/tenant-channels?${new URLSearchParams({ ...scope })}`, { signal }),
  saveTenantFeishu: (data: TenantChannelUpdate) => put<TenantChannelConfig>('/tenant-channels/feishu', data),
  tenantFeishuWebhook: (scope: TenantChannelScope, signal?: AbortSignal) =>
    get<{ webhook_url: string; tenant_id: string }>(`/tenant-channels/feishu/webhook-url?${new URLSearchParams({ ...scope })}`, { signal }),
  tenantFeishuIdentity: (scope: TenantChannelScope, signal?: AbortSignal) =>
    get<TenantFeishuIdentity>(`/tenant-channels/feishu/identity?${new URLSearchParams({ ...scope })}`, { signal }),
  tenantFeishuIdentityAction: (action: 'disconnect' | 'reconnect', principalId: string, scope: TenantChannelScope) =>
    post<{ principal_id: string; authority_bound: boolean }>(`/tenant-channels/feishu/identity/${action}`, { principal_id: principalId, ...scope }),
  feishuSharedGroups: (agentId: string) => get<FeishuSharedGroupEntry>(`/agents/${agentId}/channel/shared-groups`),
  saveFeishuSharedGroups: (agentId: string, expectedRevision: string, chatIds: string[]) =>
    put<FeishuSharedGroupEntry>(`/agents/${agentId}/channel/shared-groups`, { expected_revision: expectedRevision, chat_ids: chatIds }),
  get: (agentId: string) => get<AgentChannelConfig>(`/agents/${agentId}/channel`).catch(() => null),
  create: (agentId: string, data: unknown) => post<unknown>(`/agents/${agentId}/channel`, data),
  update: (agentId: string, data: unknown) => put<unknown>(`/agents/${agentId}/channel`, data),
  delete: (agentId: string) => del(`/agents/${agentId}/channel`),
  webhookUrl: (agentId: string) => get<{ webhook_url: string }>(`/agents/${agentId}/channel/webhook-url`).catch(() => null),
  feishuRegistrationStart: (agentId: string, platformRegion: FeishuPlatformRegion) =>
    post<FeishuAppRegistration>(`/agents/${agentId}/channel/registration/start`, {
      platform_region: platformRegion,
    }),
  feishuRegistrationActive: async (agentId: string, signal?: AbortSignal) => {
    try {
      return await get<FeishuAppRegistration>(
        `/agents/${agentId}/channel/registration/active`,
        { signal },
      );
    } catch (error) {
      if (error instanceof ApiError && error.status === 404) return null;
      throw error;
    }
  },
  feishuRegistrationGet: (agentId: string, sessionId: string, signal?: AbortSignal) =>
    get<FeishuAppRegistration>(
      `/agents/${agentId}/channel/registration/${encodeURIComponent(sessionId)}`,
      { signal },
    ),
  feishuRegistrationCancel: (agentId: string, sessionId: string) =>
    post<FeishuAppRegistration>(
      `/agents/${agentId}/channel/registration/${encodeURIComponent(sessionId)}/cancel`,
      {},
    ),
  getChannelConfig: (agentId: string, slug: string) => get<unknown>(`/agents/${agentId}/${slug}`).catch(() => null),
  getChannelWebhook: (agentId: string, slug: string) =>
    get<{ webhook_url: string }>(`/agents/${agentId}/${slug}/webhook-url`).catch(() => null),
  createChannelConfig: (agentId: string, slug: string, data: unknown) => post<unknown>(`/agents/${agentId}/${slug}`, data),
  deleteChannelConfig: (agentId: string, slug: string) => del(`/agents/${agentId}/${slug}`),
  testChannelConfig: (agentId: string, slug: string, data?: unknown) => post<unknown>(`/agents/${agentId}/${slug}/test`, data),

  // Personal WeChat (iLink QR scan)
  wechatPersonalQrStart: (agentId: string) =>
    post<{ session_key: string; qr_image_url: string | null; message: string }>(`/agents/${agentId}/wechat-personal/qr-start`, {}),
  wechatPersonalQrWait: (agentId: string, sessionKey: string) =>
    post<{ connected: boolean; account_id?: string; session_key?: string; qr_image_url?: string; message: string }>(
      `/agents/${agentId}/wechat-personal/qr-wait`, { session_key: sessionKey }),
  wechatPersonalConnect: (agentId: string, sessionKey: string) =>
    post<WeChatPersonalStatus>(`/agents/${agentId}/wechat-personal/connect`, { session_key: sessionKey }),
  wechatPersonalStatus: (agentId: string) =>
    get<WeChatPersonalStatus>(`/agents/${agentId}/wechat-personal/status`).catch(() => null),
  wechatPersonalDisconnect: (agentId: string) =>
    del(`/agents/${agentId}/wechat-personal`),
};
