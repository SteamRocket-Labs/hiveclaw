import { useEffect, useId, useRef, useState } from 'react';
import { useTranslation } from 'react-i18next';
import { extensionsApi, type McpAccount } from '../api/domains/extensions';
import McpOAuthSettings from './McpOAuthSettings';

export function McpAccountControls({ agentId, serverId, accounts, accountId, onSelect, onReviewed, onBusyChange, disabled = false }: {
  agentId: string; serverId: string; accounts: McpAccount[]; accountId: string;
  onSelect: (id: string) => void | Promise<unknown>; onReviewed: () => void; onBusyChange?: (busy: boolean) => void; disabled?: boolean;
}) {
  const { t } = useTranslation();
  const formId = useId();
  const [creating, setCreating] = useState(false);
  const [name, setName] = useState('');
  const creationId = useRef(crypto.randomUUID());
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const [revoking, setRevoking] = useState(false);
  const selected = accounts.find((account) => account.id === accountId);
  const mutate = async (action: () => Promise<unknown>) => {
    if (busy || disabled) return;
    setBusy(true); onBusyChange?.(true); setError('');
    try { await action(); onReviewed(); }
    catch (failure) { setError(failure instanceof Error ? failure.message : t('agent.mcpAccounts.failed')); }
    finally { setBusy(false); onBusyChange?.(false); }
  };
  return <section className="mcp-account-controls" aria-label={t('agent.mcpAccounts.title')}>
    <label htmlFor={`${formId}-selection`}>{t('agent.mcpAccounts.choose')}</label>
    <select className="input" id={`${formId}-selection`} value={accountId} disabled={busy || disabled} onChange={(event) => {
      setRevoking(false); const id = event.target.value; void mutate(async () => { await onSelect(id); });
    }}>{accounts.map((account) => <option key={account.id} value={account.id}>
      {account.owner_user_id ? t('agent.mcpAccounts.personal') : t('agent.mcpAccounts.company')} · {account.name}
    </option>)}</select>
    <div className="mcp-configuration-actions">
      <button className="btn btn-secondary" type="button" disabled={busy || disabled} onClick={() => {
        if (creating) { setName(''); creationId.current = crypto.randomUUID(); }
        setCreating(!creating); setError('');
      }}>{creating ? t('common.cancel') : t('agent.mcpAccounts.add')}</button>
      {selected?.owner_user_id && selected.can_grant_account && <button className="btn btn-secondary" type="button" disabled={busy || disabled}
        onClick={() => void mutate(() => extensionsApi.setMcpAccountGrant(selected.id, agentId, selected.owner_user_id!, !selected.account_use_enabled, selected.grant_version ?? 0))}>
        {selected.account_use_enabled ? t('agent.mcpAccounts.removeUse') : t('agent.mcpAccounts.grantUse')}
      </button>}
      {selected?.owner_user_id && selected.can_authorize && <button className="btn btn-secondary" type="button" disabled={busy || disabled}
        onClick={() => setRevoking(true)}>{t('agent.mcpAccounts.revoke')}</button>}
    </div>
    {selected?.owner_user_id && <p>{selected.account_use_enabled ? t('agent.mcpAccounts.granted') : t('agent.mcpAccounts.notGranted')}</p>}
    {creating && <form onSubmit={(event) => {
      event.preventDefault();
      if (!name.trim()) return;
      void mutate(async () => {
        let created: McpAccount;
        try { created = await extensionsApi.createMcpAccount(agentId, serverId, { id: creationId.current, name: name.trim() }); }
        catch (failure) {
          const current = await extensionsApi.listMcpAccounts(agentId, serverId);
          const recorded = current.find((account) => account.id === creationId.current && account.name === name.trim());
          if (!recorded) throw failure;
          created = recorded;
        }
        await onSelect(created.id); setCreating(false); setName(''); creationId.current = crypto.randomUUID();
      });
    }}>
      <label htmlFor={`${formId}-name`}>{t('agent.mcpAccounts.name')}</label>
      <input className="input" id={`${formId}-name`} value={name} maxLength={200} required disabled={busy || disabled} onChange={(event) => setName(event.target.value)} />
      <button className="btn btn-primary" type="submit" disabled={busy || disabled}>{t('agent.mcpAccounts.create')}</button>
    </form>}
    {revoking && selected && <div role="alert">
      <p>{t('agent.mcpAccounts.revokeNotice')}</p>
      <button className="btn btn-secondary" type="button" disabled={busy || disabled} onClick={() => setRevoking(false)}>{t('common.cancel')}</button>
      <button className="btn btn-primary" type="button" disabled={busy || disabled} onClick={() => void mutate(async () => {
        await extensionsApi.revokeMcpAccount(selected.id); setRevoking(false);
      })}>{t('agent.mcpAccounts.confirmRevoke')}</button>
    </div>}
    {error && <p role="alert">{error}</p>}
  </section>;
}

export default function McpAccounts({ agentId, serverId }: { agentId: string; serverId: string }) {
  const { t } = useTranslation();
  const [accounts, setAccounts] = useState<McpAccount[]>([]);
  const [selectedId, setSelectedId] = useState(serverId);
  const [revision, setRevision] = useState(0);
  const [error, setError] = useState(false);
  const [busy, setBusy] = useState(false);
  useEffect(() => {
    let active = true; setError(false);
    void extensionsApi.listMcpAccounts(agentId, serverId).then((data) => { if (active) setAccounts(data); })
      .catch(() => { if (active) setError(true); });
    return () => { active = false; };
  }, [agentId, serverId, revision]);
  const selected = accounts.find((account) => account.id === selectedId);
  const review = () => setRevision((current) => current + 1);
  return <section>
    <p>{t('agent.mcpAccounts.profileNotice')}</p>
    {error && <p role="alert">{t('agent.mcpAccounts.failed')}</p>}
    {!!accounts.length && !selected && <p role="alert">{t('agent.mcpAccounts.selectionUnavailable')}</p>}
    {!!accounts.length && <McpAccountControls agentId={agentId} serverId={serverId} accounts={accounts}
      accountId={selectedId} onSelect={setSelectedId} onReviewed={review} onBusyChange={setBusy} disabled={busy} />}
    {selected?.can_authorize && <McpOAuthSettings key={`${selected.id}:${selected.grant_version ?? 0}:${selected.auth_status}`} serverId={selected.id} personal={!!selected.owner_user_id}
      onReviewed={review} onBusyChange={setBusy} disabled={busy} />}
    <button className="btn btn-secondary" type="button" disabled={busy} onClick={review}>{t('agent.configuration.review')}</button>
  </section>;
}
