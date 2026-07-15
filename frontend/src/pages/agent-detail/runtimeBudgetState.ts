export interface RuntimeBudgetState {
  schema: 'hive.runtime_budget_binding.v1';
  status: string;
  reason?: string;
  retryable: boolean;
  interactive?: boolean;
  workAmplifyingToolsDisabled: boolean;
}

export interface ProjectedSessionRunState {
  runId: string;
  status: string;
  runtimeBudget?: RuntimeBudgetState;
}

function asRecord(value: unknown): Record<string, unknown> | null {
  return value && typeof value === 'object' ? value as Record<string, unknown> : null;
}

export function normalizeRuntimeBudgetState(value: unknown): RuntimeBudgetState | undefined {
  const record = asRecord(value);
  if (!record || record.schema !== 'hive.runtime_budget_binding.v1' || typeof record.status !== 'string') {
    return undefined;
  }
  return {
    schema: 'hive.runtime_budget_binding.v1',
    status: record.status,
    ...(typeof record.reason === 'string' ? { reason: record.reason } : {}),
    retryable: record.retryable === true,
    ...(typeof record.interactive === 'boolean' ? { interactive: record.interactive } : {}),
    workAmplifyingToolsDisabled: record.work_amplifying_tools_disabled === true,
  };
}

export function sessionRunStateFromPayload(value: unknown): ProjectedSessionRunState {
  const record = asRecord(value) || {};
  const runtimeBudget = normalizeRuntimeBudgetState(record.runtime_budget);
  return {
    runId: String(record.run_id || ''),
    status: String(record.status || 'running'),
    ...(runtimeBudget ? { runtimeBudget } : {}),
  };
}
