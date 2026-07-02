import type { ExecuteCommandResult } from '../../api/domains/ccParity';

export interface SessionCommandUiAction {
  type: string;
  session_id?: string;
  message?: string;
  level?: string;
  reason?: string;
  [key: string]: unknown;
}

export function commandResultRecord(response: ExecuteCommandResult): Record<string, unknown> | null {
  const { result } = response;
  if (!result || typeof result !== 'object' || Array.isArray(result)) return null;
  return result as Record<string, unknown>;
}

export function getSessionCommandUiAction(response: ExecuteCommandResult): SessionCommandUiAction | null {
  const record = commandResultRecord(response);
  const action = record?.ui_action;
  if (!action || typeof action !== 'object' || Array.isArray(action)) return null;
  const typed = action as Record<string, unknown>;
  return typeof typed.type === 'string' && typed.type.trim() ? typed as SessionCommandUiAction : null;
}

export function isSessionControlCommandResult(response: ExecuteCommandResult): boolean {
  const record = commandResultRecord(response);
  return Boolean(record && typeof record.action === 'string' && getSessionCommandUiAction(response));
}

// Per-action expression (§4.3): each ui_action type maps to a session-state
// sentence plus the right-panel surface where its effect lands. Raw JSON never
// reaches the transcript — unmapped results collapse to a compact summary.
const UI_ACTION_EXPRESSIONS: Record<string, { label: string; panel?: string }> = {
  session_rewound: { label: 'Session rewound to the selected checkpoint.' },
  session_branched: { label: 'Branched into a new session line.' },
  session_compacted: { label: 'Context compacted.', panel: 'context' },
  checkpoint_created: { label: 'Checkpoint saved.' },
  session_resumed: { label: 'Session resumed.' },
  session_exported: { label: 'Session exported.', panel: 'workspace' },
  permission_updated: { label: 'Permission mode updated.', panel: 'governance' },
  plan_updated: { label: 'Plan updated.', panel: 'tasks' },
  workflow_started: { label: 'Workflow run started.', panel: 'workflows' },
  workflow_resumed: { label: 'Workflow run resumed.', panel: 'workflows' },
};

export function getSessionCommandPanelTarget(response: ExecuteCommandResult): string | null {
  const uiAction = getSessionCommandUiAction(response);
  if (!uiAction) return null;
  return UI_ACTION_EXPRESSIONS[uiAction.type]?.panel ?? null;
}

function compactResultSummary(record: Record<string, unknown>): string {
  const interesting = ['status', 'action', 'count', 'session_id', 'checkpoint_event_id', 'run_id']
    .map((key) => (record[key] != null ? `${key}=${String(record[key])}` : ''))
    .filter(Boolean);
  return interesting.join(' · ');
}

export function formatSlashCommandResult(response: ExecuteCommandResult): string {
  const uiAction = getSessionCommandUiAction(response);
  if (uiAction) {
    if (typeof uiAction.message === 'string' && uiAction.message.trim()) return uiAction.message.trim();
    const mapped = UI_ACTION_EXPRESSIONS[uiAction.type];
    if (mapped) return mapped.label;
    const record = commandResultRecord(response);
    if (typeof record?.message === 'string' && record.message.trim()) return record.message.trim();
    return `Command ${response.command} completed.`;
  }

  const { result } = response;
  if (typeof result === 'string') return result.trim() || `Command ${response.command} completed.`;
  if (result && typeof result === 'object' && !Array.isArray(result)) {
    const record = result as Record<string, unknown>;
    if (typeof record.message === 'string' && record.message.trim()) {
      return record.message.trim();
    }
    // No raw JSON in the transcript: collapse to a compact key summary.
    const summary = compactResultSummary(record);
    return summary
      ? `Command ${response.command} completed. ${summary}`
      : `Command ${response.command} completed.`;
  }

  return `Command ${response.command} completed.`;
}
