import { useEffect, useId, useRef, useState } from 'react';
import { useQuery } from '@tanstack/react-query';
import { Link } from 'react-router-dom';
import { useTranslation } from 'react-i18next';
import { channelApi, type CompanyChannelChallenge, type CompanyChannelProvider, type TenantChannelScope } from '../api/domains/channels';
import { useCompanyChannelScope } from './useCompanyChannelScope';
import './TenantFeishuSettings.css';

export default function TenantCompanyIdentity({ onNavigate }: { onNavigate?: () => void }) {
  const current = useCompanyChannelScope();
  const { t } = useTranslation();
  const [open, setOpen] = useState(false);
  const [provider, setProvider] = useState<CompanyChannelProvider>('dingtalk');
  const id = useId();
  if (!current.user || !current.tenant) return null;
  return <details className="tenant-feishu-settings" open={open} onToggle={(event) => setOpen(event.currentTarget.open)}>
    <summary>{t('companyMessaging.memberTitle')}</summary>
    {open && <>
      <label htmlFor={`${id}-provider`}>{t('companyDirectory.provider')}</label>
      <select id={`${id}-provider`} className="form-input" value={provider} onChange={(event) => setProvider(event.target.value as CompanyChannelProvider)}>
        <option value="dingtalk">{t('companyDirectory.dingtalk')}</option><option value="wecom">{t('companyDirectory.wecom')}</option>
      </select>
      <MemberIdentity key={`${current.key}:${provider}`} scope={current.scope} provider={provider} onNavigate={onNavigate} />
    </>}
  </details>;
}

function MemberIdentity({ scope, provider, onNavigate }: { scope: TenantChannelScope; provider: CompanyChannelProvider; onNavigate?: () => void }) {
  const { t } = useTranslation();
  const id = useId();
  const live = useRef(true);
  useEffect(() => { live.current = true; return () => { live.current = false; }; }, []);
  const [challenge, setChallenge] = useState<CompanyChannelChallenge | null>(null);
  const [busy, setBusy] = useState(false);
  const [unknown, setUnknown] = useState(false);
  const query = useQuery({ queryKey: ['company-channel-identity', provider, scope.expected_tenant_id, scope.expected_user_id], retry: false,
    queryFn: async ({ signal }) => {
      const value = await channelApi.companyIdentity(provider, scope, signal);
      if (value.tenant_id !== scope.expected_tenant_id || value.user_id !== scope.expected_user_id || value.provider !== provider) throw new Error('Identity scope changed');
      return value;
    } });
  async function refresh() {
    setChallenge(null);
    const result = await query.refetch();
    if (live.current && !result.isError) setUnknown(false);
  }
  async function verify() {
    setBusy(true); setChallenge(null);
    try {
      const value = await channelApi.companyIdentityChallenge(provider, scope);
      if (!live.current) return;
      if (value.tenant_id !== scope.expected_tenant_id || value.user_id !== scope.expected_user_id) throw new Error('Identity scope changed');
      setChallenge(value);
    } catch { if (live.current) setUnknown(true); }
    finally { if (live.current) setBusy(false); }
  }
  async function disconnect(principalId: string) {
    setBusy(true); setChallenge(null);
    try { await channelApi.companyIdentityDisconnect(provider, principalId, scope); if (live.current) await refresh(); }
    catch { if (live.current) setUnknown(true); }
    finally { if (live.current) setBusy(false); }
  }
  const value = query.isError || unknown ? undefined : query.data;
  return <>
    <p>{t('companyMessaging.memberHelp')}</p>
    {(query.isError || unknown) && <p role="alert">{t('companyMessaging.identityFailed')}</p>}
    {query.isPending && <p role="status">{t('common.loading')}</p>}
    {value && <>
      {value.state === 'setup_required' && <p>{t('companyMessaging.setupRequired')}</p>}
      {value.state === 'directory_link_required' && <p>{t('companyMessaging.directoryRequired')}</p>}
      {value.state === 'identity_ambiguous' && <p>{t('companyMessaging.identityAmbiguous')}</p>}
      {value.state === 'identity_history_conflict' && <p>{t('companyMessaging.historyConflict')}</p>}
      {value.state === 'installation_revoked' && <p>{t('companyMessaging.revoked')}</p>}
      {value.state === 'bound' && <p role="status">{t('companyMessaging.bound')}</p>}
      {['verification_required', 'reconnect_required'].includes(value.state) && <button className="btn btn-primary" type="button" disabled={busy || query.isFetching}
        onClick={() => { void verify(); }}>{t('companyMessaging.verify')}</button>}
      {value.subject_id && <p>{t('companyMessaging.subject')}: <code>{value.subject_id}</code></p>}
      {value.disconnectable.map((entry) => <div key={entry.principal_id}>
        <p>{t('tenantFeishu.savedIdentityLink')}: <code>{entry.subject_id}</code></p>
        <button className="btn btn-secondary" type="button" disabled={busy || query.isFetching} onClick={() => { void disconnect(entry.principal_id); }}>{t('tenantFeishu.disconnect')}</button>
      </div>)}
    </>}
    {challenge && <>
      <p>{t('companyMessaging.sendCommand')} <code>{challenge.subject_id}</code></p>
      <label htmlFor={`${id}-command`}>{t('companyMessaging.command')}</label>
      <input id={`${id}-command`} className="form-input" readOnly value={challenge.command} onFocus={(event) => event.currentTarget.select()} autoComplete="off" />
      <p>{t('companyMessaging.expires')}: <time dateTime={challenge.expires_at}>{new Date(challenge.expires_at).toLocaleString()}</time></p>
    </>}
    <div className="tenant-feishu-actions">
      <button className="btn btn-secondary" type="button" disabled={busy || query.isFetching} onClick={() => { void refresh(); }}>{t('common.refresh')}</button>
      <Link to="/assistant" onClick={onNavigate}>{t('assistant.title')}</Link>
    </div>
    <p>{t('companyMessaging.coverage')}</p>
  </>;
}
