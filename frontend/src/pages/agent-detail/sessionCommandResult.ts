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

export function formatSlashCommandResult(response: ExecuteCommandResult): string {
  const uiAction = getSessionCommandUiAction(response);
  if (uiAction) {
    if (typeof uiAction.message === 'string' && uiAction.message.trim()) return uiAction.message.trim();
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
  }

  const serialized = result == null ? '' : JSON.stringify(result, null, 2);
  if (!serialized.trim()) return `Command ${response.command} completed.`;
  return `Command ${response.command} completed.\n\n\`\`\`json\n${serialized}\n\`\`\``;
}
