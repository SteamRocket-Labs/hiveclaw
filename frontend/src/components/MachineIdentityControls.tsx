import { useEffect, useRef, useState, type FormEvent } from 'react';
import { useQuery } from '@tanstack/react-query';
import { useTranslation } from 'react-i18next';
import { machineIdentitiesApi, type MachineIssue, type MachineIssueReceipt } from '../api/domains/machineIdentities';
import { ApiError } from '../api/core';
import { useAuthStore } from '../stores';

function tenant(fallback: string) {
  try { return localStorage.getItem('current_tenant_id') || fallback; } catch { return fallback; }
}

export default function MachineIdentityControls({ agentId }: { agentId: string }) {
  const user = useAuthStore((state) => state.user);
  const [generation, setGeneration] = useState(0);
  useEffect(() => {
    const changed = (event: StorageEvent) => {
      if (!event.key || event.key === 'current_tenant_id') setGeneration((value) => value + 1);
    };
    window.addEventListener('storage', changed);
    return () => window.removeEventListener('storage', changed);
  }, []);
  const companyId = tenant(user?.tenant_id || '');
  if (!user || !companyId) return null;
  return <Access key={`${agentId}:${user.id}:${companyId}:${generation}`}
    agentId={agentId} actorId={user.id} tenantId={companyId} />;
}

