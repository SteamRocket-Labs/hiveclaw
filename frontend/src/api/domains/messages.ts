/**
 * Messages domain adapter — agent-to-agent inbox.
 * (User notifications are in notifications.ts)
 */
import { get, put } from '../core';

export const messageApi = {
  inbox: (limit = 50) => get<unknown[]>(`/messages/inbox?limit=${limit}`),
  unreadCount: () => get<{ unread_count: number }>('/messages/unread-count'),
  markRead: (id: string) => put<void>(`/messages/${id}/read`),
  markAllRead: () => put<void>('/messages/read-all'),
};
