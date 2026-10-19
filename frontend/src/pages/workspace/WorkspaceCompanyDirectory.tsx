import { useEffect, useId, useRef, useState } from 'react';
import { useQuery, useQueryClient } from '@tanstack/react-query';
import { useTranslation } from 'react-i18next';
import { channelApi, type TenantChannelConfig, type TenantChannelScope } from '../../api/domains/channels';
import { ApiError } from '../../api/core';
import { enterpriseApi } from '../../api/domains/enterprise';
import { useAuthStore } from '../../stores';

type Provider = 'dingtalk' | 'wecom';

export default function WorkspaceCompanyDirectory({ selectedTenantId }: { selectedTenantId: string }) {
  const user = useAuthStore((state) => state.user);
  const [provider, setProvider] = useState<Provider>('dingtalk');
  const { t } = useTranslation();
  const id = useId();
  const tenant = selectedTenantId || user?.tenant_id || '';
  if (!tenant || !user || !['org_admin', 'platform_admin'].includes(user.role)) return null;
  const scope = { expected_tenant_id: tenant, expected_user_id: user.id };
  return <section className="card ws-org-card" aria-labelledby={`${id}-title`}>
    <h4 id={`${id}-title`}>{t('companyDirectory.title')}</h4>
    <p>{t('companyDirectory.description')}</p>
    <label htmlFor={`${id}-provider`}>{t('companyDirectory.provider')}</label>
    <select id={`${id}-provider`} className="input" value={provider} onChange={(event) => setProvider(event.target.value as Provider)}>
      <option value="dingtalk">{t('companyDirectory.dingtalk')}</option>
      <option value="wecom">{t('companyDirectory.wecom')}</option>
    </select>
    <DirectoryEditor key={`${user.id}:${user.role}:${tenant}:${provider}`} scope={scope} provider={provider} />
  </section>;
}

