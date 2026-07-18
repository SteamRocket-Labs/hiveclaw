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

export interface WeChatPersonalStatus {
  connected: boolean;
  account_id?: string;
  transport_connected: boolean;
  identity_status: 'disconnected' | 'verified' | 'rebind_required' | 'access_denied';
  requires_rebind: boolean;
  requires_access_recovery: boolean;
}

export const channelApi = {
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
