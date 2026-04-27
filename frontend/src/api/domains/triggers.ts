import { get, post, patch, del } from '../core';

export const triggerApi = {
  list: (agentId: string) => get<any[]>(`/agents/${agentId}/triggers`),
  create: (agentId: string, data: any) =>
    post<any>(`/agents/${agentId}/triggers`, data),
  update: (agentId: string, triggerId: string, data: any) =>
    patch<any>(`/agents/${agentId}/triggers/${triggerId}`, data),
  delete: (agentId: string, triggerId: string) =>
    del(`/agents/${agentId}/triggers/${triggerId}`),
};
