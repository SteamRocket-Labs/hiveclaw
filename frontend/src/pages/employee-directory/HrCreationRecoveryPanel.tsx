import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { useTranslation } from 'react-i18next';
import { Link } from 'react-router-dom';

import { hrCreationApi, type HrCreationDraft } from '../../api/domains/hrCreation';
import { ApiError } from '../../api/core';
import { requestAppConfirm, showAppToast } from '../../components/AppDialogs';
import { useAuthStore } from '../../stores';
import { isAdministratorRole } from '../../roles';
import { useAssistantTenant } from '../assistant/assistantScope';
import { blueprintStatusLabel } from '../agent-detail/hrCreationStatus';

import './HrCreationRecoveryPanel.css';

const ACTIVE_DRAFT_STATUSES = new Set(['confirmed', 'creating', 'provisioning']);

function draftName(draft: HrCreationDraft): string {
  const name = draft.blueprint?.name;
  return typeof name === 'string' && name.trim() ? name.trim() : 'Unfinished digital employee';
}

function draftFailure(draft: HrCreationDraft): string | null {
  const message = draft.failure?.message;
  if (typeof message === 'string' && message.trim()) return message.trim();
  const reason = draft.recovery?.reason;
  return typeof reason === 'string' && reason.trim() ? reason.trim() : null;
}

