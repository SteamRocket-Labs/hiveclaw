import { useEffect, useRef, useState } from 'react';
import { useTranslation } from 'react-i18next';
import { toolsApi, type SessionMcpConfiguration } from '../../api/domains/tools';
import { extensionsApi } from '../../api/domains/extensions';
import McpOAuthSettings from '../../components/McpOAuthSettings';

export default function InlineMcpConfigurationCard({ agentId, sessionId, questionId, answered, onAnswer }: {
  agentId: string; sessionId: string; questionId: string;
  answered?: boolean; onAnswer?: (itemId: string, text: string) => Promise<unknown>;
}) {
  const { t } = useTranslation();
  const [configuration, setConfiguration] = useState<SessionMcpConfiguration | null>(null);
  const [revision, setRevision] = useState(0);
  const [busy, setBusy] = useState(false);
  const [authorizing, setAuthorizing] = useState(false);
  const [loading, setLoading] = useState(false);
  const [submitted, setSubmitted] = useState(false);
  const [error, setError] = useState('');
  const resuming = useRef(false);
  const autoContinued = useRef(false);
  const creationId = useRef(crypto.randomUUID());
  const interactive = !!onAnswer;
  const review = () => setRevision((value) => value + 1);
  useEffect(() => {
    if (answered || !interactive) return;
    let active = true; setLoading(true); setError('');
    void toolsApi.getSessionMcpConfiguration(agentId, sessionId, questionId).then((value) => {
      if (active) { setConfiguration(value); if (value.answered) setSubmitted(true); }
    }).catch(() => { if (active) setError(t('agent.configuration.loadFailed')); })
      .finally(() => { if (active) setLoading(false); });
    return () => { active = false; };
  }, [agentId, sessionId, questionId, answered, interactive, revision, t]);
  const resume = async (skip = false) => {
    if (!onAnswer || resuming.current) return;
    resuming.current = true;
    setBusy(true); setError('');
    try {
      if (!skip && configuration?.state !== 'ready') {
        if (!configuration) return;
        setConfiguration(await toolsApi.confirmSessionMcpConfiguration(agentId, sessionId, questionId, { revision: configuration.revision, ...(configuration.account ? { account_id: configuration.account.id } : {}) }));
      }
      await onAnswer(questionId, skip ? 'Continue without changing configuration.' : 'Continue with the authorized MCP connection.');
      setSubmitted(true);
    } catch {
      setError(t('agent.configuration.resumeFailed')); resuming.current = false;
      review();
    }
    finally { setBusy(false); }
  };
  useEffect(() => {
    if (!answered && !submitted && !configuration?.answered && !autoContinued.current && configuration?.authorization === 'configured' && configuration.usable_tool_count) {
      autoContinued.current = true;
      void resume();
    }
  }, [configuration, answered, submitted]);
  useEffect(() => {
    if (answered || submitted || !interactive) return;
    const visible = () => { if (document.visibilityState === 'visible') review(); };
    window.addEventListener('focus', review);
    document.addEventListener('visibilitychange', visible);
    return () => {
      window.removeEventListener('focus', review);
      document.removeEventListener('visibilitychange', visible);
    };
  }, [answered, submitted, interactive]);
  if (answered || submitted) return <p>{t('agent.mcpConnect.continued')}</p>;
  if (!onAnswer) return <p>{t('agent.mcpConnect.title')}</p>;
  return <section className="card" aria-label={t('agent.mcpConnect.title')}>
    {configuration && <>
      <p>{t('agent.mcpConnect.reason', { name: configuration.server.name })}</p>
      <p>{t('agent.mcpConnect.scope')}</p>
      {!configuration.usage.enabled || !(configuration.permitted_tool_count ?? configuration.usable_tool_count)
        ? <p role="alert">{t('agent.mcpConnect.unavailable')}</p>
        : configuration.authorization !== 'configured' && (configuration.account?.owner_user_id
          ? <McpOAuthSettings key={configuration.account.id} serverId={configuration.account.id} personal inline
            disabled={busy || authorizing || loading} onReviewed={review} onBusyChange={setAuthorizing} />
          : <button className="btn btn-primary" type="button" disabled={busy || loading} onClick={async () => {
            setBusy(true); setError('');
            try {
              await extensionsApi.createMcpAccount(configuration.target_agent_id, configuration.server.id,
                { id: creationId.current, name: configuration.server.name });
              review();
            } catch { setError(t('agent.mcpConnect.failed')); }
            finally { setBusy(false); }
          }}>{t('agent.mcpConnect.connect')}</button>)}
    </>}
    {error && <p role="alert">{error}</p>}
    {loading && <p>{t('common.loading')}</p>}
    <div className="mcp-configuration-actions">
      <button className="btn btn-secondary" type="button" disabled={busy || authorizing || loading}
        onClick={() => { autoContinued.current = false; review(); }}>{t('agent.mcpConnect.recheck')}</button>
      {configuration?.state !== 'ready' && <button className="btn btn-secondary" type="button" disabled={busy || authorizing || loading}
        onClick={() => void resume(true)}>{t('agent.configuration.skip')}</button>}
    </div>
  </section>;
}
