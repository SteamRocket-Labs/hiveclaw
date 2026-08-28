import type { TFunction } from 'i18next';
import type { SessionCommandControlState } from './AgentChatSection';

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