function DirectoryEditor({ scope, provider }: { scope: TenantChannelScope; provider: Provider }) {
  const { t } = useTranslation();
  const id = useId();
  const cache = useQueryClient();
  const live = useRef(true);
  useEffect(() => { live.current = true; return () => { live.current = false; }; }, []);
  const [base, setBase] = useState<TenantChannelConfig | null | undefined>(undefined);
  const [appId, setAppId] = useState('');
  const [secret, setSecret] = useState('');
  const [messaging, setMessaging] = useState(false);
  const [namespace, setNamespace] = useState('');
  const [token, setToken] = useState('');
  const [encodingKey, setEncodingKey] = useState('');
  const [wecomTransport, setWecomTransport] = useState('app');
  const [botId, setBotId] = useState('');
  const [botSubjectMode, setBotSubjectMode] = useState('encrypted');
  const namespaceKey = provider === 'wecom' ? 'wecom_agent_id' : 'corp_id';

  const [active, setActive] = useState(true);
  const [busy, setBusy] = useState(false);
  const [message, setMessage] = useState('');
  const [error, setError] = useState(false);
  const query = useQuery({ queryKey: ['company-directory-connection', scope.expected_user_id, scope.expected_tenant_id, provider],
    retry: false, queryFn: async ({ signal }) => {
      const configs = await channelApi.tenantChannels(scope, signal);
      if (configs.some((row) => row.tenant_id !== scope.expected_tenant_id)) throw new Error('Company scope changed');
      return configs.find((row) => row.channel_type === provider) || null;
    } });
  useEffect(() => {
    if (query.data !== undefined && base === undefined) {
      setBase(query.data); setAppId(query.data?.app_id || ''); setActive(query.data?.is_active ?? true);
      setMessaging(query.data?.extra_config.messaging_enabled === true);
      setNamespace(String(query.data?.extra_config[namespaceKey] || ''));
      setWecomTransport(String(query.data?.extra_config.wecom_transport || 'app'));
      setBotId(String(query.data?.extra_config.wecom_bot_id || ''));
      setBotSubjectMode(String(query.data?.extra_config.wecom_bot_userid_mode || 'encrypted'));
    }
  }, [query.data, base, namespaceKey]);
  async function refresh() {
    const result = await query.refetch();
    if (!live.current || result.isError || result.data === undefined) return;
    setBase(result.data); setMessage(''); setError(false);
  }
  async function save() {
    setBusy(true); setMessage(''); setError(false);
    try {
      const saved = await channelApi.saveTenantDirectory(provider, { ...scope, app_id: appId, is_active: active,
        expected_revision: base?.revision || 'unconfigured', ...(secret ? { app_secret: secret } : {}),
        ...(messaging !== (base?.extra_config.messaging_enabled === true) || namespace !== String(base?.extra_config[namespaceKey] || '') || botChanged
          ? { extra_config: { ...base?.extra_config, messaging_enabled: messaging, [namespaceKey]: namespace.trim(),
            ...(provider === 'wecom' ? { wecom_transport: wecomTransport, wecom_bot_id: botId.trim(), wecom_bot_userid_mode: botSubjectMode } : {}) } } : {}),
        ...(token ? { verification_token: token } : {}), ...(encodingKey ? { encrypt_key: encodingKey } : {}) });
      if (!live.current) return;
      if (saved.tenant_id !== scope.expected_tenant_id || saved.channel_type !== provider) throw new Error('Company scope changed');
      setBase(saved); setAppId(saved.app_id || ''); setActive(saved.is_active); setSecret(''); setToken(''); setEncodingKey('');
      setMessaging(saved.extra_config.messaging_enabled === true); setNamespace(String(saved.extra_config[namespaceKey] || ''));
      setMessage(t('companyDirectory.saved'));
      await query.refetch();
    } catch (failure) {
      if (live.current) {
        setError(true);
        const code = failure instanceof ApiError && typeof failure.data === 'object' && failure.data !== null && 'code' in failure.data ? failure.data.code : null;
        setMessage(code === 'company_dingtalk_application_in_use' ? t('companyMessaging.applicationInUse') : t('companyDirectory.saveFailed'));
      }
    } finally { if (live.current) setBusy(false); }
  }
  async function sync() {
    setBusy(true); setMessage(''); setError(false);
    try {
      const result = await enterpriseApi.syncOrg(scope.expected_tenant_id, provider, scope.expected_user_id);
      if (!live.current) return;
      if (result.tenant_id !== scope.expected_tenant_id || result.provider !== provider) throw new Error('Company scope changed');
      if (result.status !== 'complete') {
        setError(true); setMessage(typeof result.error === 'string' ? result.error : t('companyDirectory.syncFailed'));
      } else {
        const assignments = result.assignment_counts as { conflict?: number; unavailable?: number } | undefined;
        setMessage(t('companyDirectory.complete', { departments: result.departments, members: result.members })
          + (assignments?.conflict || assignments?.unavailable ? ` ${t('companyDirectory.assignmentReview')}` : ''));
        for (const key of ['org-departments', 'org-members', 'org-identity-links', 'org-department-links']) {
          await cache.invalidateQueries({ predicate: (item) => item.queryKey[0] === key && item.queryKey.includes(scope.expected_tenant_id) });
        }
      }
    } catch { if (live.current) { setError(true); setMessage(t('companyDirectory.syncFailed')); } }
    finally { if (live.current) setBusy(false); }
  }
  const changedElsewhere = base !== undefined && query.data !== undefined
    && (base?.revision || 'unconfigured') !== (query.data?.revision || 'unconfigured');
  const appChanged = Boolean(base && (appId.trim() !== base.app_id || provider === 'wecom' && namespace !== String(base.extra_config.wecom_agent_id || '')));
  const botChanged = provider === 'wecom' && (wecomTransport !== (base?.extra_config.wecom_transport || 'app')
    || botId.trim() !== String(base?.extra_config.wecom_bot_id || '')
    || botSubjectMode !== (base?.extra_config.wecom_bot_userid_mode || 'encrypted'));
  const dirty = appId.trim() !== (base?.app_id || '') || active !== (base?.is_active ?? true) || Boolean(secret || token || encodingKey)
    || messaging !== (base?.extra_config.messaging_enabled === true) || namespace !== String(base?.extra_config[namespaceKey] || '') || botChanged;
  return <>
    <p>{t('companyDirectory.coverage')}</p>
    {query.isError && <p role="alert">{t('companyDirectory.readFailed')}</p>}
    {changedElsewhere && <p role="alert">{t('companyDirectory.changed')}</p>}
    <div className="ws-org-form-row">
      <div className="ws-org-field">
        <label htmlFor={`${id}-app`} className="ws-org-label">{provider === 'wecom' ? 'Corp ID' : 'App Key'}</label>
        <input id={`${id}-app`} className="input" value={appId} onChange={(event) => setAppId(event.target.value)} />
      </div>
      <div className="ws-org-field">
        <label htmlFor={`${id}-secret`} className="ws-org-label">{t('companyDirectory.secret')}</label>
        <input id={`${id}-secret`} className="input" type="password" autoComplete="new-password" value={secret}
          onChange={(event) => setSecret(event.target.value)} placeholder={base?.has_app_secret && !appChanged ? t('companyDirectory.secretSaved') : ''} />
      </div>
    </div>
    <label><input type="checkbox" checked={active} onChange={(event) => setActive(event.target.checked)} /> {t('companyDirectory.active')}</label>
    <fieldset className="tenant-feishu-fields" disabled={busy || base === undefined}>
      <legend>{t('companyMessaging.setupTitle')}</legend>
      <p>{t('companyMessaging.adminHelp')}</p>
      <label><input type="checkbox" checked={messaging} onChange={(event) => setMessaging(event.target.checked)} /> {t('companyMessaging.enable')}</label>
      <label htmlFor={`${id}-namespace`}>{provider === 'wecom' ? 'AgentID' : 'Corp ID'}</label>
      <input id={`${id}-namespace`} className="input" value={namespace} onChange={(event) => setNamespace(event.target.value)} />
      {provider === 'wecom' && <>
        <label htmlFor={`${id}-transport`}>{t('companyMessaging.wecomTransport')}</label>
        <select id={`${id}-transport`} className="input" value={wecomTransport} onChange={(event) => setWecomTransport(event.target.value)}>
          <option value="app">{t('companyMessaging.wecomApp')}</option>
          <option value="smart_bot">{t('companyMessaging.wecomSmartBot')}</option>
        </select>
        {wecomTransport === 'smart_bot' && <>
          <label htmlFor={`${id}-bot-id`}>Bot ID</label>
          <input id={`${id}-bot-id`} className="input" value={botId} onChange={(event) => setBotId(event.target.value)} />
          <label htmlFor={`${id}-bot-subject`}>{t('companyMessaging.botSubjectMode')}</label>
          <select id={`${id}-bot-subject`} className="input" value={botSubjectMode} onChange={(event) => setBotSubjectMode(event.target.value)}>
            <option value="encrypted">{t('companyMessaging.botEncrypted')}</option>
            <option value="plaintext">{t('companyMessaging.botPlaintext')}</option>
          </select>
          <p>{t('companyMessaging.smartBotHelp')}</p>
        </>}
        <label htmlFor={`${id}-callback-token`}>{t('tenantFeishu.verificationToken')}</label>
        <input id={`${id}-callback-token`} className="input" type="password" autoComplete="new-password" value={token} onChange={(event) => setToken(event.target.value)}
          placeholder={base?.has_verification_token && !appChanged && !botChanged ? t('companyDirectory.secretSaved') : ''} />
        <label htmlFor={`${id}-encoding-key`}>{t('companyMessaging.encodingKey')}</label>
        <input id={`${id}-encoding-key`} className="input" type="password" autoComplete="new-password" value={encodingKey} onChange={(event) => setEncodingKey(event.target.value)}
          placeholder={base?.has_encrypt_key && !appChanged && !botChanged ? t('companyDirectory.secretSaved') : ''} />
      </>}
      <p>{provider === 'wecom' ? t(wecomTransport === 'smart_bot' ? 'companyMessaging.smartBotSetup' : 'companyMessaging.wecomSetup') : t('companyMessaging.dingtalkSetup')}</p>
      <p>{t('companyMessaging.coverage')}</p>
      {query.data?.messaging_status?.state === 'receiving' && <p role="status">{t('companyMessaging.receiving')} {query.data.messaging_status.last_received_at && <time dateTime={query.data.messaging_status.last_received_at}>{new Date(query.data.messaging_status.last_received_at).toLocaleString()}</time>}</p>}
      {query.data?.messaging_status?.state === 'starting' && <p role="status">{t('companyMessaging.starting')}</p>}
      {query.data?.messaging_status?.state === 'configuration_conflict' && <p role="alert">{t('companyMessaging.applicationInUse')}</p>}
      {query.data?.messaging_status?.state === 'unavailable' && <p role="alert">{t('companyMessaging.unavailable')}</p>}

    </fieldset>
    {provider === 'wecom' && base?.extra_config.messaging_enabled === true && <CompanyCallbackUrl scope={scope} />}
    <div className="ws-org-actions">
      <button className="btn btn-secondary" disabled={busy || query.isPending} onClick={refresh}>{t('companyDirectory.refresh')}</button>
      <button className="btn btn-secondary" disabled={busy || base === undefined || query.isError || changedElsewhere || !appId.trim()}
        onClick={save}>{t('companyDirectory.save')}</button>
      <button className="btn btn-primary" disabled={busy || !base?.is_active || !base.has_app_secret || dirty || query.isError || changedElsewhere}
        onClick={sync}>{busy ? t('enterprise.org.syncing') : t('enterprise.org.syncNow')}</button>
    </div>
    {message && <p role={error ? 'alert' : 'status'}>{message}</p>}
    <a href="#identity-repair-person">{t('companyDirectory.repair')}</a>
    {' · '}<a href="#org-department-cutover">{t('companyDirectory.departments')}</a>
  </>;
}

function CompanyCallbackUrl({ scope }: { scope: TenantChannelScope }) {
  const { t } = useTranslation();
  const id = useId();
  const query = useQuery({ queryKey: ['company-wecom-callback', scope.expected_user_id, scope.expected_tenant_id], retry: false,
    queryFn: async ({ signal }) => {
      const value = await channelApi.tenantCompanyWebhook('wecom', scope, signal);
      if (value.tenant_id !== scope.expected_tenant_id) throw new Error('Company scope changed');
      return value.webhook_url;
    } });
  return <div className="ws-org-field">
    {query.isError && <p role="alert">{t('companyDirectory.readFailed')}</p>}
    {query.data && !query.isError && <><label htmlFor={`${id}-url`}>{t('tenantFeishu.webhook')}</label>
      <input id={`${id}-url`} className="input" value={query.data} readOnly onFocus={(event) => event.currentTarget.select()} /></>}
  </div>;
}
