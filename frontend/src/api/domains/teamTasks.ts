import { get, post } from '../core';

export interface SharedTeamTask {
  id: string;
  agent_id: string;
  agent_name: string;
  title: string;
  description: string | null;
  status: string;
  priority: string;
  responsible_user_id: string;
  responsible_name: string | null;
  created_by_name: string | null;
  updated_at: string;
  can_handover: boolean;
}
export interface TaskResponsibilityChange {
  request_id: string;
  responsible_user_id: string | null;
  expected_updated_at: string;
  reason: string;
}
export interface TaskSharingChange extends TaskResponsibilityChange { business_team_id: string | null }
export interface TaskChangeReceipt { receipt_id: string; task: SharedTeamTask; paused_routine_count: number }
export interface TeamTaskQueue {
  tenant_id: string;
  business_team_id: string;
  tasks: SharedTeamTask[];
  assignees: Array<{ id: string; name: string }>;
  next_offset: number | null;
}
export const teamTasksApi = {
  list: (teamId: string, offset = 0) => get<TeamTaskQueue>(`/business-teams/${teamId}/tasks?limit=10&offset=${offset}`),
  share: (agentId: string, taskId: string, body: TaskSharingChange) =>
    post<TaskChangeReceipt>(`/agents/${agentId}/tasks/${taskId}/team-sharing`, body),
  handover: (teamId: string, taskId: string, body: TaskResponsibilityChange) =>
    post<TaskChangeReceipt>(`/business-teams/${teamId}/tasks/${taskId}/handover`, body),
};
