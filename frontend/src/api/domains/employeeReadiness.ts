import { get, post } from '../core';

export interface EmployeeReadinessCheck {
  id: string;
  agent_id: string;
  request_id: string;
  execution_status: string;
  created_at: string;
  observed_at: string | null;
  checks: Array<{ key: string; status: 'passed' | 'blocked' | 'unverified' | 'not_applicable'; code?: string; model_label?: string | null }>;
  error_code: string | null;
  claim_version: number;
}
function base(agentId: string, teamId?: string) {
  return `${teamId ? `/business-teams/${teamId}` : ''}/agents/${agentId}/readiness-checks`;
}
export const employeeReadinessApi = {
  latest: (agentId: string, teamId?: string) => get<EmployeeReadinessCheck | null>(`${base(agentId, teamId)}/latest`),
  get: (agentId: string, id: string, teamId?: string) => get<EmployeeReadinessCheck>(`${base(agentId, teamId)}/${id}`),
  start: (agentId: string, requestId: string, teamId?: string) => post<EmployeeReadinessCheck>(base(agentId, teamId), { request_id: requestId }),
  cancel: (agentId: string, id: string, teamId?: string) => post<{ id: string; execution_status: string }>(`${base(agentId, teamId)}/${id}/cancel`),
};
