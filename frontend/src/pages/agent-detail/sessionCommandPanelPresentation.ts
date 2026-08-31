import type { TFunction } from 'i18next';
import type { AgentPermissions } from '../../api/domains/agents';
import type { SessionContextUsage } from '../../api/domains/ccParity';
import type { SessionCommandControlState, SessionPermissionMode } from './AgentChatSection';

export interface ReadOnlySessionCommandPanelProps {
  contextUsage?: SessionContextUsage | null;
  agentUsage?: {
    usedToday?: number | null;
    limitToday?: number | null;
    usedMonth?: number | null;
    limitMonth?: number | null;
  };
  agentPermissions?: AgentPermissions | null;
  sessionPermissionMode?: SessionPermissionMode;
}

function record(value: unknown): Record<string, unknown> {
  return value != null && typeof value === 'object' && !Array.isArray(value)
    ? value as Record<string, unknown>
    : {};
}

function number(value: unknown): number | null {
  if (value == null || value === '') return null;
  const numeric = typeof value === 'number' ? value : Number(value);
  return Number.isFinite(numeric) ? numeric : null;
}

function tokens(value: number): string {
  return `${value.toLocaleString('en-US')} tokens`;
}

export function buildReadOnlySessionCommandDetails(
  control: SessionCommandControlState,
  props: ReadOnlySessionCommandPanelProps,
  permissionModeLabel: string,
): Array<[string, string]> | null {
  if (control.type === 'context_panel') {
    const context = props.contextUsage;
    if (!context) return [['Status', 'Loading context details…']];
    const used = number(context.used_tokens);
    const window = number(context.model_window_tokens);
    const selected = number(context.counts?.selected_contexts) ?? context.selected_contexts?.length ?? 0;
    const suppressed = number(context.counts?.suppressed_contexts) ?? context.suppressed_contexts?.length ?? 0;
    const details: Array<[string, string]> = [];
    if (used != null && window != null) {
      details.push(['Context window', `${used.toLocaleString('en-US')} / ${tokens(window)}`]);
    }
    details.push(
      ['Authorized context', `${selected.toLocaleString('en-US')} selected`],
      ['Unavailable context', `${suppressed.toLocaleString('en-US')} restricted or unavailable`],
      ['Loaded Skills', `${context.loaded_skills?.length ?? 0}`],
      ['Available tools', `${(context.active_tool_names?.length ?? 0).toLocaleString('en-US')}`],
    );
    return details;
  }
  if (control.type === 'usage_panel') {
    const usage = record(control.payload?.usage);
    const cost = record(control.payload?.cost);
    const input = number(usage.input_tokens) ?? 0;
    const output = number(usage.output_tokens) ?? 0;
    const total = number(usage.total_tokens) ?? input + output;
    const details: Array<[string, string]> = [['Session total', tokens(total)]];
    if (input || output) details.push(['Input / output', `${input.toLocaleString('en-US')} / ${tokens(output)}`]);
    const costUsd = number(cost.cost_usd);
    if (costUsd != null) {
      details.push(['Estimated cost', `$${costUsd.toLocaleString('en-US', { minimumFractionDigits: 2, maximumFractionDigits: 6 })}`]);
    }
    const today = number(props.agentUsage?.usedToday);
    const todayLimit = number(props.agentUsage?.limitToday);
    const month = number(props.agentUsage?.usedMonth);
    const monthLimit = number(props.agentUsage?.limitMonth);
    if (today != null) details.push(['Agent today', todayLimit != null ? `${today.toLocaleString('en-US')} / ${tokens(todayLimit)}` : tokens(today)]);
    if (month != null) details.push(['Agent this month', monthLimit != null ? `${month.toLocaleString('en-US')} / ${tokens(monthLimit)}` : tokens(month)]);
    return details;
  }
  if (control.type === 'permissions_panel') {
    const access = String(props.agentPermissions?.access_level || control.payload?.access_level || 'none');
    return [
      ['Agent access', access === 'manage' ? 'Manage' : access === 'use' ? 'Use' : access === 'view' ? 'View' : 'No access'],
      ['Session mode', permissionModeLabel],
      ['Policy boundary', 'Enterprise policies still apply.'],
    ];
  }
  return null;
}

