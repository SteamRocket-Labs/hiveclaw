import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { useTranslation } from 'react-i18next';
import { Link } from 'react-router-dom';

import { agentApi } from '../../api/domains/agents';
import { hrCreationApi, type HrCreationDraft } from '../../api/domains/hrCreation';
import { requestAppConfirm, showAppToast } from '../../components/AppDialogs';

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
  const { data: hrAgent } = useQuery({
    queryKey: ['hr-agent'],
    queryFn: () => agentApi.getHrAgent(),
    retry: 1,
  });
  const queryKey = ['hr-recoverable-drafts', hrAgent?.id] as const;
  const { data: drafts = [], error: draftsError, refetch: reloadDrafts } = useQuery({
    queryKey,
    queryFn: () => hrCreationApi.listRecoverable(hrAgent!.id),
    enabled: Boolean(hrAgent?.id),
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
  const retryMutation = useMutation({
    mutationFn: (draftId: string) => hrCreationApi.retry(hrAgent!.id, draftId),
    onSuccess: refresh,
    onError: (error: Error) => showAppToast(error.message, 'error'),
  });
  const abandonMutation = useMutation({
    mutationFn: (draftId: string) => hrCreationApi.abandon(hrAgent!.id, draftId),
    onSettled: refresh,
    onError: (error: Error) => showAppToast(error.message, 'error'),
  });

  if (draftsError) return (
    <section className="hr-recovery-panel" role="alert">
      {t('employees.hrRecovery.loadFailed', 'Could not load interrupted creations or pending cleanup.')}
      <button type="button" className="btn btn-secondary" onClick={() => void reloadDrafts()}>
        {t('common.retry', 'Retry')}
      </button>
    </section>
  );
  if (!hrAgent?.id || drafts.length === 0) return null;

  const abandon = async (draft: HrCreationDraft) => {
    if (draft.recovery?.cleanup_pending) {
      abandonMutation.mutate(draft.blueprint_id);
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
    if (confirmed) abandonMutation.mutate(draft.blueprint_id);
  };

  return (
    <section className="hr-recovery-panel" aria-label={t('employees.hrRecovery.title', 'Interrupted creations')}>
      <div className="hr-recovery-heading">
        <div>
          <span>{t('employees.hrRecovery.eyebrow', 'HR recovery')}</span>
          <h2>{t('employees.hrRecovery.title', 'Interrupted creations')}</h2>
        </div>
        <p>{t('employees.hrRecovery.description', 'Resume the original HR session or recover provisioning directly—no model restatement required.')}</p>
      </div>
      <div className="hr-recovery-list">
        {drafts.map((draft) => {
          const recovery = draft.recovery;
          const failure = draftFailure(draft);
          return (
            <article className="hr-recovery-item" key={draft.blueprint_id} data-status={draft.draft_status}>
              <div>
                <div className="hr-recovery-title-row">
                  <h3>{draftName(draft)}</h3>
                  <span>{recovery?.cleanup_pending
                    ? t('employees.hrRecovery.cleanupStatus', 'File cleanup pending')
                    : draft.draft_status.replace(/_/g, ' ')}</span>
                </div>
                {recovery?.cleanup_pending && (
                  <p>{t('employees.hrRecovery.cleanupPending', 'Removal is committed. Retry only the remaining file archival; the employee will not be removed again.')}</p>
                )}
                {failure && <p role={recovery?.requires_operator ? 'alert' : undefined}>{failure}</p>}
                {recovery?.requires_operator && (
                  <small>{t('employees.hrRecovery.operatorRequired', 'Operator reconciliation is required before retry.')}</small>
                )}
              </div>
              <div className="hr-recovery-actions">
                {recovery?.can_resume && draft.session_id && (
                  <Link to={`/agents/${hrAgent.id}?session_id=${encodeURIComponent(draft.session_id)}#chat`}>
                    {t('employees.hrRecovery.resume', 'Resume HR session')}
                  </Link>
                )}
                {recovery?.can_retry && (
                  <button
                    type="button"
                    className="btn btn-secondary"
                    disabled={retryMutation.isPending}
                    onClick={() => retryMutation.mutate(draft.blueprint_id)}
                  >
                    {t('employees.hrRecovery.retry', 'Retry provisioning')}
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
