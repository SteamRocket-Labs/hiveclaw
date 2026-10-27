import { useCallback, useEffect, useState } from 'react';
import { useTranslation } from 'react-i18next';
import { toolsApi, type SessionChannelConfiguration } from '../../api/domains/tools';
import ChannelConfig from '../../components/ChannelConfig';
import TenantFeishuSettings from '../../components/TenantFeishuSettings';
import { useAuthStore } from '../../stores';

export default function InlineChannelConfigurationCard({ agentId, sessionId, questionId, channelType, question, answered, onAnswer }: {
  agentId: string; sessionId: string; questionId: string; channelType: string; question: string;
  answered?: boolean; onAnswer?: (itemId: string, text: string) => Promise<unknown>;
}) {
  const { t } = useTranslation();
  const user = useAuthStore((state) => state.user);
  const [configuration, setConfiguration] = useState<SessionChannelConfiguration | null>(null);
  const [loading, setLoading] = useState(false);
  const [busy, setBusy] = useState(false);
  const [saving, setSaving] = useState(false);
  const [submitted, setSubmitted] = useState(false);
  const [editing, setEditing] = useState(false);
  const [error, setError] = useState('');
  const [revision, setRevision] = useState(0);
  const review = useCallback(() => setRevision((value) => value + 1), []);
  const interactive = !!onAnswer;
  const company = configuration?.configuration?.scope === 'company';
  const companyTenant = configuration?.target_tenant_id;
  const canManageCompany = !!user && ['org_admin', 'platform_admin'].includes(user.role);
  useEffect(() => {
    if (answered || !interactive) return;
    let active = true;
    setLoading(true);
    setError('');
    void toolsApi.getSessionChannelConfiguration(agentId, sessionId, questionId).then((result) => {
      if (active) setConfiguration(result);
    }).catch((failure: unknown) => {
      if (active) setError(failure instanceof Error ? failure.message : t('agent.configuration.loadFailed', 'Could not load configuration.'));
    }).finally(() => { if (active) setLoading(false); });
    return () => { active = false; };
  }, [agentId, sessionId, questionId, answered, interactive, revision, t]);

  const resume = async (skip = false) => {
    if (!onAnswer || busy) return;
    setBusy(true);
    setError('');
    try {
      if (!skip && configuration?.state !== 'ready') {
        const snapshot = configuration?.configuration;
        if (!snapshot) throw new Error(t('agent.configuration.channelMissing', 'Configure the channel, then review its saved settings.'));
        const confirmed = await toolsApi.confirmSessionChannelConfiguration(agentId, sessionId, questionId,
          { config_id: snapshot.id, updated_at: snapshot.updated_at });
        setConfiguration(confirmed);
      }
      await onAnswer(questionId, skip ? 'Continue without changing configuration.' : 'Continue with the configured channel.');
      setSubmitted(true);
    } catch (failure) {
      setError(failure instanceof Error ? failure.message : t('agent.configuration.resumeFailed', 'Could not continue the work. Retry continuation.'));
    } finally { setBusy(false); }
  };
  if (answered || submitted || !onAnswer) return <p>{question} · {t('agent.configuration.recorded', 'Configuration request recorded')}</p>;
  return <section className="card" aria-label={t('agent.configuration.channelTitle', 'Configure channel for this work')}>
    <p>{question}</p>
    {configuration && <p><strong>{configuration.target_agent_name}</strong> · {configuration.configuration?.scope === 'company'
      ? t('agent.configuration.companyChannel', 'Existing company channel') : t('agent.configuration.employeeChannel', 'Employee channel')}</p>}
    <p>{t('agent.configuration.channelPrivate', 'Channel credentials stay in connection settings. Configuration readback does not verify external delivery or grant additional permissions.')}</p>
    {error && <p role="alert">{error}</p>}
    {loading && <p>{t('common.loading')}</p>}
    {configuration?.state === 'waiting' && company && !canManageCompany && <p>{t('agent.configuration.companyEditAccess',
      'A company administrator can edit this connection. You can continue with the current configuration.')}</p>}
    {configuration?.state === 'waiting' && company && canManageCompany && !companyTenant && <p>{t('agent.configuration.companyScopeMissing',
      'Review the current configuration to reload its company before editing.')}</p>}
    {!loading && configuration?.state === 'waiting' && (!configuration.configuration || editing) && (company
      ? companyTenant && canManageCompany && <TenantFeishuSettings selectedTenantId={companyTenant} onConfigured={review} onBusyChange={setSaving} />
      : <ChannelConfig key={configuration.target_agent_id}
        mode="edit" agentId={configuration.target_agent_id} channelType={channelType} canManage onConfigured={review} onBusyChange={setSaving} />)}
    <div className="channel-config-actions">
      <button className="btn btn-secondary" disabled={busy || saving || loading} onClick={review}>{t('agent.configuration.review', 'Review current configuration')}</button>
      {configuration?.state === 'waiting' && configuration.configuration && !editing && (!company || canManageCompany) && <button className="btn btn-secondary"
        disabled={busy || saving || loading || (company && !companyTenant)} onClick={() => setEditing(true)}>{t('agent.configuration.editChannel', 'Edit channel configuration')}</button>}
      {configuration && <button className="btn btn-primary" disabled={busy || saving || loading || !configuration.configuration} onClick={() => void resume()}>
        {configuration.state === 'ready' ? t('agent.configuration.continue', 'Continue original work') : t('agent.configuration.useChannel', 'Use this configuration and continue')}
      </button>}
      {configuration?.state !== 'ready' && <button className="btn btn-secondary" disabled={busy || saving || loading}
        onClick={() => void resume(true)}>{t('agent.configuration.skip', 'Continue without configuration')}</button>}
    </div>
  </section>;
}
