import { useState } from 'react';
import { useQuery, useQueryClient } from '@tanstack/react-query';
import { useTranslation } from 'react-i18next';
import { vaultApi, type VaultRemoval, type VaultRow } from '../api/domains/vault';
import Modal from '../components/ui/Modal';
import { useAuthStore } from '../stores';
import { useAssistantTenant } from './assistant/assistantScope';
import './Connections.css';

export default function Connections() {
  const user = useAuthStore((state) => state.user);
  const tenant = useAssistantTenant(user?.tenant_id || null);
  return <VaultList key={`${user?.id}:${tenant}`} userId={user?.id} tenant={tenant} />;
}

function VaultList({ userId, tenant }: { userId?: string; tenant: string | null }) {
  const { t, i18n } = useTranslation();
  const client = useQueryClient();
  const queryKey = ['vault', userId, tenant];
  const rows = useQuery({ queryKey, queryFn: vaultApi.list, enabled: !!userId && !!tenant });
  const [editing, setEditing] = useState<{ row: VaultRow; remove: boolean }>();
  const [values, setValues] = useState<Record<string, string>>({});
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState(false);
  const [message, setMessage] = useState('');
  const fieldLabel = (field: VaultRow['fields'][number]) => {
    const labels: Record<string, string> = { access_token: 'token', bearer_token: 'token', api_key: 'apiKey',
      auth_code: 'password', basic_password: 'password', basic_username: 'username', client_secret: 'appSecret' };
    return labels[field.key] ? t(`vault.labels.${labels[field.key]}`) : field.label || t('vault.credential');
  };
  const effect = (removal: VaultRemoval, count: number) => (removal.fallback === 'company' && count
    ? t(count === 1 ? 'vault.removal.companySingle' : 'vault.removal.companyMultiple') : t(`vault.removal.${removal.fallback}`))
    + (removal.review_required ? ` ${t('vault.reviewRequired')}` : '');
  const close = () => { if (!busy) { setEditing(undefined); setValues({}); setError(false); } };
  const open = (row: VaultRow, remove: boolean) => { setValues({}); setError(false); setMessage(''); setEditing({ row, remove }); };
  const submit = async () => {
    if (!editing || busy) return;
    setBusy(true); setError(false);
    try {
      const result = await (editing.remove ? vaultApi.remove(editing.row) : vaultApi.replace(editing.row, values));
      setValues({}); setEditing(undefined);
      setMessage(result.removed ? effect(result.removal, editing.row.bots.length) : t('vault.saved') + (result.removal.review_required ? ` ${t('vault.reviewRequired')}` : ''));
      await client.invalidateQueries({ queryKey });
    } catch { setError(true); }
    finally { setBusy(false); }
  };
  return <section className="vault-page">
    <header className="swb-header"><h1>{t('vault.title')}</h1></header>
    <p className="vault-intro">{t('vault.description')}</p>
    {message && <p role="status">{message}</p>}
    {(rows.isError || error && !editing) && <p role="alert">{t('vault.failed')}</p>}
    {rows.isPending && <p>{t('common.loading')}</p>}
    {rows.data?.length === 0 && <p>{t('vault.empty')}</p>}
    <div className="vault-list">
      {rows.data?.map((row) => <article className="vault-row" key={`${row.kind}:${row.id}:${row.field || ''}`}>
        <div className="vault-row-copy">
          <h2>{row.name}</h2>
          {row.field && <p>{fieldLabel(row.fields[0])}</p>}
          <p>{row.bots.length ? row.bots.map((bot) => bot.name).join(i18n.language.startsWith('zh') ? '、' : ', ') : t('vault.unassigned')}</p>
          <div className="vault-facts"><span>{t(`vault.scope.${row.scope}`)}</span><span>{t(row.configured ? 'vault.configured' : 'vault.notConfigured')}</span></div>
        </div>
        {!row.read_only && <div className="vault-actions">
          <button className="btn btn-secondary" type="button" onClick={() => open(row, false)}>{t('vault.replace')}</button>
          <button className="btn btn-ghost" type="button" onClick={() => open(row, true)}>{t('vault.remove')}</button>
        </div>}
      </article>)}
    </div>
    <Modal open={!!editing} onClose={close} title={t(editing?.remove ? 'vault.removeTitle' : 'vault.replaceTitle')} ariaLabel={t(editing?.remove ? 'vault.removeTitle' : 'vault.replaceTitle')}>
      {editing && <form onSubmit={(event) => { event.preventDefault(); void submit(); }}>
        <p className="vault-dialog-name">{editing.row.name}</p>
        {editing.remove ? <p>{editing.row.kind === 'bot' ? t('vault.removeOwnCredential') : effect(editing.row.removal, editing.row.bots.length)}</p> : <>
          <p>{t('vault.secureInput')}</p>
          {editing.row.fields.map((field, index) => <label className="vault-field" key={field.key} htmlFor={`vault-secret-${index}`}>
            {editing.row.fields.length === 1 ? t('vault.credential') : fieldLabel(field)}
            <input id={`vault-secret-${index}`} className="input" type="password" autoComplete="new-password"
              maxLength={editing.row.kind === 'mcp' ? 16384 : undefined}
              required disabled={busy} value={values[field.key] || ''}
              onChange={(event) => setValues((current) => ({ ...current, [field.key]: event.target.value }))} />
          </label>)}
        </>}
        {error && <p role="alert">{t('vault.failed')}</p>}
        <div className="vault-dialog-actions">
          <button className="btn btn-secondary" type="button" disabled={busy} onClick={close}>{t('common.cancel')}</button>
          <button className={`btn ${editing.remove ? 'btn-danger' : 'btn-primary'}`} type="submit" disabled={busy}>{busy ? t('common.loading') : t(editing.remove ? 'vault.remove' : 'common.save')}</button>
        </div>
      </form>}
    </Modal>
  </section>;
}
