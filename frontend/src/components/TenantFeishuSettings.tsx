import { useEffect, useId, useState } from 'react';
import { useQuery } from '@tanstack/react-query';
import { Link } from 'react-router-dom';
import { useTranslation } from 'react-i18next';
import type { TFunction } from 'i18next';
import { ApiError } from '../api/core';
import { channelApi, type TenantChannelConfig, type TenantChannelScope, type FeishuPlatformRegion, type TenantFeishuIdentityState } from '../api/domains/channels';
import { useCompanyChannelScope } from './useCompanyChannelScope';
import './TenantFeishuSettings.css';
import TenantFeishuGroupRoutes from './TenantFeishuGroupRoutes';


export default function TenantFeishuSettings({ selectedTenantId, onConfigured, onBusyChange }: {
  selectedTenantId: string; onConfigured?: () => void; onBusyChange?: (busy: boolean) => void;
}) {
  const current = useCompanyChannelScope(selectedTenantId);
  if (!current.user || !current.tenant || !['org_admin', 'platform_admin'].includes(current.user.role)) return null;
  return <CompanyBotEditor key={current.key} scope={current.scope} scopeKey={current.key}
    onConfigured={onConfigured} onBusyChange={onBusyChange} />;
}

function CompanyBotEditor({ scope, scopeKey, onConfigured, onBusyChange }: {
  scope: TenantChannelScope; scopeKey: string; onConfigured?: () => void; onBusyChange?: (busy: boolean) => void;
}) {
  const { t } = useTranslation();
  const id = useId();
  const [base, setBase] = useState<TenantChannelConfig | null | undefined>(undefined);
  const [appId, setAppId] = useState('');
  const [active, setActive] = useState(true);
  const [region, setRegion] = useState<FeishuPlatformRegion | 'keep'>('feishu_cn');
  const [secrets, setSecrets] = useState({ app_secret: '', encrypt_key: '', verification_token: '' });
  const [clear, setClear] = useState({ encrypt_key: false, verification_token: false });
  const [busy, setBusy] = useState(false);
  const [outcome, setOutcome] = useState<'saved' | 'unknown' | 'invalid' | 'denied' | null>(null);
  const query = useQuery({ queryKey: ['tenant-feishu-config', scopeKey], retry: false,
    queryFn: async ({ signal }) => {
      const [configs, webhook] = await Promise.all([channelApi.tenantChannels(scope, signal), channelApi.tenantFeishuWebhook(scope, signal)]);
      const config = configs.find((item) => item.channel_type === 'feishu') || null;
      if (config && config.tenant_id !== scope.expected_tenant_id || webhook.tenant_id !== scope.expected_tenant_id) throw new Error('Company scope changed');
      return { config, webhook: webhook.webhook_url };
    } });
  function setInitial(config: TenantChannelConfig | null) {
    setAppId(config?.app_id || ''); setActive(config?.is_active ?? true);
    const savedRegion = config?.extra_config.platform_region;
    setRegion(savedRegion === 'lark_global' ? 'lark_global' : savedRegion && savedRegion !== 'feishu_cn' ? 'keep' : 'feishu_cn');
  }
  useEffect(() => { if (query.data && base === undefined) { setBase(query.data.config); setInitial(query.data.config); } }, [query.data, base]);
  async function refresh() {
    const latest = await query.refetch();
    if (!latest.isError && latest.data) { if (base === undefined) setInitial(latest.data.config); setBase(latest.data.config); setOutcome(null); }
  }
  async function save() {
    setBusy(true); onBusyChange?.(true); setOutcome(null);
    try {
      const saved = await channelApi.saveTenantFeishu({ ...scope, app_id: appId, is_active: active,
        ...(region !== 'keep' ? { platform_region: region } : {}),
        expected_revision: base?.revision || 'unconfigured', ...(secrets.app_secret ? { app_secret: secrets.app_secret } : {}),
        ...(clear.encrypt_key ? { encrypt_key: '' } : secrets.encrypt_key ? { encrypt_key: secrets.encrypt_key } : {}),
        ...(clear.verification_token ? { verification_token: '' } : secrets.verification_token ? { verification_token: secrets.verification_token } : {}),
      });
      if (saved.tenant_id !== scope.expected_tenant_id) throw new Error('Company scope changed');
      setBase(saved); setInitial(saved); setSecrets({ app_secret: '', encrypt_key: '', verification_token: '' });
      setClear({ encrypt_key: false, verification_token: false }); setOutcome('saved');
      await query.refetch();
      onConfigured?.();
    } catch (error) {
      setOutcome(error instanceof ApiError && error.status === 422 ? 'invalid'
        : error instanceof ApiError && [400, 401, 403].includes(error.status) ? 'denied' : 'unknown');
    } finally { setBusy(false); onBusyChange?.(false); }
  }
  const saved = !query.isError ? query.data?.config : undefined;
  const groupScopeKey = JSON.stringify([scopeKey, saved?.id, saved?.app_id, saved?.extra_config.platform_region, saved?.extra_config.open_api_domain]);
  const changedElsewhere = base !== undefined && query.data && (base?.revision || 'unconfigured') !== (query.data.config?.revision || 'unconfigured');
  const appChanged = Boolean(base && (base.app_id !== appId.trim() || region !== 'keep' && region !== (base.extra_config.platform_region || 'feishu_cn')));
  const secretFields = [
    { field: 'app_secret' as const, label: t('tenantFeishu.appSecret', 'App secret'), stored: saved?.has_app_secret },
    { field: 'encrypt_key' as const, label: t('tenantFeishu.encryptKey', 'Encrypt key'), stored: saved?.has_encrypt_key },
    { field: 'verification_token' as const, label: t('tenantFeishu.verificationToken', 'Verification token'), stored: saved?.has_verification_token },
  ];
  return <section className="card tenant-feishu-settings" aria-labelledby={`${id}-title`}>
    <h3 id={`${id}-title`}>{t('tenantFeishu.title', 'Company Feishu / Lark bot')}</h3>
    <p>{t('tenantFeishu.description', 'Private messages go to each member’s selected My Assistant. Configure the application callback in Feishu or Lark after saving.')}</p>
    <p>{t('tenantFeishu.notDeliveryProof', 'Saved settings and verified identity do not confirm channel permissions or message delivery.')}</p>
    <p>{t('tenantFeishu.authenticationHint', 'An enabled bot needs an app secret and at least one callback authenticator: Encrypt Key or Verification Token.')}</p>
    {query.isError && <p role="alert">{t('tenantFeishu.readFailed', 'Could not read current settings. Your draft is kept; refresh to retry.')}</p>}
    {query.isPending && <p role="status">{t('common.loading', 'Loading…')}</p>}
    {outcome === 'invalid' && <p role="alert">{t('tenantFeishu.invalidSettings', 'Check the application credentials and callback authentication. A new application or platform needs new credentials.')}</p>}
    {outcome === 'denied' && <p role="alert">{t('tenantFeishu.saveDenied', 'This save was refused. Check your account and company selection, then refresh.')}</p>}
    {(changedElsewhere || outcome === 'unknown') && <p role="alert">{t('tenantFeishu.reviewDraft', 'Could not confirm this draft against the saved settings. Refresh, compare the saved application with your draft, then save if needed.')}</p>}
    {query.data && !query.isError && <p>{t('tenantFeishu.savedApplication', 'Saved application')}: {saved?.app_id || t('tenantFeishu.none', 'Not configured')} · {saved?.is_active ? t('tenantFeishu.enabled', 'Enabled') : t('tenantFeishu.paused', 'Paused')}</p>}
    {appChanged && <p>{t('tenantFeishu.newAppCredentials', 'Changing the application or platform requires its own app secret and callback authentication. Previous credentials are not reused.')}</p>}
    <fieldset disabled={busy || base === undefined} className="tenant-feishu-fields">
      <label htmlFor={`${id}-app`}>{t('tenantFeishu.appId', 'App ID')}</label>
      <input id={`${id}-app`} className="form-input" value={appId} onChange={(event) => { setAppId(event.target.value); setOutcome(null); }} autoComplete="off" />
      <label htmlFor={`${id}-region`}>{t('tenantFeishu.platform', 'Platform')}</label>
      <select id={`${id}-region`} className="form-input" value={region} onChange={(event) => { setRegion(event.target.value as FeishuPlatformRegion | 'keep'); setOutcome(null); }}>
        <option value="feishu_cn">{t('tenantFeishu.feishu', 'Feishu')}</option><option value="lark_global">{t('tenantFeishu.lark', 'Lark')}</option>
        {Boolean(base?.extra_config.platform_region) && !['feishu_cn', 'lark_global'].includes(String(base?.extra_config.platform_region)) && <option value="keep">{t('tenantFeishu.keepPlatform', 'Keep the existing platform')}</option>}
      </select>
      {secretFields.map(({ field, label, stored }) => <div key={field} className="tenant-feishu-secret">
        <label htmlFor={`${id}-${field}`}>{label}</label>
        <input id={`${id}-${field}`} className="form-input" type="password" autoComplete="new-password" value={secrets[field]}
          placeholder={stored && !appChanged ? t('tenantFeishu.keepSecret', 'Saved; leave blank to keep') : t('tenantFeishu.enterSecret', 'Enter a new value')}
          onChange={(event) => { setSecrets({ ...secrets, [field]: event.target.value }); setOutcome(null); }} disabled={field !== 'app_secret' && clear[field]} />
        {field !== 'app_secret' && stored && <label className="tenant-feishu-check"><input type="checkbox" checked={clear[field]}
          onChange={(event) => { setClear({ ...clear, [field]: event.target.checked }); setOutcome(null); }} />{t('tenantFeishu.clearSecret', { defaultValue: 'Clear saved {{field}}', field: label })}</label>}
      </div>)}
      <label className="tenant-feishu-check"><input type="checkbox" checked={active} onChange={(event) => { setActive(event.target.checked); setOutcome(null); }} />{t('tenantFeishu.enableBot', 'Enable the company bot')}</label>
    </fieldset>
    {query.data && !query.isError && <label className="tenant-feishu-webhook" htmlFor={`${id}-webhook`}>{t('tenantFeishu.webhook', 'Event callback URL')}
      <input id={`${id}-webhook`} className="form-input" readOnly value={query.data.webhook} onFocus={(event) => event.currentTarget.select()} /></label>}
    <div className="tenant-feishu-actions">
      <button type="button" className="btn btn-secondary" onClick={() => { void refresh(); }} disabled={busy || query.isFetching}>{t('common.refresh', 'Refresh')}</button>
      <button type="button" className="btn btn-primary" onClick={() => { void save(); }} disabled={busy || base === undefined || query.isError || query.isFetching || Boolean(changedElsewhere) || !appId.trim()}>{t('common.save', 'Save')}</button>
    </div>
    {outcome === 'saved' && <p role="status">{t('tenantFeishu.saved', 'Company bot settings saved.')}</p>}
    {saved?.is_active && <TenantFeishuGroupRoutes key={groupScopeKey} scope={scope} scopeKey={groupScopeKey} />}
  </section>;
}

