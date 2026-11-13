import type { TFunction } from 'i18next';
import { runtimeStatusLabel } from './runtimeStatus';
import type { RunStepSnapshot } from './chatDisclosureReducer';
import { memberToolSummarySentence } from './memberToolSummary';

export function isKeptResultStep(step: RunStepSnapshot): boolean {
  const event = step.systemEvent;
  return event?.type === 'agent_task_notification'
    && (event.metadata.runtime_result_integration_page === true || event.metadata.task_type === 'runtime_result_integration')
    && ['denied', 'needs_reconciliation'].includes(String(event.metadata.continuation_status))
    && hasKeptResult(event.metadata);
}

function hasKeptResult(metadata: Record<string, unknown>): boolean {
  if (typeof metadata.has_kept_result === 'boolean') return metadata.has_kept_result;
  const manifest = metadata.result_manifest as { items?: Array<{ terminal_status?: string; result_size_bytes?: number; artifact_count?: number }> } | undefined;
  return (manifest?.items || []).some(item => (item.artifact_count || 0) > 0
    || (item.terminal_status === 'completed' && (item.result_size_bytes || 0) > 0));
}

// Match only known producer templates. Unmatched sentences and model prose
// stay as stored, including specific approval and delegation outcomes.
export function presentSystemStep(step: RunStepSnapshot, t: TFunction): RunStepSnapshot {
  const event = step.systemEvent;
  if (!event) return step;
  const metadata = event.metadata;
  const text = typeof step.details === 'string' ? step.details : step.summary || '';
  const display = (title: string, summary?: string): RunStepSnapshot => ({ ...step, title, titleIsLocalized: true, summary, details: summary });
  const delegation = metadata.action_kind === 'a2a_delegation' || (event.type === 'child_session' && String(metadata.reason || '').startsWith('delegation_'));
  if (delegation && (['killed', 'cancelled'].includes(String(metadata.status))
    || (event.type === 'runtime_action_started' && ['completed', 'failed', 'error', 'blocked', 'skipped'].includes(String(metadata.status))))) {
    const status = String(metadata.status);
    const sentence = memberToolSummarySentence({ kind: 'delegation', status: status === 'killed' ? 'cancelled' : status,
      state: status === 'completed' ? 'done' : 'not_done' }, t)!;
    return event.type === 'child_session' ? { ...step, summary: sentence, details: sentence }
      : display(t('agent.chat.disclosure.tools.delegateWork', 'Delegate work'), sentence);
  }
  if (step.status === 'failed') {
    const actor = metadata.target_agent_name;
    const prefix = event.type === 'runtime_action_failed' && metadata.action_kind === 'a2a_delegation'
      && typeof actor === 'string' && text.startsWith(`${actor} 的委派任务失败：`)
      ? `${actor} 的委派任务失败：` : '';
    const reason = text.slice(prefix.length);
    const localized = /^\[Runtime Limit\] This turn stopped because the configured token budget was exhausted \(\d+\/\d+ tokens used\)\.$/.test(reason)
      ? t('agent.chat.disclosure.failure.tokenBudget', 'This turn used up its usage budget and did not finish.')
      : /^I reached the configured tool-round limit \(\d+\) before I could finish\. The current state has been saved; continue the task to resume from here, or raise max_tool_rounds for this agent\.$/.test(reason)
        ? t('agent.chat.disclosure.failure.toolRound', 'This turn reached its round limit and did not finish. Retry to continue.') : null;
    if (localized) return { ...step, summary: prefix + localized, details: prefix + localized };
  }
  const statusLabel = (status: unknown) => status === 'needs_reconciliation'
    ? t('agent.businessTasks.status.needs_reconciliation', 'Review required')
    : runtimeStatusLabel(status, t);
  if (event.type === 'agent_task_notification' && (
    metadata.runtime_result_integration_page === true || metadata.task_type === 'runtime_result_integration'
  ) && /^\d+ runtime result\(s\) ready in integration epoch \d+ \(terminal=\d+\/expected=\d+\)\.(?: Parent continuation denied: .+)?$/.test(text)) {
    const count = metadata.item_count;
    const stopped = ['denied', 'needs_reconciliation'].includes(String(metadata.continuation_status));
    const title = stopped && !hasKeptResult(metadata)
      ? t('agent.chat.disclosure.system.colleagueEnded', 'The colleague task has ended')
      : typeof count === 'number' && Number.isInteger(count) && count >= 0
      ? t('agent.chat.disclosure.system.resultsReceived', 'Received {{count}} task results', { count })
      : t('agent.chat.disclosure.system.resultsAvailable', 'Task results received');
    const review = stopped ? hasKeptResult(metadata)
      ? t('agent.chat.disclosure.system.resultsKept', 'Results are kept for review; this conversation did not continue.')
      : t('agent.chat.disclosure.system.noResultKept', 'There is no result to view; this conversation did not continue.') : undefined;
    const items = (metadata.result_manifest as { items?: Array<{ terminal_status?: string }> } | undefined)?.items || [];
    const cancelled = items.length > 0 && items.every(item => ['killed', 'cancelled', 'canceled'].includes(String(item.terminal_status)));
    return { ...display(title, review), ...(cancelled ? { status: 'cancelled' as const } : {}) };
  }
  if (event.type === 'team_member' && typeof metadata.member_name === 'string'
    && text === `Team member created: ${metadata.member_name}`) {
    return display(t('agent.chat.disclosure.system.coworkerAdded', 'Digital employee added: {{name}}', { name: metadata.member_name }));
  }
  if (event.type === 'file_changes' && text === 'file_changes' && typeof metadata.file_change_count === 'number') {
    return display(t('agent.chat.disclosure.system.filesUpdated', 'Updated {{count}} files', { count: metadata.file_change_count }));
  }
  if (['tool_group_activation', 'pack_activation', 'deferred_tools_delta'].includes(event.type)
    && (text === 'Activated MCP runtime tool group.' || text === 'Activated runtime tool groups for this task.'
      || /^Discovered deferred tools: [^\n]+$/.test(text))) {
    return display(t('agent.chat.disclosure.system.toolsReady', 'Tools ready'));
  }
  if ((event.type === 'workflow_run' || event.type === 'workflow_step' || metadata.action_kind === 'workflow')
    && text === `Workflow ${metadata.workflow_step_id ? `step ${metadata.workflow_step_id}` : 'run'} ${metadata.status}`) {
    return display(t('agent.chat.disclosure.system.taskProgress', 'Task update'), statusLabel(metadata.status));
  }
  if (event.type === 'schedule_fire' && typeof metadata.trigger_name === 'string'
    && text === `Workflow trigger fired: ${metadata.trigger_name}`) {
    return display(t('agent.chat.disclosure.system.scheduledTask', 'Scheduled task: {{name}}', { name: metadata.trigger_name }));
  }
  if (event.type === 'runtime_action_started' && typeof metadata.target_agent_name === 'string'
    && text === `Subagent ${metadata.target_agent_name} is running in the background.`) {
    return display(t('agent.chat.disclosure.system.subagentRunning', '{{name}} is working in the background.', { name: metadata.target_agent_name }));
  }
  if (event.type === 'goal' && typeof metadata.objective === 'string'
    && [`Goal started: ${metadata.objective}`, `Goal updated: ${metadata.objective}`, `Goal ${metadata.status}: ${metadata.objective}`].includes(text)) {
    return display(t('agent.chat.disclosure.system.taskProgress', 'Task update'), metadata.objective);
  }
  if (event.type === 'agent_task_notification' && typeof metadata.summary === 'string') {
    const childName = typeof metadata.child_agent_name === 'string' ? metadata.child_agent_name : undefined;
    const actor = childName || 'Background task';
    const status = metadata.status || 'completed';
    const summary = metadata.summary.trim();
    const clipped = summary.length > 240 ? `${summary.slice(0, 237).trimEnd()}...` : summary;
    if (text === `${actor} ${status}${clipped ? `: ${clipped}` : ''}`) {
      const name = childName || t('agent.chat.disclosure.system.taskProgress', 'Task update');
      return display(`${name} · ${statusLabel(status)}`, metadata.summary);
    }
  }
  return step;
}
