import type { TFunction } from 'i18next';
import { runtimeStatusLabel } from './runtimeStatus';
import type { RunStepSnapshot } from './chatDisclosureReducer';

// System event copy is rendered from protocol fields. Model prose and tool
// outputs keep their existing renderers; the original event is not rewritten.
const SYSTEM_LABELS: Record<string, string> = {
  permission: 'permission',
  permission_request: 'permission',
  permission_resolved: 'approvalDecision',
  tool_group_activation: 'toolsReady',
  pack_activation: 'toolsReady',
  deferred_tools_delta: 'toolsReady',
  team_memory: 'sharedMemory',
  hook_progress: 'requestCheck',
  hook_summary: 'requestCheck',
  hook_attachment: 'contextAdded',
  hook_blocked: 'actionBlocked',
  workflow_run: 'taskProgress',
  workflow_step: 'taskProgress',
  dynamic_workflow: 'taskProgress',
  child_session: 'taskProgress',
  agent_task_notification: 'taskProgress',
  subagent: 'taskProgress',
  team_member: 'coworkerAdded',
  schedule: 'scheduledTask',
  schedule_fire: 'scheduledTask',
  goal: 'taskProgress',
  once: 'scheduledTask',
  memory_candidate: 'memoryReview',
  artifact_update: 'fileUpdated',
  artifact_delivery: 'fileReady',
  file_changes: 'fileUpdated',
  runtime_action_started: 'taskProgress',
  runtime_action_progress: 'taskProgress',
  runtime_action_completed: 'taskProgress',
  runtime_action_blocked: 'actionBlocked',
  runtime_action_failed: 'taskProgress',
  runtime_action_unconfirmed: 'outcomeUnknown',
};

export function presentSystemStep(step: RunStepSnapshot, t: TFunction): RunStepSnapshot {
  const event = step.systemEvent;
  if (!event) return step;
  const metadata = event.metadata;
  if (event.type === 'agent_task_notification' && (
    metadata.runtime_result_integration_page === true || metadata.task_type === 'runtime_result_integration'
  )) {
    const count = metadata.item_count;
    const title = typeof count === 'number' && Number.isInteger(count) && count >= 0
      ? t('agent.chat.disclosure.system.resultsReceived', 'Received {{count}} task results', { count })
      : t('agent.chat.disclosure.system.resultsAvailable', 'Task results received');
    const held = metadata.continuation_status === 'denied' || metadata.continuation_status === 'needs_reconciliation';
    const review = held
      ? t('agent.chat.disclosure.system.resultsKept', 'Results are kept for review; this conversation did not continue.')
      : undefined;
    return { ...step, title, summary: review, details: review };
  }
  const label = SYSTEM_LABELS[event.type];
  if (!label) return step;
  const title = t(`agent.chat.disclosure.system.${label}`);
  if (event.type === 'goal') {
    // The objective is member-authored content, including credential markers.
    const objective = typeof metadata.objective === 'string' ? metadata.objective : undefined;
    return { ...step, title, summary: objective ?? step.summary, details: objective ?? step.details };
  }
  if (event.type === 'child_session') return { ...step, title };
  if (event.type === 'agent_task_notification' && typeof metadata.summary === 'string') {
    return { ...step, title, summary: runtimeStatusLabel(metadata.status || step.status, t), details: metadata.summary };
  }
  const summary = ['taskProgress', 'requestCheck', 'scheduledTask'].includes(label)
    ? runtimeStatusLabel(metadata.status || step.status, t)
    : undefined;
  return { ...step, title, summary, details: undefined };
}
