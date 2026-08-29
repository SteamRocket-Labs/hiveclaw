import type { useTranslation } from 'react-i18next';

export type ActivityTranslator = ReturnType<typeof useTranslation>['t'];

// Tool-call activity summaries written before the payload-hygiene fix embed the
// raw result text. Hygiene is unconditional for tool_call action types. Exact
// built-in actions get a user-facing label; every other internal identifier
// falls back to a generic action. Full evidence stays behind progressive
// disclosure / operator views.
export function activityDisplaySummary(act: any, t: ActivityTranslator): string {
  const actionType = String(act?.action_type || '');
  if (actionType === 'tool_call' || actionType === 'tool_call_approved') {
    const detail = act?.detail as Record<string, unknown> | null | undefined;
    const toolName = typeof detail?.tool === 'string' ? String(detail.tool).trim() : '';
    if (actionType === 'tool_call') {
      if (toolName === 'read_ledger') return t('dashboard.activity.toolReadLedger', 'Reviewed work progress');
      if (toolName === 'track_todo') return t('dashboard.activity.toolTrackTodo', 'Updated work progress');
    }
    return actionType === 'tool_call_approved'
      ? t('dashboard.activity.toolCallApprovedGeneric', 'Approved tool call')
      : t('dashboard.activity.toolCallGeneric', 'Tool call');
  }
  return act?.summary || '';
}

// Known activity action types map to translations; an unknown backend code must
// not render as raw snake_case prose for normal users.
export function activityActionTypeLabel(actionType: unknown, t: ActivityTranslator): string {
  const code = String(actionType || '').trim();
  const known: Record<string, string> = {
    chat_reply: t('agent.activityLog.actionTypes.chatReply', 'Chat reply'),
    tool_call: t('agent.activityLog.actionTypes.toolCall', 'Tool call'),
    tool_call_approved: t('agent.activityLog.actionTypes.toolCallApproved', 'Approved tool call'),
    task_created: t('agent.activityLog.actionTypes.taskCreated', 'Task created'),
    task_updated: t('agent.activityLog.actionTypes.taskUpdated', 'Task updated'),
    file_written: t('agent.activityLog.actionTypes.fileWritten', 'File written'),
    error: t('agent.activityLog.actionTypes.error', 'Error'),
    heartbeat: t('agent.activityLog.actionTypes.heartbeat', 'Heartbeat'),
    plaza_post: t('agent.activityLog.actionTypes.plazaPost', 'Plaza post'),
    schedule_run: t('agent.activityLog.actionTypes.scheduleRun', 'Scheduled run'),
    feishu_msg_sent: t('agent.activityLog.actionTypes.feishuMsgSent', 'Feishu message'),
    agent_msg_sent: t('agent.activityLog.actionTypes.agentMsgSent', 'Agent message'),
    web_msg_sent: t('agent.activityLog.actionTypes.webMsgSent', 'Web message'),
  };
  return known[code] || t('agent.activityLog.actionTypes.other', 'Activity');
}
