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

// Own-key-only dictionaries: a crafted key like `constructor` must fall through
// to the generic line instead of reaching anything on the prototype chain.
const FAILURE_LINE_KEYS: Record<string, string> = {
  missing_confirmation_evidence: 'employees.hrRecovery.failureMissingConfirmation',
  runtime_task_failed: 'employees.hrRecovery.failureProvisionFailed',
  provisioning_failed: 'employees.hrRecovery.failureProvisionFailed',
  missing_creation_model: 'employees.hrRecovery.failureModelMissing',
  capability_denied: 'employees.hrRecovery.failureCapabilityDenied',
  approval_required: 'employees.hrRecovery.failureApprovalRequired',
  runtime_task_needs_reconciliation: 'employees.hrRecovery.failureNeedsOperator',
  runtime_task_draft_divergence: 'employees.hrRecovery.failureNeedsOperator',
  cancellation_reconciliation_required: 'employees.hrRecovery.failureNeedsOperator',
};

const ACTION_ERROR_KEYS: Record<string, string> = {
  agent_retirement_administrator_required: 'employees.hrRecovery.retirementAdministrator',
  invalid_status: 'employees.hrRecovery.errorStateChanged',
  provisioning_task_missing: 'employees.hrRecovery.errorStateChanged',
  retry_required: 'employees.hrRecovery.errorStateChanged',
  not_found: 'employees.hrRecovery.errorMissing',
  confirmation_missing: 'employees.hrRecovery.errorConfirmationMissing',
  // A direct-creation request whose authorization evidence expired: the
  // member's remedy is to ask again, not to keep retrying this draft.
  missing_confirmation_evidence: 'agent.chat.toolResults.creationExpired',
  reconciliation_required: 'employees.hrRecovery.errorReconciliationRequired',
  invalid_task_status: 'employees.hrRecovery.errorStateChanged',
  // Capability/identity refusals the retry handler can raise: the sentence
  // keeps telling the member what to do (often: ask an administrator) instead
  // of collapsing into the generic retry line.
  capability_denied: 'employees.hrRecovery.failureCapabilityDenied',
  approval_required: 'employees.hrRecovery.failureApprovalRequired',
  capability_unavailable: 'employees.hrRecovery.errorCapabilityUnavailable',
  source_execute_denied: 'employees.hrRecovery.errorSourceExecuteDenied',
  direct_human_input_required: 'employees.hrRecovery.errorDirectInputRequired',
};

function failureLine(draft: HrCreationDraft, t: (key: string, fallback: string) => string): string | null {
  const failure = draft.failure;
  const code = failure && typeof failure === 'object' && typeof (failure as Record<string, unknown>).code === 'string'
    ? (failure as Record<string, unknown>).code as string : '';
  if (code && Object.prototype.hasOwnProperty.call(FAILURE_LINE_KEYS, code)) {
    return t(FAILURE_LINE_KEYS[code], FAILURE_LINE_KEYS[code]);
  }
  const legacyMessage = failure && typeof failure === 'object' && typeof (failure as Record<string, unknown>).message === 'string'
    ? (failure as Record<string, unknown>).message as string : '';
  const legacyReason = typeof draft.recovery?.reason === 'string' ? draft.recovery.reason : '';
  // A failure line exists only for a draft that actually failed: an unmapped
  // code or a codeless legacy message/reason keeps one stable Chinese line
  // (never the stored English sentence), while a draft still awaiting
  // confirmation or provisioning shows no failure line at all.
  if (!code && !legacyMessage.trim() && !legacyReason.trim()) return null;
  return t('employees.hrRecovery.failureGeneric', 'This creation did not finish. Retry it or abandon it.');
}

/** One plain Chinese sentence for a refused abandon/retry, mapped from the
 * server's error code. Raw server text never reaches the member. */
function actionErrorText(error: Error, t: (key: string, fallback: string) => string): string {
  if (!(error instanceof ApiError)) return error.message;
  const data = error.data;
  const code = data !== null && typeof data === 'object' && !Array.isArray(data)
    ? String((data as Record<string, unknown>).code || (data as Record<string, unknown>).error || '')
    : '';
  if (code && Object.prototype.hasOwnProperty.call(ACTION_ERROR_KEYS, code)) {
    return t(ACTION_ERROR_KEYS[code], ACTION_ERROR_KEYS[code]);
  }
  return t('employees.hrRecovery.errorGeneric', 'The action did not complete. Please try again.');
}

function draftName(draft: HrCreationDraft, t: (key: string, fallback: string) => string): string {
  const name = draft.blueprint?.name;
  return typeof name === 'string' && name.trim()
    ? name.trim()
    : t('employees.hrRecovery.unnamedDraft', 'Unfinished digital employee');
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
    onError: (error: Error) => showAppToast(actionErrorText(error, t), 'error'),
  });
  const abandonMutation = useMutation({
    mutationFn: (draft: HrCreationDraft) => hrCreationApi.abandon(sourceAgentId(draft), draft.blueprint_id),
    onSettled: refresh,
    onError: (error: Error) => showAppToast(actionErrorText(error, t), 'error'),
  });
  const errorData = abandonMutation.error instanceof ApiError ? abandonMutation.error.data : null;
  const retirementRecovery = errorData !== null && typeof errorData === 'object' && 'code' in errorData
    && errorData.code === 'agent_retirement_administrator_required'
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
    const confirmed = await requestAppConfirm({
      title: t('employees.hrRecovery.removeTitle', 'Abandon unfinished creation'),
      message: t(
        'employees.hrRecovery.removeConfirm',
        'Abandon the unfinished creation of {{name}}? Its employee, files and history are kept and stay until a company admin deactivates them.',
        { name: draftName(draft, t) },
      ),
      confirmLabel: t('employees.hrRecovery.remove', 'Abandon unfinished creation'),
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
            : failureLine(draft, t);
          const retirementError = abandonMutation.variables?.blueprint_id === draft.blueprint_id
            ? retirementRecovery : null;
          return (
            <article className="hr-recovery-item" key={draft.blueprint_id} data-status={draft.draft_status}>
              <div>
                <div className="hr-recovery-title-row">
                  <h3>{draftName(draft, t)}</h3>
                  <span>{blueprintStatusLabel(draft.draft_status, t)}</span>
                </div>
                {failure && <p role={recovery?.requires_operator ? 'alert' : undefined}>{failure}</p>}
                {retirementError && <div role="alert">
                  <p>{t('employees.hrRecovery.retirementAdministrator', 'This creation already produced a digital employee. A company administrator deactivates it on the digital-employees page; then this draft can be abandoned.')}</p>
                  {'can_manage_retirement' in retirementError && retirementError.can_manage_retirement === true
                    && isAdministratorRole(user?.role)
                    && <Link to="/enterprise/digital-employees">{t('employees.hrRecovery.reviewRetirement', 'Deactivate it on the digital-employees page')}</Link>}
                </div>}
                {recovery?.requires_operator && (
                  <small>{t('employees.hrRecovery.operatorRequired', 'An operator needs to review this before retry.')}</small>
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
                    {t('employees.hrRecovery.remove', 'Abandon unfinished creation')}
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