type StatusUiAction =
  | 'open_resume_picker'
  | 'confirm_workspace_restore'
  | 'install_compacted_context'
  | 'install_active_projection'
  | 'install_workspace_snapshot'
  | 'install_active_projection_with_workspace';

type CopyKind =
  | 'resume_interrupted'
  | 'resume_ready'
  | 'resume_needs_reconciliation'
  | 'resume_active'
  | 'workspace_restore_confirmation'
  | 'context_compacted'
  | 'workspace_restored'
  | 'rewind_complete';

const COPY: Record<CopyKind, [string, string, string, string]> = {
  resume_interrupted: [
    'sessionWorkbench.commandPanel.resumeInterruptedTitle', 'Continue interrupted work',
    'sessionWorkbench.commandPanel.resumeInterruptedMessage', 'The previous turn stopped before completing. Continue from the last saved checkpoint.',
  ],
  resume_ready: [
    'sessionWorkbench.commandPanel.resumeReadyTitle', 'Session is ready',
    'sessionWorkbench.commandPanel.resumeReadyMessage', 'No interrupted work was found. You can send your next message normally.',
  ],
  resume_needs_reconciliation: [
    'sessionWorkbench.commandPanel.resumeReviewTitle', 'Review required before continuing',
    'sessionWorkbench.commandPanel.resumeReviewMessage', 'The previous request may have reached the model. To avoid duplicate work, this session is paused until an administrator verifies the delivery.',
  ],
  resume_active: [
    'sessionWorkbench.commandPanel.resumeActiveTitle', 'Work is already in progress',
    'sessionWorkbench.commandPanel.resumeActiveMessage', 'This session already has an active turn. Wait for it to finish or use the action it is currently requesting.',
  ],
  workspace_restore_confirmation: [
    'sessionWorkbench.commandPanel.confirmWorkspaceTitle', 'Restore workspace files?',
    'sessionWorkbench.commandPanel.confirmWorkspaceMessage', 'Only files changed by this session after the selected point will be restored. Newer or interleaved changes are protected.',
  ],
  context_compacted: [
    'sessionWorkbench.commandPanel.contextCompactedTitle', 'Context compacted',
    'sessionWorkbench.commandPanel.contextCompactedMessage', 'Future requests will use the compacted context.',
  ],
  workspace_restored: [
    'sessionWorkbench.commandPanel.workspaceRestoredTitle', 'Workspace restored',
    'sessionWorkbench.commandPanel.workspaceRestoredMessage', 'Workspace files were restored from the selected point.',
  ],
  rewind_complete: [
    'sessionWorkbench.commandPanel.rewindCompleteTitle', 'Rewind complete',
    'sessionWorkbench.commandPanel.rewindCompleteMessage', 'The selected request is back in the composer. Later history is preserved outside the active context.',
  ],
};

function copyKind(action: StatusUiAction, interrupted: boolean, resumeState?: string): CopyKind {
  if (action === 'open_resume_picker') {
    if (resumeState === 'needs_reconciliation') return 'resume_needs_reconciliation';
    if (resumeState === 'active') return 'resume_active';
    return interrupted ? 'resume_interrupted' : 'resume_ready';
  }
  if (action === 'confirm_workspace_restore') return 'workspace_restore_confirmation';
  if (action === 'install_compacted_context') return 'context_compacted';
  if (action === 'install_workspace_snapshot' || action === 'install_active_projection_with_workspace') {
    return 'workspace_restored';
  }
  return 'rewind_complete';
}

export function buildSessionCommandStatusControl(
  t: TFunction,
  action: StatusUiAction,
  input: {
    command?: string;
    payload: Record<string, unknown> | null;
    level?: 'success' | 'error' | 'info';
    interrupted?: boolean;
    resumeState?: string;
  },
): SessionCommandControlState {
  const [titleKey, titleFallback, messageKey, messageFallback] = COPY[
    copyKind(action, input.interrupted === true, input.resumeState)
  ];
  return {
    type: action === 'open_resume_picker'
      ? 'resume_picker'
      : action === 'confirm_workspace_restore' ? 'workspace_restore_confirmation' : 'projection_status',
    title: String(t(titleKey, titleFallback)),
    message: String(t(messageKey, messageFallback)),
    command: input.command,
    payload: input.payload,
    level: action === 'confirm_workspace_restore' ? 'info' : input.level,
  };
}
