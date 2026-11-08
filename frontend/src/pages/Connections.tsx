import { useState } from 'react';
import { useQuery, useQueryClient } from '@tanstack/react-query';
import { useTranslation } from 'react-i18next';
import { extensionsApi } from '../api/domains/extensions';
import { useAuthStore } from '../stores';
import { useAssistantTenant } from './assistant/assistantScope';

export default function Connections() {
  const { t } = useTranslation();
  const client = useQueryClient();
  const user = useAuthStore((state) => state.user);
  const tenant = useAssistantTenant(user?.tenant_id || null);
  const queryKey = ['my-mcp-connections', user?.id, tenant];
  const [removing, setRemoving] = useState<string>();
  const [error, setError] = useState(false);
  const accounts = useQuery({ queryKey, queryFn: extensionsApi.listMyMcpAccounts, enabled: !!user?.id && !!tenant });
  return <section>
    <header className="swb-header"><h1>{t('agent.mcpConnect.title')}</h1></header>
    <p>{t('agent.mcpConnect.scope')}</p>
    {(accounts.isError || error) && <p role="alert">{t('agent.mcpConnect.failed')}</p>}
    {accounts.isPending && <p>{t('common.loading')}</p>}
    {accounts.data?.length === 0 && <p>{t('agent.mcpConnect.empty')}</p>}
    {accounts.data?.map((account) => <section className="card" key={account.id}>
      <strong>{account.name}</strong>
      <p>{t('memberSettings.account', { name: user?.display_name || user?.email || '' })}</p>
      <button className="btn btn-secondary" type="button" disabled={!!removing} onClick={async () => {
        setRemoving(account.id); setError(false);
        try {
          await extensionsApi.revokeMcpAccount(account.id);
          await client.invalidateQueries({ queryKey });
        } catch { setError(true); }
        finally { setRemoving(undefined); }
      }}>{t('agent.mcpConnect.remove')}</button>
    </section>)}
  </section>;
}
