import { useEffect, useId, useState } from 'react';
import { useTranslation } from 'react-i18next';
import { extensionsApi, type McpOAuthRegistration, type McpOAuthStatus } from '../api/domains/extensions';
import { ApiError } from '../api/core';
import './McpOAuthSettings.css';

export default function McpOAuthSettings({ serverId, onReviewed, onBusyChange }: { serverId: string; onReviewed?: () => void; onBusyChange?: (busy: boolean) => void }) {
  const { t } = useTranslation();
  const formId = useId();
  const [status, setStatus] = useState<McpOAuthStatus | null>(null);
  const [draft, setDraft] = useState<McpOAuthRegistration>({ authorization_endpoint: '', token_endpoint: '', client_id: '',
    redirect_uri: new URL('/api/enterprise/mcp/oauth/callback', window.location.origin).href, scope: '' });
  const [secret, setSecret] = useState('');
  const [clearSecret, setClearSecret] = useState(false);
  const [link, setLink] = useState('');
  const [attemptId, setAttemptId] = useState('');
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<'' | 'load' | 'start' | 'encryption'>('');
  useEffect(() => {
    let active = true;
    setBusy(true);
    void extensionsApi.getMcpOAuthStatus(serverId).then((value) => {
      if (!active) return;
      setStatus(value);
      const saved = value.registration;
      setDraft((current) => ({ authorization_endpoint: saved?.authorization_endpoint || current.authorization_endpoint,
        token_endpoint: saved?.token_endpoint || current.token_endpoint, client_id: saved?.client_id || current.client_id,
        redirect_uri: saved?.redirect_uri || current.redirect_uri, scope: saved?.scope || current.scope }));
    }).catch(() => { if (active) setError('load'); })
      .finally(() => { if (active) setBusy(false); });
    return () => { active = false; };
  }, [serverId]);
  const review = async () => {
    setBusy(true); setError('');
    try {
      const current = await extensionsApi.getMcpOAuthStatus(serverId);
      setStatus(current);
      if (current.attempt?.id !== attemptId || current.attempt?.outcome !== 'pending') setLink('');
      onReviewed?.();
    }
    catch { setError('load'); }
    finally { setBusy(false); }
  };
  return <section aria-label={t('agent.mcpOAuth.title')}>
    <p>{t('agent.mcpOAuth.companyScope')}</p>
    {status?.authorized_agents && (status.authorized_agents.length ? <details>
      <summary>{t('agent.mcpOAuth.authorizedAgents', { count: status.authorized_agents.length })}</summary>
      <ul>{status.authorized_agents.map((agent) => <li key={agent.id}>{agent.name}</li>)}</ul>
    </details> : <p>{t('agent.mcpOAuth.noAuthorizedAgents')}</p>)}
    {status && <p role="status">{t('agent.mcpOAuth.status', { status: t(`agent.mcpOAuth.states.${status.attempt?.outcome || status.auth_status}`, status.attempt?.outcome || status.auth_status) })}</p>}
    {error && <p role="alert">{error === 'load' ? t('agent.mcpOAuth.loadFailed') : error === 'encryption' ? t('agent.mcpOAuth.encryptionUnavailable') : t('agent.mcpOAuth.startFailed')}</p>}
    <form onSubmit={async (event) => {
      event.preventDefault(); if (busy) return;
      setBusy(true); onBusyChange?.(true); setError(''); setLink('');
      try {
        const result = await extensionsApi.startMcpOAuth(serverId, { ...draft,
          ...(clearSecret ? { client_secret: '' } : secret ? { client_secret: secret } : {}) });
        if (!['https:', 'http:'].includes(new URL(result.authorization_url).protocol)) throw new Error('Invalid authorization URL');
        setSecret(''); setClearSecret(false); setAttemptId(result.attempt_id); setLink(result.authorization_url);
      } catch (failure) {
        const code = failure instanceof ApiError && failure.data && typeof failure.data === 'object' && 'code' in failure.data ? failure.data.code : null;
        setError(code === 'oauth_encryption_unavailable' ? 'encryption' : 'start');
      }
      finally { setBusy(false); onBusyChange?.(false); }
    }}>
      <fieldset disabled={busy} className="mcp-oauth-fields">
        {(['authorization_endpoint', 'token_endpoint', 'client_id', 'redirect_uri', 'scope'] as const).map((key) => <div key={key}>
          <label className="mcp-oauth-label" htmlFor={`${formId}-${key}`}>{t(`agent.mcpOAuth.fields.${key}`)}</label>
          <input className="input" id={`${formId}-${key}`} type={key.includes('endpoint') || key === 'redirect_uri' ? 'url' : 'text'}
            required={key !== 'scope'} value={draft[key] || ''} onChange={(event) => setDraft({ ...draft, [key]: event.target.value })} />
        </div>)}
        <div>
          <label className="mcp-oauth-label" htmlFor={`${formId}-secret`}>{t('agent.mcpOAuth.fields.client_secret')}</label>
          <input className="input" id={`${formId}-secret`} type="password" autoComplete="new-password" value={secret}
            disabled={clearSecret} onChange={(event) => setSecret(event.target.value)} />
          {status?.client_secret_configured && <>
            <p>{t('agent.mcpOAuth.keepSecret')}</p>
            <label><input type="checkbox" checked={clearSecret} onChange={(event) => setClearSecret(event.target.checked)} />{t('agent.mcpOAuth.clearSecret')}</label>
          </>}
        </div>
        <button className="btn btn-secondary" type="submit">{busy ? t('common.loading') : t('agent.mcpOAuth.start')}</button>
      </fieldset>
    </form>
    {link && <p><a className="btn btn-primary" href={link} target="_blank" rel="noopener noreferrer">{t('agent.mcpOAuth.open')}</a></p>}
    <p>{t('agent.mcpOAuth.return')}</p>
    <button className="btn btn-secondary" type="button" disabled={busy} onClick={() => void review()}>{t('agent.mcpOAuth.review')}</button>
  </section>;
}
