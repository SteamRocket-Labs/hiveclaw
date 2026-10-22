import React from 'react';
import { useTranslation } from 'react-i18next';
import { chatApi } from '../../api/domains/chat';
import type { BusinessTaskPermission } from '../../api/domains/tasks';
import type { Task } from '../../types';
import SessionPermissionActions from './SessionPermissionActions';

type PermissionDecision = 'allow_once' | 'allow_session' | 'deny';

function currentPermission(agentId: string, task: Task, permission: BusinessTaskPermission): boolean {
  return task.agent_id === agentId && task.status === 'doing' && task.last_execution_status === 'awaiting_permission'
    && Boolean(task.active_runtime_task_id && task.reflection_session_id)
    && permission.session_id === task.reflection_session_id
    && permission.request.session_id === permission.session_id
    && permission.request.runtime_task_id === task.active_runtime_task_id;
}

export async function resolveBusinessTaskPermission(
  agentId: string, task: Task, permission: BusinessTaskPermission, userId: string | undefined,
  action: PermissionDecision, changedMessage = 'This permission request is no longer current. Refresh the assignment.',
) {
  if (!currentPermission(agentId, task, permission) || !permission.can_resolve || !userId
    || permission.request.requested_by_user_id !== userId) throw new Error(changedMessage);
  return chatApi.resolveSessionPermission(agentId, permission.session_id,
    permission.request.permission_request_id, { action });
}

export default function BusinessTaskPermissions({ agentId, task, permissions, userId, onResolved }: {
  agentId: string; task: Task; permissions: BusinessTaskPermission[]; userId?: string; onResolved: () => Promise<void>;
}) {
  const { t } = useTranslation();
  return <>{permissions.filter((permission) => currentPermission(agentId, task, permission)).map((permission) => (
    <article key={`${task.active_runtime_task_id}:${permission.request.permission_request_id}`}
      className="business-task-recovery" data-testid="business-task-permission">
      <h4>{t('agent.chat.permission.title', 'Tool permission')}</h4>
      <p>{permission.request.tool_display_name || permission.request.tool_name}</p>
      <details><summary>{t('agent.chat.permission.actionDetails', 'Action details')}</summary>
        <pre>{JSON.stringify(permission.request.arguments || {}, null, 2)}</pre></details>
      <SessionPermissionActions permissionRequest={permission.request} t={t}
        readOnly={!permission.can_resolve || permission.request.requested_by_user_id !== userId}
        onResolveSessionPermission={async (_request, action) => {
          await resolveBusinessTaskPermission(agentId, task, permission, userId, action,
            t('agent.chat.permission.changed', 'This permission request is no longer current. Refresh the assignment.'));
          await onResolved();
        }} />
    </article>
  ))}</>;
}
