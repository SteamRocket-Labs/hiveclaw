import { get } from '../core';

export interface ToolFailureCountRow {
  count: number;
  tool_name?: string;
  provider?: string;
  error_class?: string;
  http_status?: number;
}

export interface ToolFailureRecentRow {
  summary: string;
  tool_name?: string;
  provider?: string;
  error_class?: string;
  http_status?: number | null;
  retryable?: boolean | null;
  created_at?: string | null;
}

export interface ToolFailureSummary {
  total_errors: number;
  by_tool: ToolFailureCountRow[];
  by_provider: ToolFailureCountRow[];
  by_error_class: ToolFailureCountRow[];
  by_http_status: ToolFailureCountRow[];
  recent_errors: ToolFailureRecentRow[];
}

export const activityApi = {
  list: (agentId: string, limit = 50) => get<any[]>(`/agents/${agentId}/activity?limit=${limit}`),
  getToolFailureSummary: (agentId: string, hours = 24, limit = 500) =>
    get<ToolFailureSummary>(`/agents/${agentId}/activity/tool-failures?hours=${hours}&limit=${limit}`),
};