export default function HrCreationRecoveryPanel() {
  const { t } = useTranslation();
  const queryClient = useQueryClient();
  const user = useAuthStore((state) => state.user);
  const tenantId = useAssistantTenant(user?.tenant_id ?? null);
  const queryKey = ['hr-recoverable-drafts', user?.id, tenantId, user?.role] as const;
  const { data: drafts = [], error: draftsError, refetch: reloadDrafts } = useQuery({
    queryKey,
    queryFn: () => hrCreationApi.listMine(),
    enabled: Boolean(user?.id && tenantId),
    refetchInterval: (query) => (
      ((query.state.data as HrCreationDraft[] | undefined) || []).some((draft) => ACTIVE_DRAFT_STATUSES.has(draft.draft_status))
        ? 3_000
        : false
    ),
  });
  const refresh = async () => {
    await Promise.all([
      queryClient.invalidateQueries({ queryKey }),
      queryClient.invalidateQueries({ queryKey: ['agents'] }),
    ]);
  };
  const sourceAgentId = (draft: HrCreationDraft) => {
    if (!draft.hr_agent_id) throw new Error(t('employees.hrRecovery.sourceMissing', 'The original employee could not be identified. Reload before retrying.'));
    return draft.hr_agent_id;
  };
  const retryMutation = useMutation({
    mutationFn: (draft: HrCreationDraft) => hrCreationApi.retry(sourceAgentId(draft), draft.blueprint_id),
    onSuccess: refresh,
    onError: (error: Error) => showAppToast(error.message, 'error'),
  });
  const abandonMutation = useMutation({
    mutationFn: (draft: HrCreationDraft) => hrCreationApi.abandon(sourceAgentId(draft), draft.blueprint_id),
    onSettled: refresh,
    onError: (error: Error) => showAppToast(error.message, 'error'),
  });
  const errorData = abandonMutation.error instanceof ApiError ? abandonMutation.error.data : null;
  const retirementRecovery = errorData !== null && typeof errorData === 'object' && 'code' in errorData
    && (errorData.code === 'agent_retirement_required' || errorData.code === 'agent_retirement_pending')
    && 'recovery' in errorData && errorData.recovery !== null && typeof errorData.recovery === 'object'
    ? errorData.recovery : null;

  if (draftsError) return (
    <section className="hr-recovery-panel" role="alert">
      {t('employees.hrRecovery.loadFailed', 'Could not load interrupted creations or pending cleanup.')}
      <button type="button" className="btn btn-secondary" onClick={() => void reloadDrafts()}>
        {t('common.retry', 'Retry')}
      </button>
    </section>
  );
  if (drafts.length === 0) return null;

  const abandon = async (draft: HrCreationDraft) => {
    if (draft.recovery?.cleanup_pending) {
      abandonMutation.mutate(draft);
      return;
    }
    const confirmed = await requestAppConfirm({
      title: t('employees.hrRecovery.removeTitle', 'Remove unfinished employee'),
      message: t(
        'employees.hrRecovery.removeConfirm',
        'Remove {{name}} and stop its unfinished provisioning? Audit history is preserved.',
        { name: draftName(draft) },
      ),
      confirmLabel: t('employees.hrRecovery.remove', 'Remove unfinished employee'),
      danger: true,
    });
    if (confirmed) abandonMutation.mutate(draft);
  };

  return (
    <section className="hr-recovery-panel" aria-label={t('employees.hrRecovery.title', 'Interrupted creations')}>
      <div className="hr-recovery-heading">
        <div>
          <span>{t('employees.hrRecovery.eyebrow', 'Creation recovery')}</span>
          <h2>{t('employees.hrRecovery.title', 'Interrupted creations')}</h2>
        </div>
        <p>{t('employees.hrRecovery.description', 'Resume the original work or recover provisioning directly, without restating the request.')}</p>
      </div>
      <div className="hr-recovery-list">
        {drafts.map((draft) => {
          const recovery = draft.recovery;
          const firstTaskRecovery = draft.draft_status === 'completed' && Boolean(draft.provisioning?.first_task);
          const failure = firstTaskRecovery
            ? t('employees.hrRecovery.firstTaskPending', 'The employee is ready; its first task needs recovery.')
            : draftFailure(draft);
          const retirementError = abandonMutation.variables?.blueprint_id === draft.blueprint_id
            ? retirementRecovery : null;
          return (
            <article className="hr-recovery-item" key={draft.blueprint_id} data-status={draft.draft_status}>
              <div>
                <div className="hr-recovery-title-row">
                  <h3>{draftName(draft)}</h3>
                  <span>{recovery?.cleanup_pending
                    ? t('employees.hrRecovery.cleanupStatus', 'File cleanup pending')
                    : blueprintStatusLabel(draft.draft_status, t)}</span>
                </div>
                {recovery?.cleanup_pending && (
                  <p>{t('employees.hrRecovery.cleanupPending', 'Removal is committed. Retry only the remaining file archival; the employee will not be removed again.')}</p>
                )}
                {failure && <p role={recovery?.requires_operator ? 'alert' : undefined}>{failure}</p>}
                {retirementError && <div role="alert">
                  <p>{t('employees.hrRecovery.retirementRequired', 'This employee has unresolved external work. Complete retirement before retrying removal of the unfinished creation. The employee and files are retained.')}</p>
                  {'can_manage_retirement' in retirementError && retirementError.can_manage_retirement === true
                    && isAdministratorRole(user?.role)
                    ? <Link to="/enterprise/digital-employees">{t('employees.hrRecovery.reviewRetirement', 'Review employee retirement')}</Link>
                    : <p>{t('employees.hrRecovery.retirementAdministrator', 'Ask a company administrator to review this employee’s retirement, then retry removal here.')}</p>}
                </div>}
                {recovery?.requires_operator && (
                  <small>{t('employees.hrRecovery.operatorRequired', 'Operator reconciliation is required before retry.')}</small>
                )}
              </div>
              <div className="hr-recovery-actions">
                {recovery?.can_resume && draft.session_id && draft.hr_agent_id && (
                  <Link to={`/agents/${encodeURIComponent(draft.hr_agent_id)}?session_id=${encodeURIComponent(draft.session_id)}#chat`}>
                    {t('employees.hrRecovery.resume', 'Resume original work')}
                  </Link>
                )}
                {recovery?.can_retry && (
                  <button
                    type="button"
                    className="btn btn-secondary"
                    disabled={retryMutation.isPending}
                    onClick={() => retryMutation.mutate(draft)}
                  >
                    {firstTaskRecovery
                      ? t('agent.chat.toolResults.retryFirstTask', 'Retry first task')
                      : t('employees.hrRecovery.retry', 'Retry provisioning')}
                  </button>
                )}
                {recovery?.can_abandon && (
                  <button
                    type="button"
                    className="btn btn-ghost hr-recovery-remove"
                    disabled={abandonMutation.isPending}
                    onClick={() => void abandon(draft)}
                  >
                    {recovery?.cleanup_pending
                      ? t('employees.hrRecovery.retryCleanup', 'Retry file cleanup')
                      : t('employees.hrRecovery.remove', 'Remove unfinished employee')}
                  </button>
                )}
              </div>
            </article>
          );
        })}
      </div>
    </section>
  );
}