function stateLabel(state: TenantFeishuIdentityState, t: TFunction): string {
  switch (state) {
    case 'company_bot_unconfigured': return t('tenantFeishu.stateUnconfigured', 'The company bot is not configured. Contact your company administrator.');
    case 'company_bot_paused': return t('tenantFeishu.statePaused', 'The company bot is paused.');
    case 'company_bot_setup_required': return t('tenantFeishu.stateSetup', 'The company bot needs credentials or callback authentication. Contact your administrator.');
    case 'identity_provider_required': return t('tenantFeishu.stateProvider', 'Company Feishu sign-in needs administrator configuration.');
    case 'identity_app_mismatch': return t('tenantFeishu.stateAppMismatch', 'The bot and verified identity application do not match. Ask your administrator to review the company setup.');
    case 'identity_required': return t('tenantFeishu.stateIdentity', 'Verify your Feishu account above, then refresh this status.');
    case 'identity_conflict': return t('tenantFeishu.stateConflict', 'This identity needs administrator review before it can act as your account.');
    case 'installation_revoked': return t('tenantFeishu.stateRevoked', 'This bot identity was revoked. Contact your administrator.');
    case 'awaiting_first_message': return t('tenantFeishu.stateWaiting', 'Your identity is ready. Send the company bot a message to start work.');
    case 'reconnect_required': return t('tenantFeishu.stateReconnect', 'This identity is disconnected. Reconnect explicitly to use your account again.');
    case 'bound': return t('tenantFeishu.stateBound', 'Your identity is verified for this company bot.');
    case 'assistant_required': return t('tenantFeishu.stateAssistant', 'Choose or open My Assistant before sending work to the company bot.');
    case 'multiple_identities': return t('tenantFeishu.stateMultiple', 'Review each of your verified identities below.');
  }
}

