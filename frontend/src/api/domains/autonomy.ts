import { get } from '../core';

export interface AutonomyQuery {
  lookbackHours?: number;
}

export interface RuntimeTaskQuery {
  taskType?: string;
  triggerId?: string;
  objectiveId?: string;
  status?: string;
  limit?: number;
  diagnostics?: boolean;
}

const withParams = (path: string, params: Record<string, string | number | boolean | undefined>) => {
  const qs = new URLSearchParams();
  for (const [key, value] of Object.entries(params)) {
    if (value !== undefined && value !== '') qs.set(key, String(value));
  }
  const query = qs.toString();
  return query ? `${path}?${query}` : path;
};

export const autonomyApi = {
  getOverview: (agentId: string, query: AutonomyQuery = {}) =>
    get<any>(withParams(`/agents/${agentId}/autonomy/overview`, {
      lookback_hours: query.lookbackHours,
    })),
  getDiagnostics: (agentId: string, query: AutonomyQuery = {}) =>
    get<any>(withParams(`/agents/${agentId}/autonomy/diagnostics`, {
      lookback_hours: query.lookbackHours,
    })),
  listRuntimeTasks: (agentId: string, query: RuntimeTaskQuery = {}) =>
    get<any[]>(withParams(`/agents/${agentId}/runtime-tasks`, {
      task_type: query.taskType,
      trigger_id: query.triggerId,
      objective_id: query.objectiveId,
      status: query.status,
      limit: query.limit,
      diagnostics: query.diagnostics,
    })),
  getRuntimeArtifact: (agentId: string, runtimeTaskId: string, diagnostics = false) =>
    get<any>(withParams(`/agents/${agentId}/runtime-artifacts/${runtimeTaskId}`, {
      diagnostics: diagnostics || undefined,
    })),
};
