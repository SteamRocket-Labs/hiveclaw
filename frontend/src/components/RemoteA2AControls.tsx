import { useEffect, useRef, useState, type FormEvent } from 'react';
import { useQuery } from '@tanstack/react-query';
import { useTranslation } from 'react-i18next';
import { ApiError } from '../api/core';
import { remoteA2AApi, type RemoteA2AConfiguration } from '../api/domains/remoteA2A';
import { useAuthStore } from '../stores';

function company(fallback: string) {
  try { return localStorage.getItem('current_tenant_id') || fallback; } catch { return fallback; }
}

export default function RemoteA2AControls({ agentId }: { agentId: string }) {
  const user = useAuthStore((state) => state.user);
  const [generation, setGeneration] = useState(0);
  useEffect(() => {
    const changed = (event: StorageEvent) => {
      if (!event.key || event.key === 'current_tenant_id') setGeneration((value) => value + 1);
    };
    window.addEventListener('storage', changed);
    return () => window.removeEventListener('storage', changed);
  }, []);
  const tenantId = company(user?.tenant_id || '');
  if (!user || !tenantId) return null;
  return <Connection key={`${agentId}:${user.id}:${tenantId}:${generation}`}
    agentId={agentId} actorId={user.id} tenantId={tenantId} />;
}

