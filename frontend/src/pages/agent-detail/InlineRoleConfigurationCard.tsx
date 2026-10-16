import { useEffect, useState } from 'react';
import { useTranslation } from 'react-i18next';
import { useQueryClient } from '@tanstack/react-query';
import EmployeeRoleEditor from '../../components/EmployeeRoleEditor';
import { toolsApi, type SessionRoleConfiguration } from '../../api/domains/tools';

export default function InlineRoleConfigurationCard({ agentId, sessionId, questionId, question, answered, onAnswer }: {
  agentId: string; sessionId: string; questionId: string; question: string;
  answered?: boolean; onAnswer?: (itemId: string, text: string) => Promise<unknown>;
}) {
  const { t } = useTranslation();
  const queryClient = useQueryClient();
  const [configuration, setConfiguration] = useState<SessionRoleConfiguration | null>(null);
  const [revision, setRevision] = useState(0);
  const [loading, setLoading] = useState(false);
  const [busy, setBusy] = useState(false);
  const [editingBusy, setEditingBusy] = useState(false);
  const [submitted, setSubmitted] = useState(false);
  const [error, setError] = useState('');
  const interactive = !!onAnswer && !answered && !submitted;
  useEffect(() => {
    if (!interactive) return;
    let active = true; setLoading(true); setError('');
    void toolsApi.getSessionRoleConfiguration(agentId, sessionId, questionId)
      .then((value) => { if (active) setConfiguration(value); })
      .catch((failure) => { if (active) setError(failure instanceof Error ? failure.message : t('agent.roleSettings.failed')); })
      .finally(() => { if (active) setLoading(false); });
    return () => { active = false; };
  }, [agentId, sessionId, questionId, interactive, revision, t]);
  const resume = async (skip = false) => {
    if (!onAnswer || busy) return;
    setBusy(true); setError('');
    try {
      await onAnswer(questionId, skip ? 'Continue without changing configuration.' : 'Continue with the reviewed responsibilities.');
      setSubmitted(true);
    } catch (failure) { setError(failure instanceof Error ? failure.message : t('agent.configuration.resumeFailed')); }
    finally { setBusy(false); }
  };
  if (!interactive) return <p>{question} · {t('agent.configuration.recorded')}</p>;
  return <section className="card" aria-label={t('agent.roleSettings.title')}>
    <p>{question}</p>
    {configuration && <>
      <p><strong>{configuration.target_agent_name}</strong></p>
      {configuration.state === 'ready' ? <>
        <p style={{ whiteSpace: 'pre-wrap' }}>{configuration.role_description}</p>
        <p role="status">{t('agent.roleSettings.ready')}</p>
      </> : <EmployeeRoleEditor key={`${questionId}:${configuration.target_agent_id}`} agentId={configuration.target_agent_id}
        value={configuration} saveLabel={t('agent.roleSettings.saveAndReview')} onBusyChange={setEditingBusy}
        onSaved={async (saved) => {
          setConfiguration(await toolsApi.confirmSessionRoleConfiguration(agentId, sessionId, questionId, saved.role_revision));
          queryClient.setQueryData(['agent', configuration.target_agent_id], (current: any) => current ? { ...current, ...saved } : current);
        }} />}
    </>}
    {error && <p role="alert">{error}</p>}
    {loading && <p role="status">{t('common.loading')}</p>}
    <div style={{ display: 'flex', flexWrap: 'wrap', gap: 'var(--space-2)', marginTop: 'var(--space-3)' }}>
      <button className="btn btn-secondary" type="button" disabled={busy || editingBusy || loading} onClick={() => setRevision((current) => current + 1)}>{t('agent.configuration.review')}</button>
      <button className="btn btn-primary" type="button" disabled={busy || editingBusy || loading || configuration?.state !== 'ready'} onClick={() => void resume()}>{t('agent.configuration.continue')}</button>
      <button className="btn btn-secondary" type="button" disabled={busy || editingBusy || loading} onClick={() => void resume(true)}>{t('agent.configuration.skip')}</button>
    </div>
  </section>;
}
