import { useEffect, useRef, useState } from 'react';
import { useTranslation } from 'react-i18next';
import { extensionsApi, type McpAccount } from '../api/domains/extensions';
import McpOAuthSettings from './McpOAuthSettings';

export default function McpAccounts({ agentId, serverId }: { agentId: string; serverId: string }) {
  const { t } = useTranslation();
  const [accounts, setAccounts] = useState<McpAccount[]>([]);
  const [revision, setRevision] = useState(0);
  const [error, setError] = useState(false);
  const [busy, setBusy] = useState(false);
  const creationId = useRef(crypto.randomUUID());
  const review = () => setRevision((current) => current + 1);
  useEffect(() => {
    let active = true; setError(false);
    void extensionsApi.listMcpAccounts(agentId, serverId).then((data) => { if (active) setAccounts(data); })
      .catch(() => { if (active) setError(true); });
    return () => { active = false; };
  }, [agentId, serverId, revision]);
  const personal = accounts.filter((account) => account.owner_user_id);
  return <section>
    <p>{t('agent.mcpConnect.scope')}</p>
    {error && <p role="alert">{t('agent.mcpConnect.failed')}</p>}
    {accounts.filter((account) => account.owner_user_id || account.can_authorize).map((account) => <section key={account.id}>
      <strong>{account.name}</strong>
      <McpOAuthSettings key={`${account.id}:${account.auth_status}`} serverId={account.id} personal={!!account.owner_user_id}
        onReviewed={review} onBusyChange={setBusy} disabled={busy} />
      {account.owner_user_id && <button className="btn btn-secondary" type="button" disabled={busy} onClick={async () => {
        setBusy(true); setError(false);
        try { await extensionsApi.revokeMcpAccount(account.id); review(); }
        catch { setError(true); }
        finally { setBusy(false); }
      }}>{t('agent.mcpConnect.remove')}</button>}
    </section>)}
    {!personal.length && accounts.length > 0 && <button className="btn btn-secondary" disabled={busy} onClick={async () => {
      setBusy(true); setError(false);
      try {
        await extensionsApi.createMcpAccount(agentId, serverId, { id: creationId.current, name: accounts[0].name });
        review();
      } catch { setError(true); }
      finally { setBusy(false); }
    }}>{t('agent.mcpConnect.connect')}</button>}
  </section>;
}
