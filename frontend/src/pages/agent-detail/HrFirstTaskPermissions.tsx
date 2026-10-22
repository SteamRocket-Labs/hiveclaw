import { useTranslation } from 'react-i18next';
import { chatApi } from '../../api/domains/chat';
import type { HrCreationDraft, HrFirstTaskPermission } from '../../api/domains/hrCreation';
import SessionPermissionActions from './SessionPermissionActions';

function currentPermission(agentId: string, draft: HrCreationDraft, permission: HrFirstTaskPermission): boolean {
  const work = draft.first_task_work;
  if (!work) return false;
  return draft.hr_agent_id === agentId && draft.draft_status === 'completed'
    && draft.created_agent_id === work.agent_id && ['running', 'suspended'].includes(work.status)
    && permission.session_id === work.session_id && permission.request.session_id === work.session_id
    && permission.request.runtime_task_id === work.runtime_task_id;
}

export async function resolveFirstTaskPermission(
  agentId: string, draft: HrCreationDraft, permission: HrFirstTaskPermission, userId: string | undefined,
  action: 'allow_once' | 'allow_session' | 'deny',
  changedMessage = 'This permission request is no longer current. Refresh the assignment.',
) {
  if (!currentPermission(agentId, draft, permission) || !permission.can_resolve || !userId
    || draft.requested_by_user_id !== userId || permission.request.requested_by_user_id !== userId) {
    throw new Error(changedMessage);
  }
  return chatApi.resolveSessionPermission(draft.first_task_work!.agent_id, permission.session_id,
    permission.request.permission_request_id, { action });
}

export default function HrFirstTaskPermissions({ agentId, draft, userId, onResolved }: {
  agentId: string; draft: HrCreationDraft; userId?: string; onResolved: () => Promise<void>;
}) {
  const { t } = useTranslation();
  return <>{(draft.first_task_work?.permissions || []).filter((permission) => currentPermission(agentId, draft, permission)).map((permission) => (
    <section key={permission.request.permission_request_id} data-testid="first-task-permission">
      <h4>{t('agent.chat.permission.title', 'Tool permission')}</h4>
      <p>{permission.request.tool_display_name || permission.request.tool_name}</p>
      <details><summary>{t('agent.chat.permission.actionDetails', 'Action details')}</summary>
        <pre>{JSON.stringify(permission.request.arguments || {}, null, 2)}</pre></details>
      <SessionPermissionActions permissionRequest={permission.request} t={t}
        readOnly={!permission.can_resolve || draft.requested_by_user_id !== userId || permission.request.requested_by_user_id !== userId}
        onResolveSessionPermission={async (_request, action) => {
          await resolveFirstTaskPermission(agentId, draft, permission, userId, action,
            t('agent.chat.permission.changed', 'This permission request is no longer current. Refresh the assignment.'));
          await onResolved();
        }} />
    </section>
  ))}</>;
}
