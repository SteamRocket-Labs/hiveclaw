/**
 * Notifications domain adapter — unified notification center.
 *
 * Consolidates both /notifications and /messages endpoints.
 * The legacy /messages/* read endpoints are mapped to /notifications/*.
 */

import { get, post } from '../core';

export interface Notification {
  id: string;
  type: string;
  title?: string;
  body?: string;
  content?: string;
  is_read: boolean;
  created_at: string;
}

export interface UnreadCount {
  unread_count: number;
}

export interface NotificationListParams {
  limit?: number;
  offset?: number;
  unreadOnly?: boolean;
  category?: string;
}

export interface BroadcastNotificationPayload {
  title: string;
  body?: string;
}

export interface BroadcastNotificationResult {
  users_notified: number;
  agents_notified: number;
}

export const notificationsApi = {
  list: (params?: NotificationListParams) => {
    const qs = new URLSearchParams();
    if (params?.limit !== undefined) qs.set('limit', String(params.limit));
    if (params?.offset !== undefined) qs.set('offset', String(params.offset));
    if (params?.unreadOnly) qs.set('unread_only', 'true');
    if (params?.category) qs.set('category', params.category);
    const query = qs.toString();
    const suffix = query ? `?${query}` : '';
    return get<Notification[]>(`/notifications${suffix}`);
  },
  getUnreadCount: () => get<UnreadCount>('/notifications/unread-count'),
  markRead: (id: string) => post<void>(`/notifications/${id}/read`),
  markAllRead: () => post<void>('/notifications/read-all'),
  broadcast: (data: BroadcastNotificationPayload) =>
    post<BroadcastNotificationResult>('/notifications/broadcast', data),

  /** Agent-to-agent inbox (separate from user notifications) */
  getInbox: () => get<unknown[]>('/messages/inbox'),
  getInboxUnreadCount: () => get<UnreadCount>('/messages/unread-count'),
};