export function TenantFeishuIdentity({ onNavigate }: { onNavigate?: () => void }) {
  const current = useCompanyChannelScope();
  if (!current.user || !current.tenant) return null;
  return <MemberBotIdentity key={current.key} scope={current.scope} scopeKey={current.key} onNavigate={onNavigate} />;
}

function MemberBotIdentity({ scope, scopeKey, onNavigate }: { scope: TenantChannelScope; scopeKey: string; onNavigate?: () => void }) {
  const { t } = useTranslation();
  const [busy, setBusy] = useState(false);
  const [unknown, setUnknown] = useState(false);
  const query = useQuery({ queryKey: ['tenant-feishu-identity', scopeKey], retry: false, queryFn: async ({ signal }) => {
    const value = await channelApi.tenantFeishuIdentity(scope, signal);
    if (value.tenant_id !== scope.expected_tenant_id || value.user_id !== scope.expected_user_id) throw new Error('Identity scope changed');
    return value;
  } });
  async function refresh() { const result = await query.refetch(); if (!result.isError) setUnknown(false); }
  async function act(action: 'disconnect' | 'reconnect', principalId: string) {
    setBusy(true); setUnknown(false);
    try { await channelApi.tenantFeishuIdentityAction(action, principalId, scope); await refresh(); }
    catch { setUnknown(true); } finally { setBusy(false); }
  }
  const value = !query.isError && !unknown ? query.data : undefined;
  return <section className="tenant-feishu-settings" aria-label={t('tenantFeishu.memberTitle', 'Company bot identity')}>
    <h4>{t('tenantFeishu.memberTitle', 'Company bot identity')}</h4>
    {(query.isError || unknown) && <p role="alert">{t('tenantFeishu.identityReadFailed', 'Could not confirm your current bot identity. Refresh before acting again.')}</p>}
    {query.isPending && <p role="status">{t('common.loading', 'Loading…')}</p>}
    {value && <>
      <p>{stateLabel(value.state, t)}</p>
      {value.entries.map((entry) => <div key={entry.subject_id} className="tenant-feishu-identity-row">
        {value.entries.length > 1 && <p><code>{entry.subject_id}</code> · {stateLabel(entry.state, t)}</p>}
        {entry.principal_id && entry.state === 'bound' && <button className="btn btn-secondary" type="button" disabled={busy || query.isFetching}
          onClick={() => { void act('disconnect', entry.principal_id!); }}>{t('tenantFeishu.disconnect', 'Disconnect this identity')}</button>}
        {entry.principal_id && entry.state === 'reconnect_required' && <button className="btn btn-primary" type="button" disabled={busy || query.isFetching}
          onClick={() => { void act('reconnect', entry.principal_id!); }}>{t('tenantFeishu.reconnect', 'Reconnect this identity')}</button>}
      </div>)}
      {(value.disconnectable || []).filter((link) => !value.entries.some((entry) => entry.principal_id === link.principal_id && entry.state === 'bound'))
        .map((link) => <div key={link.principal_id} className="tenant-feishu-identity-row">
          <p>{t('tenantFeishu.savedIdentityLink', 'Saved account link')}: <code>{link.subject_id}</code></p>
          <button className="btn btn-secondary" type="button" disabled={busy || query.isFetching}
            onClick={() => { void act('disconnect', link.principal_id); }}>{t('tenantFeishu.disconnect', 'Disconnect this identity')}</button>
        </div>)}
      {value.assistant_required && <Link to="/assistant" onClick={onNavigate}>{t('assistant.title', 'My Assistant')}</Link>}
      {(value.entries.some((entry) => entry.state === 'bound') || Boolean(value.disconnectable?.length)) && <p>{t('tenantFeishu.disconnectEffect', 'Disconnecting stops this identity from acting as your Hive account. Earlier work is retained.')}</p>}
    </>}
    <button className="btn btn-secondary" type="button" onClick={() => { void refresh(); }} disabled={busy || query.isFetching}>{t('common.refresh', 'Refresh')}</button>
  </section>;
}