function Access({ agentId, actorId, tenantId }: { agentId: string; actorId: string; tenantId: string }) {
  const { t } = useTranslation();
  const [name, setName] = useState('');
  const [expiry, setExpiry] = useState(() => {
    const date = new Date(Date.now() + 7 * 86400000);
    return new Date(date.getTime() - date.getTimezoneOffset() * 60000).toISOString().slice(0, 16);
  });
  const [busy, setBusy] = useState(false);
  const [unknown, setUnknown] = useState(false);
  const [formError, setFormError] = useState<string | null>(null);
  const [secret, setSecret] = useState<MachineIssueReceipt | null>(null);
  const [showSecret, setShowSecret] = useState(false);
  const [copied, setCopied] = useState(false);
  const [lost, setLost] = useState<string | null>(null);
  const pending = useRef<MachineIssue | null>(null);
  const matches = () => {
    const current = useAuthStore.getState().user;
    return current?.id === actorId && tenant(current?.tenant_id || '') === tenantId;
  };
  const query = useQuery({ queryKey: ['machine-identities', actorId, tenantId, agentId], retry: false,
    queryFn: async () => {
      const page = await machineIdentitiesApi.list(agentId);
      if (!matches() || page.agent_id !== agentId) throw new Error('Machine identity scope changed');
      return page;
    } });
  async function refresh() {
    const result = await query.refetch();
    if (!result.isError && matches()) {
      const issued = result.data?.identities.find((identity) => identity.issue_request_id === pending.current?.issue_request_id);
      if (issued) { setLost(issued.id);pending.current = null; }
      setUnknown(false);
    }
  }
  async function issue(event: FormEvent) {
    event.preventDefault();
    const date = new Date(expiry);
    if (!matches()) { setUnknown(true); return; }
    if (!pending.current && (!name.trim() || !Number.isFinite(date.getTime()) || date.getTime() <= Date.now())) {
      setFormError(t('machineAccess.validRequest', 'Enter an integration name and a future expiry time.'));return;
    }
    const request = pending.current || { name: name.trim(), expires_at: date.toISOString(), scopes: ['agent.describe'] as ['agent.describe'],
      issue_request_id: crypto.randomUUID(), expected_user_id: actorId, expected_tenant_id: tenantId };
    pending.current = request;
    setBusy(true);
    setFormError(null);
    setSecret(null);
    setShowSecret(false);setCopied(false);
    try {
      // A plaintext response never enters a query/mutation cache or AuthStore.
      const receipt = await machineIdentitiesApi.issue(agentId, request);
      if (!matches() || receipt.agent_id !== agentId || receipt.issue_request_id !== request.issue_request_id) throw new Error('Machine issue scope changed');
      if (receipt.token && receipt.secret_status === 'available_once') setSecret(receipt);
      else setLost(receipt.id);
      pending.current = null;
      await refresh();
    } catch (error) {
      if (error instanceof ApiError && error.status === 422 && error.data
        && (error.data as { code?: unknown }).code === 'machine_expiry_required') {
        pending.current = null;
        setFormError(t('machineAccess.validRequest', 'Enter an integration name and a future expiry time.'));
      } else setUnknown(true);
    } finally { setBusy(false); }
  }
  async function revoke(id: string) {
    if (!matches()) { setUnknown(true); return; }
    setBusy(true);
    try {
      const receipt = await machineIdentitiesApi.revoke(agentId, id, { expected_user_id: actorId, expected_tenant_id: tenantId });
      if (!matches() || receipt.id !== id || receipt.agent_id !== agentId || receipt.status !== 'revoked') throw new Error('Machine revoke scope changed');
      if (secret?.id === id) setSecret(null);
      if (lost === id) setLost(null);
      await refresh();
    } catch { setUnknown(true); } finally { setBusy(false); }
  }
  const statusLabels = { active: t('machineAccess.active', 'Active'), revoked: t('machineAccess.revoked', 'Revoked'),
    expired: t('machineAccess.expired', 'Expired'), needs_reissue: t('machineAccess.needsReissue', 'Needs a new credential'),
    unavailable: t('machineAccess.unavailable', 'Employee or owner unavailable') };
  return <section className="card card-pad-sm">
    <h3>{t('machineAccess.title', 'Machine access')}</h3>
    <p>{t('machineAccess.scope', 'Credentials can read this employee’s description. Task execution is not available yet.')}</p>
    {query.isPending && <p role="status">{t('common.loading', 'Loading…')}</p>}
    {(query.isError || unknown) && <p role="alert">{t('machineAccess.unknown', 'The access or request outcome could not be confirmed. Refresh before acting again.')}</p>}
    {lost && <p role="alert">{t('machineAccess.lost', 'This credential was already issued. Its secret cannot be recovered; revoke it before issuing another.')}</p>}
    {formError && <p role="alert">{formError}</p>}
    {query.data && <>
      {query.data.can_issue ? <form onSubmit={(event) => { void issue(event); }}>
        <label>{t('machineAccess.name', 'Integration name')}<input value={name} maxLength={200} required
          disabled={busy || unknown || !!lost || !!secret || !!pending.current} onChange={(event) => setName(event.target.value)} /></label>
        <label>{t('machineAccess.expiry', 'Expires at')}<input type="datetime-local" required value={expiry}
          disabled={busy || unknown || !!lost || !!secret || !!pending.current} onChange={(event) => setExpiry(event.target.value)} /></label>
        <button className="btn btn-primary" type="submit" disabled={busy || unknown || !!lost || !!secret || !name.trim()}>
          {pending.current ? t('machineAccess.retry', 'Retry issuance') : t('machineAccess.issue', 'Issue credential')}</button>
      </form> : <p>{t('machineAccess.admin', 'Ask a company administrator to issue a credential. The current owner can revoke existing access.')}</p>}
      {secret && <div>
        <p>{t('machineAccess.once', 'Save this new secret now. It is shown only for this issuance.')}</p>
        <label>{t('machineAccess.secret', 'New machine secret')}<input readOnly type={showSecret ? 'text' : 'password'} value={secret.token || ''} /></label>
        <label><input type="checkbox" checked={showSecret} onChange={(event) => setShowSecret(event.target.checked)} />
          {t('machineAccess.show', 'Show secret')}</label>
        <button className="btn btn-secondary" type="button" onClick={() => {
          if (matches() && secret.token) void navigator.clipboard?.writeText(secret.token).then(() => setCopied(true)).catch(() => setCopied(false));
        }}>{copied ? t('machineAccess.copied', 'Copied') : t('machineAccess.copy', 'Copy secret')}</button>
        <button className="btn btn-secondary" type="button" onClick={() => setSecret(null)}>{t('machineAccess.hide', 'Dismiss secret')}</button>
      </div>}
      <p>{t('machineAccess.endpoint', 'Employee description endpoint')}: <code>{machineIdentitiesApi.descriptionPath(agentId)}</code></p>
      {query.data.identities.map((identity) => <div key={identity.credential_id}>
        <p>{identity.name} · {statusLabels[identity.status] || identity.status}</p>
        <p>{t('machineAccess.expiry', 'Expires at')}: {new Date(identity.expires_at).toLocaleString()}</p>
        {identity.status !== 'revoked' && <button type="button" className="btn btn-secondary" disabled={busy || unknown}
          onClick={() => { void revoke(identity.id); }}>{t('machineAccess.revoke', 'Revoke access')}</button>}
      </div>)}
    </>}
    <button type="button" className="btn btn-secondary" disabled={busy || query.isFetching}
      onClick={() => { void refresh(); }}>{t('common.refresh', 'Refresh')}</button>
  </section>;
}