function Connection({ agentId, actorId, tenantId }: { agentId: string; actorId: string; tenantId: string }) {
  const { t } = useTranslation();
  const [cardUrl, setCardUrl] = useState('');
  const [rpcUrl, setRpcUrl] = useState('');
  const [token, setToken] = useState('');
  const [clearToken, setClearToken] = useState(false);
  const [networks, setNetworks] = useState('');
  const [maxBytes, setMaxBytes] = useState(16 * 1024 * 1024);
  const [timeout, setTimeout] = useState(20);
  const [busy, setBusy] = useState(false);
  const [unknown, setUnknown] = useState(false);
  const [credentialUnavailable, setCredentialUnavailable] = useState(false);
  const [endpointCredentialRequired, setEndpointCredentialRequired] = useState(false);
  const dirty = useRef(false);
  const pending = useRef<RemoteA2AConfiguration | null>(null);
  const matches = () => {
    const current = useAuthStore.getState().user;
    return current?.id === actorId && company(current?.tenant_id || '') === tenantId;
  };
  const query = useQuery({ queryKey: ['remote-a2a', actorId, tenantId, agentId], retry: false,
    queryFn: async () => {
      const page = await remoteA2AApi.get(agentId);
      if (!matches() || page.agent_id !== agentId) throw new Error('Remote connection scope changed');
      return page;
    } });
  useEffect(() => {
    if (!query.data || dirty.current || pending.current) return;
    setCardUrl(query.data.card_url || '');setRpcUrl(query.data.rpc_url || '');
    setNetworks(query.data.allowed_networks.join(', '));
    setMaxBytes(query.data.max_response_bytes);setTimeout(query.data.timeout_seconds);
  }, [query.data]);
  useEffect(() => () => { pending.current = null; }, []);
  function credentialRepair(error: unknown): 'unreadable' | 'endpoint' | null {
    if (!(error instanceof ApiError)) return null;
    const data = error.data as { code?: string; detail?: { code?: string } } | undefined;
    const code = data?.code || data?.detail?.code;
    if (error.status === 503 && code === 'remote_a2a_credential_unavailable') return 'unreadable';
    if (error.status === 409 && code === 'remote_a2a_new_endpoint_credential_required') return 'endpoint';
    return null;
  }
  async function refresh() {
    const result = await query.refetch();
    if (result.isError || !matches()) return;
    if (!pending.current || result.data?.request_id === pending.current.request_id) {
      pending.current = null;setToken('');setClearToken(false);setUnknown(false);
      if (!result.data?.credential_configured) { setCredentialUnavailable(false);setEndpointCredentialRequired(false); }
    }
  }
  async function save(event: FormEvent) {
    event.preventDefault();
    if (!matches() || !query.data) { setUnknown(true);return; }
    const body = pending.current || { expected_user_id: actorId, expected_tenant_id: tenantId,
      expected_version: query.data.version, request_id: crypto.randomUUID(), card_url: cardUrl, rpc_url: rpcUrl,
      bearer_token: clearToken ? '' : token || undefined, allowed_networks: networks.split(',').map((value) => value.trim()).filter(Boolean),
      max_response_bytes: maxBytes, timeout_seconds: timeout };
    pending.current = body;setBusy(true);setUnknown(false);setCredentialUnavailable(false);setEndpointCredentialRequired(false);
    try {
      // The credential stays in component memory, outside query/mutation caches.
      const result = await remoteA2AApi.configure(agentId, body);
      if (!matches() || result.agent_id !== agentId || result.request_id !== body.request_id) throw new Error('Remote configuration scope changed');
      pending.current = null;dirty.current = false;setToken('');setClearToken(false);
      await refresh();
    } catch (error) {
      const repair = credentialRepair(error);
      if (repair) {
        pending.current = null;setCredentialUnavailable(repair === 'unreadable');setEndpointCredentialRequired(repair === 'endpoint');setUnknown(false);
      } else {
        if (error instanceof ApiError && error.status >= 400 && error.status < 500) pending.current = null;
        setUnknown(true);
      }
    } finally { setBusy(false); }
  }
  async function act(action: 'discover' | 'disable') {
    if (!matches() || !query.data) { setUnknown(true);return; }
    setBusy(true);
    try {
      const result = await remoteA2AApi[action](agentId, { expected_user_id: actorId, expected_tenant_id: tenantId,
        expected_version: query.data.version });
      if (!matches() || result.agent_id !== agentId) throw new Error('Remote connection scope changed');
      if (action === 'disable') { dirty.current = false;setToken('');setClearToken(false); }
      await refresh();
    } catch (error) {
      const repair = credentialRepair(error);
      if (repair) { setCredentialUnavailable(repair === 'unreadable');setEndpointCredentialRequired(repair === 'endpoint');setUnknown(false); }
      else setUnknown(true);
    } finally { setBusy(false); }
  }
  const states: Record<string, string> = {
    not_configured: t('remoteA2A.states.not_configured', 'Not configured'),
    configured: t('remoteA2A.states.configured', 'Configured'),
    discovered: t('remoteA2A.states.discovered', 'Agent Card read'),
    disabled: t('remoteA2A.states.disabled', 'Disabled'),
    needs_reauthorization: t('remoteA2A.states.needs_reauthorization', 'Owner authorization required'),
    unavailable: t('remoteA2A.states.unavailable', 'Unavailable'),
  };
  return <section className="card-pad-sm">
    <h3>{t('remoteA2A.title', 'Remote A2A connection')}</h3>
    <p>{t('remoteA2A.boundary', 'Connection discovery is available. Employee dispatch is not activated; action control and complete cost metering remain unverified.')}</p>
    {query.isPending && <p role="status">{t('common.loading', 'Loading…')}</p>}
    {(query.isError || unknown) && <p role="alert">{t('remoteA2A.unknown', 'The operation could not be confirmed. Refresh or retry the same saved request.')}</p>}
    {credentialUnavailable && <p role="alert">{t('remoteA2A.credentialUnavailable', 'The saved machine credential cannot be read. Replace it or disable the connection.')}</p>}
    {endpointCredentialRequired && <p role="alert">{t('remoteA2A.endpointCredentialRequired', 'Provide a machine credential again for the changed addresses, or remove the saved credential.')}</p>}
    {query.data && <>
      <p>{states[query.data.state] || states.unavailable}</p>
      {query.data.discovery_error && <p role="alert">{t('remoteA2A.discoveryFailed', 'Check the endpoint, credential and approved network ranges.')} <code>{query.data.discovery_error}</code></p>}
      {query.data.agent_card && <p>{query.data.agent_card.name} · {query.data.agent_card.description}</p>}
      {query.data.can_configure ? <form onSubmit={(event) => { void save(event); }}>
        <fieldset disabled={busy || unknown || !!pending.current} onChange={() => { dirty.current = true; }}>
          <label>{t('remoteA2A.cardUrl', 'Agent Card URL')}<input type="url" required value={cardUrl} onChange={(event) => setCardUrl(event.target.value)} /></label>
          <label>{t('remoteA2A.rpcUrl', 'Task interface URL')}<input type="url" required value={rpcUrl} onChange={(event) => setRpcUrl(event.target.value)} /></label>
          <label>{t('remoteA2A.token', 'Machine credential (leave blank to keep)')}<input type="password" autoComplete="off" value={token} onChange={(event) => setToken(event.target.value)} /></label>
          {query.data.credential_configured && <label><input type="checkbox" checked={clearToken} onChange={(event) => setClearToken(event.target.checked)} />{t('remoteA2A.clearToken', 'Remove stored credential')}</label>}
          <label>{t('remoteA2A.networks', 'Approved private network ranges (comma separated)')}<input value={networks} onChange={(event) => setNetworks(event.target.value)} /></label>
          <label>{t('remoteA2A.maxBytes', 'Maximum response size (bytes)')}<input type="number" required min={1} max={2147483647} value={maxBytes} onChange={(event) => setMaxBytes(Number(event.target.value))} /></label>
          <label>{t('remoteA2A.timeout', 'Connection timeout (seconds)')}<input type="number" required min={1} max={2147483647} value={timeout} onChange={(event) => setTimeout(Number(event.target.value))} /></label>
        </fieldset>
        <button className="btn btn-primary" type="submit" disabled={busy || (unknown && !pending.current)}>
          {pending.current ? t('remoteA2A.retry', 'Retry same save') : t('common.save', 'Save')}</button>
      </form> : <p>{t('remoteA2A.admin', 'Ask a company administrator to configure this connection.')}</p>}
      {query.data.version !== null && <>
        {query.data.can_configure && <button className="btn btn-secondary" type="button" disabled={busy || unknown || query.data.state === 'disabled'} onClick={() => { void act('discover'); }}>{t('remoteA2A.discover', 'Read Agent Card')}</button>}
        {query.data.can_disable && query.data.state !== 'disabled' && <button className="btn btn-secondary" type="button" disabled={busy || unknown} onClick={() => { void act('disable'); }}>{t('remoteA2A.disable', 'Disable connection')}</button>}
      </>}
    </>}
    <button className="btn btn-secondary" type="button" disabled={busy || query.isFetching} onClick={() => { void refresh(); }}>{t('common.refresh', 'Refresh')}</button>
  </section>;
}
