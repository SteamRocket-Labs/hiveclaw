import { get } from '../core';
import type { ToolFailureSummary } from './activity';
import type { ChatSession } from './chat';

export interface DashboardActivity {
  id: string;
  agent_id: string;
  action_type: string;
  summary: string;
  detail?: Record<string, unknown> | null;
  related_id?: string | null;
  created_at?: string | null;
  authority_source: string;
  operator_view: boolean;
}

export interface DashboardQueryEvidence {
  agent_count: number;
  session_limit: number;
  activity_limit: number;
  failure_hours: number;
  failure_limit: number;
  failure_rows_scanned: number;
  failure_rows_truncated: boolean;
}

export interface DashboardOverview {
  recent_sessions: ChatSession[];
  session_count: number;
  recent_activities: DashboardActivity[];
  tool_failures: Record<string, ToolFailureSummary>;
  query_evidence: DashboardQueryEvidence;
}

export const dashboardApi = {
  getOverview: (tenantId?: string) => {
    const params = new URLSearchParams();
    if (tenantId) params.set('tenant_id', tenantId);
    const query = params.toString();
    return get<DashboardOverview>(`/dashboard/overview${query ? `?${query}` : ''}`);
  },
};
