import { get, post, put, del } from '../core';

export const channelApi = {
  get: (agentId: string) => get<unknown>(`/agents/${agentId}/channel`).catch(() => null),
  create: (agentId: string, data: unknown) => post<unknown>(`/agents/${agentId}/channel`, data),
  update: (agentId: string, data: unknown) => put<unknown>(`/agents/${agentId}/channel`, data),
  delete: (agentId: string) => del(`/agents/${agentId}/channel`),
  webhookUrl: (agentId: string) => get<{ webhook_url: string }>(`/agents/${agentId}/channel/webhook-url`).catch(() => null),
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
    post<{ connected: boolean; account_id?: string }>(`/agents/${agentId}/wechat-personal/connect`, { session_key: sessionKey }),
  wechatPersonalStatus: (agentId: string) =>
    get<{ connected: boolean; account_id?: string }>(`/agents/${agentId}/wechat-personal/status`).catch(() => null),
  wechatPersonalDisconnect: (agentId: string) =>
    del(`/agents/${agentId}/wechat-personal`),
};
