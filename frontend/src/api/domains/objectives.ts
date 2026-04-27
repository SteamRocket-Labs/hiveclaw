import { get, patch, post } from '../core';

export const objectiveApi = {
  list: (agentId: string) => get<any[]>(`/agents/${agentId}/objectives`),
  propose: (agentId: string, data: any) => post<any>(`/agents/${agentId}/objectives/proposals`, data),
  update: (agentId: string, objectiveId: string, data: any) =>
    patch<any>(`/agents/${agentId}/objectives/${objectiveId}`, data),
  approve: (agentId: string, objectiveId: string, data: { reason?: string } = {}) =>
    post<any>(`/agents/${agentId}/objectives/${objectiveId}/approve`, data),
  reject: (agentId: string, objectiveId: string, data: { reason?: string } = {}) =>
    post<any>(`/agents/${agentId}/objectives/${objectiveId}/reject`, data),
};
