// Display only. Execution and recovery continue to use their original values.
const groups = {
  running: ['active', 'queued', 'pending', 'created', 'started', 'in_progress', 'running', 'streaming', 'resuming', 'resumable', 'starting', 'thinking', 'responding', 'tool_running', 'hook_evaluating', 'compacting', 'summarizing', 'continuation_gap', 'processing', 'retrying', 'summary_only', 'waiting_for_child', 'waiting_for_workflow', 'cancelling'],
  waiting: ['waiting', 'waiting_user', 'waiting_for_user', 'awaiting_user_clarification', 'awaiting_permission', 'gate_waiting', 'waiting_for_gate', 'awaiting_approval', 'approval_required', 'waiting_approval', 'waiting_budget_approval', 'awaiting_budget', 'suspended', 'blocked', 'waiting_for_permission'],
  done: ['idle', 'ready', 'complete', 'completed', 'succeeded', 'done', 'success', 'delivered', 'sent'],
  failed: ['failed', 'error', 'provider_error', 'quota_denied', 'quota_unavailable', 'not_admitted', 'skipped', 'needs_reconciliation', 'interrupted', 'dead_letter', 'denied', 'unavailable', 'unknown'],
  stopped: ['stopped', 'killed', 'cancelled', 'canceled', 'hard_stopped', 'expired', 'exhausted', 'paused', 'budget_limited', 'usage_limited'],
};
export type RuntimeDisplayStatus = keyof typeof groups;
const labels = { running: 'Running', waiting: 'Waiting for you', done: 'Done', failed: 'Failed', stopped: 'Stopped' };
const statusKeys = new Map(Object.entries(groups).flatMap(([key, values]) => values.map(value => [value, key as RuntimeDisplayStatus] as const)));
export function runtimeStatusKey(status: unknown): RuntimeDisplayStatus {
  return statusKeys.get(String(status || '').trim().toLowerCase()) || 'failed';
}
export function userFacingRuntimeStatus(status: unknown): string {
  return labels[runtimeStatusKey(status)];
}
export function runtimeStatusLabel(status: unknown, t: (key: string, fallback: string) => string, phase?: string): string {
  if (phase === 'queued') return t('agent.aware.workQueued', 'Queued');
  const key = runtimeStatusKey(status);
  return t(`agent.runtimeStatus.${key}`, labels[key]);
}
