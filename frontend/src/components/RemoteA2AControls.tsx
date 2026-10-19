import { useEffect, useRef, useState, type FormEvent } from 'react';
import { useQuery, useQueryClient } from '@tanstack/react-query';
import { useTranslation } from 'react-i18next';
import { ApiError } from '../api/core';
import { remoteA2AApi, type RemoteA2AConfiguration, type RemoteRuntimeSelection } from '../api/domains/remoteA2A';
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
  const client = useQueryClient();
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
  const pendingRuntime = useRef<RemoteRuntimeSelection | null>(null);
  const [authorizeEffects, setAuthorizeEffects] = useState(false);
  const [authorizeCosts, setAuthorizeCosts] = useState(false);
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
  useEffect(() => {
    setAuthorizeEffects(false);setAuthorizeCosts(false);
  }, [query.data?.version, query.data?.runtime_revision]);
  useEffect(() => () => { pending.current = null;pendingRuntime.current = null; }, []);
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
    const configurationConfirmed = !pending.current || result.data?.request_id === pending.current.request_id;
    const runtimeConfirmed = !pendingRuntime.current || result.data?.runtime_request_id === pendingRuntime.current.request_id
      || (result.data?.runtime_revision ?? null) !== pendingRuntime.current.expected_runtime_revision;
    if (configurationConfirmed) {
      pending.current = null;setToken('');setClearToken(false);
      if (!result.data?.credential_configured) { setCredentialUnavailable(false);setEndpointCredentialRequired(false); }
    }
    if (runtimeConfirmed) pendingRuntime.current = null;
    setUnknown(!configurationConfirmed || !runtimeConfirmed);
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
  async function selectRuntime(selected: RemoteRuntimeSelection['selected_runtime']) {
    if (!matches() || !query.data || pending.current) return;
    const body = pendingRuntime.current || { expected_user_id: actorId, expected_tenant_id: tenantId,
      expected_version: query.data.version, expected_runtime_revision: query.data.runtime_revision ?? null,
      request_id: crypto.randomUUID(), selected_runtime: selected,
      uncontrolled_effects_acknowledged: authorizeEffects, unknown_cost_acknowledged: authorizeCosts };
    pendingRuntime.current = body;setBusy(true);
    try {
      const result = await remoteA2AApi.selectRuntime(agentId, body);
      if (!matches() || result.agent_id !== agentId || result.runtime_request_id !== body.request_id) throw new Error('Remote runtime scope changed');
      pendingRuntime.current = null;setUnknown(false);
      await refresh();
      void client.invalidateQueries({ queryKey: ['agent', agentId] });
    } catch (error) {
      if (error instanceof ApiError && error.status >= 400 && error.status < 500) pendingRuntime.current = null;
      setUnknown(true);
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
    {query.data && <p>{query.data.runtime_activation !== 'not_activated'
      ? t('remoteA2A.selectedBoundary', 'Executor selection does not verify the remote service’s internal actions, data access or charges. Saved native model settings are preserved for restoration.')
      : t('remoteA2A.boundary', 'Automatic employee dispatch is not activated. Explicit remote requests are available in the original work; internal action control and complete cost metering remain unverified.')}</p>}
    {query.isPending && <p role="status">{t('common.loading', 'Loading…')}</p>}
    {(query.isError || unknown) && <p role="alert">{t('remoteA2A.unknown', 'The operation could not be confirmed. Refresh or retry the same saved request.')}</p>}
    {credentialUnavailable && <p role="alert">{t('remoteA2A.credentialUnavailable', 'The saved machine credential cannot be read. Replace it or disable the connection.')}</p>}
    {endpointCredentialRequired && <p role="alert">{t('remoteA2A.endpointCredentialRequired', 'Provide a machine credential again for the changed addresses, or remove the saved credential.')}</p>}
    {query.data && <>
      <p>{states[query.data.state] || states.unavailable}</p>
      {query.data.discovery_error && <p role="alert">{t('remoteA2A.discoveryFailed', 'Check the endpoint, credential and approved network ranges.')} <code>{query.data.discovery_error}</code></p>}
      {query.data.agent_card && <p>{query.data.agent_card.name} · {query.data.agent_card.description}</p>}
      <p role="status">{query.data.runtime_activation === 'activated'
        ? t('remoteA2A.runtimeActive', 'Remote executor selected for employee delegation. Remote internal actions and charges remain unverified.')
        : query.data.runtime_activation === 'authorization_required'
          ? t('remoteA2A.runtimeReauthorize', 'The selected remote executor needs renewed authorization. New dispatch is paused; review the connection or restore the previous executor.')
          : t('remoteA2A.runtimeInactive', 'The previous employee executor remains selected.')}</p>
      {query.data.can_configure && query.data.state === 'discovered' && <fieldset disabled={busy || unknown}>
        <legend>{t('remoteA2A.runtimeTitle', 'Choose the employee executor')}</legend>
        <label><input type="checkbox" checked={authorizeEffects} onChange={(event) => setAuthorizeEffects(event.target.checked)} />
          {t('remoteA2A.runtimeEffects', 'I authorize future employee delegations to this endpoint with unverified internal effect controls.')}</label>
        <label><input type="checkbox" checked={authorizeCosts} onChange={(event) => setAuthorizeCosts(event.target.checked)} />
          {t('remoteA2A.runtimeCosts', 'I accept unknown external charges for future delegations; no hard remote spend limit is claimed.')}</label>
        <button className="btn btn-secondary" type="button" disabled={!authorizeEffects || !authorizeCosts}
          onClick={() => { void selectRuntime('remote_a2a'); }}>{t('remoteA2A.selectRuntime', 'Select remote executor')}</button>
      </fieldset>}
      {query.data.runtime_activation !== 'not_activated' && query.data.can_disable && <button className="btn btn-secondary" type="button"
        disabled={busy || unknown} onClick={() => { void selectRuntime('previous'); }}>{t('remoteA2A.restoreRuntime', 'Restore previous executor')}</button>}
      {pendingRuntime.current && <button className="btn btn-secondary" type="button" disabled={busy}
        onClick={() => { void selectRuntime(pendingRuntime.current!.selected_runtime); }}>{t('remoteA2A.retryRuntime', 'Retry same executor selection')}</button>}
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
