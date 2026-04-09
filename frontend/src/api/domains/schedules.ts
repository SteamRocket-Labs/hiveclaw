import { get, post, patch, del } from '../core';

export const scheduleApi = {
  list: (agentId: string) => get<any[]>(`/agents/${agentId}/schedules/`),
  create: (agentId: string, data: { name: string; instruction: string; cron_expr: string }) =>
    post<any>(`/agents/${agentId}/schedules/`, data),
  update: (agentId: string, scheduleId: string, data: any) =>
    patch<any>(`/agents/${agentId}/schedules/${scheduleId}`, data),
  delete: (agentId: string, scheduleId: string) =>
    del(`/agents/${agentId}/schedules/${scheduleId}`),
  trigger: (agentId: string, scheduleId: string) =>
    post<any>(`/agents/${agentId}/schedules/${scheduleId}/run`),
  history: (agentId: string, scheduleId: string) =>
    get<any[]>(`/agents/${agentId}/schedules/${scheduleId}/history`),
};
