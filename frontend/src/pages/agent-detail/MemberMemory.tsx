import { useState } from 'react';
import { useQuery, useQueryClient } from '@tanstack/react-query';
import { useTranslation } from 'react-i18next';
import { memoryApi } from '../../api/domains/memory';
import { useAuthStore } from '../../stores';
import { useAssistantTenant } from '../assistant/assistantScope';

export default function MemberMemory({ agentId }: { agentId: string }) {
  const { t } = useTranslation();
  const user = useAuthStore(state => state.user);
  const tenant = useAssistantTenant(user?.tenant_id || null);
  const client = useQueryClient();
  const queryKey = ['my-bot-memory', user?.id, tenant, agentId];
  const entries = useQuery({ queryKey, enabled: !!user?.id && !!tenant, queryFn: () => memoryApi.listOwn(agentId) });
  const [removing, setRemoving] = useState<string>();
  const [error, setError] = useState(false);
  return <section>
    <h2>{t('memberSettings.memory')}</h2>
    <p>{t('memberSettings.memoryHint')}</p>
    {(entries.isError || error) && <p role="alert">{t('memberSettings.failed')}</p>}
    {entries.isPending && <p>{t('common.loading')}</p>}
    {entries.data?.length === 0 && <p>{t('memberSettings.noMemory')}</p>}
    {entries.data?.map(entry => <section className="card" key={entry.id}>
      <p>{entry.content}</p>
      <button className="btn btn-secondary" type="button" disabled={!!removing} onClick={async () => {
        setRemoving(entry.id); setError(false);
        try {
          await memoryApi.deleteOwn(agentId, entry.id);
          await Promise.all([client.invalidateQueries({ queryKey }),
            client.invalidateQueries({ queryKey: ['knowledge-overview', agentId] })]);
        }
        catch { setError(true); }
        finally { setRemoving(undefined); }
      }}>{t('common.delete')}</button>
    </section>)}
  </section>;
}
