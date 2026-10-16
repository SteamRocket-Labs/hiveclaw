import { useEffect, useState } from 'react';
import { useTranslation } from 'react-i18next';
import { toolsApi, type SessionMcpConfiguration } from '../../api/domains/tools';
import { extensionsApi, type McpToolMode } from '../../api/domains/extensions';
import McpOAuthSettings from '../../components/McpOAuthSettings';

export default function InlineMcpConfigurationCard({ agentId, sessionId, questionId, question, answered, onAnswer }: {
  agentId: string; sessionId: string; questionId: string; question: string;
  answered?: boolean; onAnswer?: (itemId: string, text: string) => Promise<unknown>;
}) {
  const { t } = useTranslation();
  const [configuration, setConfiguration] = useState<SessionMcpConfiguration | null>(null);
  const [revision, setRevision] = useState(0);
  const [busy, setBusy] = useState(false);
  const [authorizing, setAuthorizing] = useState(false);
  const [loading, setLoading] = useState(false);
  const [editing, setEditing] = useState(false);
  const [submitted, setSubmitted] = useState(false);
  const [error, setError] = useState('');
  const [mode, setMode] = useState<McpToolMode>('auto');
  const interactive = !!onAnswer;
  const review = () => setRevision((value) => value + 1);
  useEffect(() => {
    if (answered || !interactive) return;
    let active = true; setLoading(true); setError('');
    void toolsApi.getSessionMcpConfiguration(agentId, sessionId, questionId).then((value) => {
      if (active) { setConfiguration(value); if (value.usage.default_tool_mode !== 'deny') setMode(value.usage.default_tool_mode as McpToolMode); }
    }).catch((failure: unknown) => { if (active) setError(failure instanceof Error ? failure.message : t('agent.configuration.loadFailed')); })
      .finally(() => { if (active) setLoading(false); });
    return () => { active = false; };
  }, [agentId, sessionId, questionId, answered, interactive, revision, t]);
  const resume = async (skip = false) => {
    if (!onAnswer || busy) return;
    setBusy(true); setError('');
    try {
      if (!skip && configuration?.state !== 'ready') {
        if (!configuration) return;
        setConfiguration(await toolsApi.confirmSessionMcpConfiguration(agentId, sessionId, questionId, { revision: configuration.revision }));
      }
      await onAnswer(questionId, skip ? 'Continue without changing configuration.' : 'Continue with the authorized MCP connection.');
      setSubmitted(true);
    } catch (failure) { setError(failure instanceof Error ? failure.message : t('agent.configuration.resumeFailed')); }
    finally { setBusy(false); }
  };
  if (answered || submitted || !onAnswer) return <p>{question} · {t('agent.configuration.recorded')}</p>;
  return <section className="card" aria-label={t('agent.mcpOAuth.workTitle')}>
    <p>{question}</p>
    {configuration && <>
      <p><strong>{configuration.target_agent_name}</strong> · {configuration.server.name}</p>
      <p>{t('agent.mcpOAuth.authorization', { status: t(`agent.mcpOAuth.states.${configuration.authorization}`) })}</p>
      <p>{configuration.usage.enabled ? t('agent.mcpOAuth.usageEnabled') : t('agent.mcpOAuth.usageMissing')}</p>
      {configuration.can_authorize && configuration.state !== 'ready' && (configuration.authorization !== 'configured' || editing)
        ? <McpOAuthSettings key={configuration.server.id} serverId={configuration.server.id} onReviewed={review} onBusyChange={setAuthorizing} />
        : configuration.authorization !== 'configured' && <p>{t('agent.mcpOAuth.adminRequired')}</p>}
      {configuration.can_authorize && configuration.authorization === 'configured' && !editing && configuration.state !== 'ready' && <button
        className="btn btn-secondary" type="button" disabled={busy || authorizing || loading} onClick={() => setEditing(true)}>{t('agent.mcpOAuth.reauthorize')}</button>}
      {(!configuration.usage.enabled || !configuration.usable_tool_count) && <div>
        <label>{t('agent.mcpOAuth.executionMode')} <select value={mode} disabled={busy || authorizing || loading} onChange={(event) => setMode(event.target.value as McpToolMode)}>
          <option value="approval">{t('agent.mcpOAuth.approval')}</option><option value="auto">{t('agent.mcpOAuth.auto')}</option>
        </select></label>
        <button className="btn btn-secondary" type="button" disabled={busy || authorizing || loading} onClick={async () => {
          setBusy(true); setError('');
          try { await extensionsApi.setAgentMcpAssignment(configuration.target_agent_id, configuration.server.id,
            { enabled: true, default_tool_mode: mode, always_load: configuration.usage.always_load }); review(); }
          catch (failure) { setError(failure instanceof Error ? failure.message : t('agent.mcpOAuth.grantFailed')); }
          finally { setBusy(false); }
        }}>{t('agent.mcpOAuth.grantUse')}</button>
      </div>}
      {configuration.usage.enabled && !configuration.usable_tool_count && <p>{t('agent.mcpOAuth.noTools')}</p>}
    </>}
    {error && <p role="alert">{error}</p>}
    {loading && <p>{t('common.loading')}</p>}
    <div className="mcp-configuration-actions">
      <button className="btn btn-secondary" type="button" disabled={busy || authorizing || loading} onClick={review}>{t('agent.configuration.review')}</button>
      {configuration && <button className="btn btn-primary" type="button" disabled={busy || authorizing || loading || configuration.authorization !== 'configured' || !configuration.usable_tool_count}
        onClick={() => void resume()}>{t('agent.configuration.continue')}</button>}
      {configuration?.state !== 'ready' && <button className="btn btn-secondary" type="button" disabled={busy || authorizing || loading}
        onClick={() => void resume(true)}>{t('agent.configuration.skip')}</button>}
    </div>
  </section>;
}
