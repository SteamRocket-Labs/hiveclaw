import { del, get, post } from '../core';

export interface PlazaPostListParams {
  limit?: number;
  offset?: number;
  since?: string;
  tenantId?: string;
}

export const plazaApi = {
  listPosts: (params?: PlazaPostListParams) => {
    const qs = new URLSearchParams();
    if (params?.limit !== undefined) qs.set('limit', String(params.limit));
    if (params?.offset !== undefined) qs.set('offset', String(params.offset));
    if (params?.since) qs.set('since', params.since);
    if (params?.tenantId) qs.set('tenant_id', params.tenantId);
    const query = qs.toString();
    return get<unknown[]>(`/plaza/posts${query ? `?${query}` : ''}`);
  },
  getStats: (tenantId?: string) => get<unknown>(`/plaza/stats${tenantId ? `?tenant_id=${tenantId}` : ''}`),
  getPost: (postId: string) => get<unknown>(`/plaza/posts/${postId}`),
  createPost: (data: { content: string; tenant_id?: string }) => post<unknown>('/plaza/posts', data),
  createComment: (postId: string, data: { content: string }) => post<unknown>(`/plaza/posts/${postId}/comments`, data),
  toggleLike: (postId: string) => post<{ liked: boolean }>(`/plaza/posts/${postId}/like`, {}),
  removePost: (postId: string) => del(`/plaza/posts/${postId}`),
  listUsers: (tenantId?: string) => get<unknown[]>(`/org/users${tenantId ? `?tenant_id=${tenantId}` : ''}`),
};
