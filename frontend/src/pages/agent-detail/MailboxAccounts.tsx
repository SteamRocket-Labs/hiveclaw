import { useEffect, useId, useRef, useState } from 'react';
import { useTranslation } from 'react-i18next';
import { toolsApi, type AgentTool, type MailboxAccount } from '../../api/domains/tools';
import { useAuthStore } from '../../stores';
import ToolConfigurationForm from './ToolConfigurationForm';

export default function MailboxAccounts({ agentId, tool, canManageCompany, onSaveCompany, onConfirm, onBusyChange, initialAccountId, onSelectionChange }: {
  agentId: string; tool: AgentTool; canManageCompany: boolean;
  onSaveCompany: (config: Record<string, unknown>) => Promise<void>;
  onConfirm?: (account: MailboxAccount) => Promise<void>; onBusyChange?: (busy: boolean) => void;
  initialAccountId?: string | null; onSelectionChange?: () => void;
}) {
  const { t } = useTranslation();
  const userId = useAuthStore((state) => state.user?.id);
  const formId = useId();
  const [accounts, setAccounts] = useState<MailboxAccount[]>([]);
  const [selectedId, setSelectedId] = useState(initialAccountId || 'company');
  useEffect(() => { setSelectedId(initialAccountId || 'company'); }, [initialAccountId]);
  const [creating, setCreating] = useState(false);
  const [name, setName] = useState('');
  const createId = useRef(crypto.randomUUID());
  const [editing, setEditing] = useState<MailboxAccount | 'company' | null>(null);
  const [revoking, setRevoking] = useState(false);
  const [busy, setBusy] = useState(false);
  const fence = useRef(false);
  const loadEpoch = useRef(0);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState('');
  const load = async () => {
    const epoch = ++loadEpoch.current; setLoading(true);
    try { const current = await toolsApi.listMailboxes(agentId); if (epoch === loadEpoch.current) setAccounts(current); }
    catch (failure) { if (epoch === loadEpoch.current) setError(failure instanceof Error ? failure.message : t('agent.mcpAccounts.failed')); }
    finally { if (epoch === loadEpoch.current) setLoading(false); }
  };
  useEffect(() => { void load(); return () => { loadEpoch.current++; }; }, [agentId]);
  const mutate = async (action: () => Promise<void>) => {
    if (fence.current) return;
    fence.current = true; setBusy(true); onBusyChange?.(true); setError('');
    try { await action(); await load(); }
    catch (failure) { setError(failure instanceof Error ? failure.message : t('agent.mcpAccounts.failed')); }
    finally { fence.current = false; setBusy(false); onBusyChange?.(false); }
  };
  const selected = accounts.find((account) => (account.id || 'company') === selectedId);
  const owned = !!selected?.owner_user_id && selected.owner_user_id === userId;
  return <section className="card" aria-label={t('agent.mailboxAccounts.title')}>
    <h4>{t('agent.mailboxAccounts.title')}</h4>
    <p>{t('agent.mcpAccounts.profileNotice')}</p>
    {loading && <p>{t('common.loading')}</p>}
    {error && <p role="alert">{error}</p>}
    <label htmlFor={`${formId}-account`}>{t('agent.mcpAccounts.choose')}</label>
    <select className="input" id={`${formId}-account`} value={selectedId} disabled={busy || loading || !!editing} onChange={(event) => {
      setSelectedId(event.target.value); setRevoking(false); onSelectionChange?.();
    }}>
      {!selected && <option value={selectedId}>{t('agent.mcpAccounts.selectionUnavailable')}</option>}
      {accounts.map((account) => <option key={account.id || 'company'} value={account.id || 'company'}>
        {account.owner_user_id ? `${t('agent.mcpAccounts.personal')} · ${account.name}` : t('agent.mcpAccounts.company')}
      </option>)}
    </select>
    {selected?.owner_user_id && <p>{t(selected.account_use_enabled ? 'agent.mcpAccounts.granted' : 'agent.mcpAccounts.notGranted')}</p>}
    <div className="mcp-configuration-actions">
      <button className="btn btn-secondary" type="button" disabled={busy || !!editing} onClick={() => {
        if (creating) { setName(''); createId.current = crypto.randomUUID(); }
        setCreating(!creating);
        onSelectionChange?.();
      }}>{creating ? t('common.cancel') : t('agent.mcpAccounts.add')}</button>
      {selected && (owned || !selected.owner_user_id && canManageCompany) && <button className="btn btn-secondary" disabled={busy || loading || !!editing}
        onClick={() => { onSelectionChange?.(); void mutate(async () => setEditing(selected.id ? await toolsApi.getMailbox(selected.id) : 'company')); }}>{t('common.edit')}</button>}
      {owned && selected?.id && userId && <button className="btn btn-secondary" disabled={busy || loading || !!editing} onClick={() => void mutate(async () => {
        onSelectionChange?.();
        await toolsApi.setMailboxGrant(selected.id!, agentId, userId, !selected.account_use_enabled, selected.grant_version || 0);
      })}>{t(selected.account_use_enabled ? 'agent.mcpAccounts.removeUse' : 'agent.mcpAccounts.grantUse')}</button>}
      {owned && <button className="btn btn-secondary" disabled={busy || !!editing} onClick={() => { setRevoking(true); onSelectionChange?.(); }}>{t('agent.mcpAccounts.revoke')}</button>}
      <button className="btn btn-secondary" disabled={busy || loading} onClick={() => void load()}>{t('agent.configuration.review')}</button>
    </div>
    {creating && <form onSubmit={(event) => {
      event.preventDefault(); if (!name.trim()) return;
      void mutate(async () => {
        let created: MailboxAccount;
        try { created = await toolsApi.createMailbox(agentId, createId.current, name.trim()); }
        catch (failure) {
          const current = await toolsApi.listMailboxes(agentId);
          const recorded = current.find((account) => account.id === createId.current && account.name === name.trim());
          if (!recorded) throw failure;
          created = recorded;
        }
        setSelectedId(created.id!); setCreating(false); setName(''); createId.current = crypto.randomUUID();
        setEditing(await toolsApi.getMailbox(created.id!));
      });
    }}>
      <label htmlFor={`${formId}-name`}>{t('agent.mcpAccounts.name')}</label>
      <input className="input" id={`${formId}-name`} value={name} required maxLength={200} disabled={busy} onChange={(event) => setName(event.target.value)} />
      <button className="btn btn-primary" disabled={busy}>{t('agent.mcpAccounts.create')}</button>
    </form>}
    {editing && <ToolConfigurationForm key={editing === 'company' ? 'company' : editing.id} tool={editing === 'company' ? tool : {
      ...tool, global_config: {}, agent_config: editing.config || {}, config_schema: editing.config_schema || tool.config_schema,
    }} onCancel={() => setEditing(null)} onSave={async (config) => {
      if (fence.current) throw new Error(t('common.loading'));
      fence.current = true; setBusy(true); onBusyChange?.(true);
      try {
        if (editing === 'company') await onSaveCompany(config);
        else await toolsApi.saveMailbox(editing.id!, editing.version!, config, agentId);
        setEditing(null); await load();
      } finally { fence.current = false; setBusy(false); onBusyChange?.(false); }
    }} />}
    {revoking && selected?.id && <div role="alert">
      <p>{t('agent.mcpAccounts.revokeNotice')}</p>
      <button className="btn btn-secondary" disabled={busy} onClick={() => setRevoking(false)}>{t('common.cancel')}</button>
      <button className="btn btn-primary" disabled={busy} onClick={() => void mutate(async () => {
        await toolsApi.revokeMailbox(selected.id!, selected.version!); setRevoking(false);
      })}>{t('agent.mcpAccounts.confirmRevoke')}</button>
    </div>}
    {onConfirm && <button className="btn btn-primary" disabled={busy || loading || !!editing || !selected?.is_configured || !selected.account_use_enabled}
      onClick={() => selected && void mutate(() => onConfirm(selected))}>{t('agent.mailboxAccounts.confirm')}</button>}
  </section>;
}
