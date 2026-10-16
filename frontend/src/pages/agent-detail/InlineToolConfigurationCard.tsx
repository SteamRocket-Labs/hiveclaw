import { useEffect, useState } from 'react';
import { useTranslation } from 'react-i18next';
import { toolsApi, type SessionToolConfiguration } from '../../api/domains/tools';
import ToolConfigurationForm from './ToolConfigurationForm';
import ToolAccounts from './ToolAccounts';

export default function InlineToolConfigurationCard({ agentId, sessionId, questionId, question, answered, onAnswer }: {
  agentId: string;
  sessionId: string;
  questionId: string;
  question: string;
  answered?: boolean;
  onAnswer?: (itemId: string, text: string) => Promise<unknown>;
}) {
  const { t } = useTranslation();
  const [configuration, setConfiguration] = useState<SessionToolConfiguration | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState('');
  const [continuing, setContinuing] = useState(false);
  const [saving, setSaving] = useState(false);
  const [accountDirty, setAccountDirty] = useState(false);
  const [submitted, setSubmitted] = useState(false);
  const [reload, setReload] = useState(0);
  const interactive = !!onAnswer;
  useEffect(() => {
    if (answered || !onAnswer) return;
    let active = true;
    setLoading(true);
    setError('');
    void toolsApi.getSessionToolConfiguration(agentId, sessionId, questionId).then((result) => {
      if (active) { setConfiguration(result); setAccountDirty(false); }
    }).catch((failure: unknown) => {
      if (active) setError(failure instanceof Error ? failure.message : t('agent.configuration.loadFailed', 'Could not load configuration.'));
    }).finally(() => { if (active) setLoading(false); });
    return () => { active = false; };
  }, [agentId, sessionId, questionId, answered, interactive, reload, t]);

  const resume = async (saved: boolean) => {
    if (!onAnswer || continuing) return;
    setContinuing(true);
    setError('');
    try {
      await onAnswer(questionId, saved ? 'Continue after saving configuration.' : 'Continue without changing configuration.');
      setSubmitted(true);
    } catch (failure) {
      setError(failure instanceof Error ? failure.message : t('agent.configuration.resumeFailed', 'Could not continue the work. Retry continuation.'));
    } finally {
      setContinuing(false);
    }
  };
  if (answered || submitted || !onAnswer) return <p>{question} · {t('agent.configuration.recorded', 'Configuration request recorded')}</p>;
  return <section className="card" aria-label={t('agent.configuration.title', 'Configure for this work')}>
    <p>{question}</p>
    {configuration && <p><strong>{configuration.target_agent_name}</strong> · {configuration.tool.display_name}</p>}
    <p>{t('agent.configuration.private', 'Save account settings here. Credentials are not sent to the conversation.')}</p>
    {loading && <p>{t('common.loading')}</p>}
    {error && <p role="alert">{error}</p>}
    {!loading && !configuration && <button className="btn btn-secondary" onClick={() => setReload((value) => value + 1)}>{t('common.retry', 'Retry')}</button>}
    {!loading && configuration && (configuration.tool.name === 'send_email' || configuration.tool.type === 'custom_api') && <ToolAccounts key={`${configuration.target_agent_id}:${configuration.tool.id}`}
      agentId={configuration.target_agent_id} tool={configuration.tool} canManageCompany={!!(configuration.can_manage_company_mailbox || configuration.can_manage_company_api_account)}
      initialAccountId={(configuration.tool.type === 'custom_api' ? configuration.api_account_selection : configuration.mailbox_selection)?.account_id} onSelectionChange={() => setAccountDirty(true)}
      onBusyChange={setSaving} onSaveCompany={async (config) => {
        const result = await toolsApi.saveSessionToolConfiguration(agentId, sessionId, questionId, config);
        setConfiguration({ ...configuration, ...result });
      }} onConfirm={async (account) => {
        const result = await (configuration.tool.type === 'custom_api' ? toolsApi.confirmSessionApiAccount : toolsApi.confirmSessionMailbox)(agentId, sessionId, questionId, account);
        setConfiguration({ ...configuration, ...result });
        setAccountDirty(false);
      }} />}
    {!loading && configuration?.tool.name !== 'send_email' && configuration?.tool.type !== 'custom_api' && configuration?.state === 'waiting' && <ToolConfigurationForm key={questionId} tool={configuration.tool}
      submitLabel={t('agent.configuration.saveContinue', 'Save and continue')} onSave={async (config) => {
        setSaving(true);
        try {
          const result = await toolsApi.saveSessionToolConfiguration(agentId, sessionId, questionId, config);
          setConfiguration(result);
          await resume(true);
        } finally { setSaving(false); }
      }} />}
    {configuration?.state === 'saved' && <>
      <p>{t('agent.configuration.saved', 'Configuration saved. Connection will be verified when the employee uses it.')}</p>
      <button className="btn btn-primary" disabled={continuing || saving || loading || accountDirty} onClick={() => void resume(true)}>{t('agent.configuration.continue', 'Continue original work')}</button>
    </>}
    {configuration?.state !== 'saved' && <button className="btn btn-secondary" disabled={saving || continuing || loading}
      onClick={() => void resume(false)}>{t('agent.configuration.skip', 'Continue without configuration')}</button>}
  </section>;
}
