import { useState } from 'react';
import { useQuery, useQueryClient } from '@tanstack/react-query';
import { useTranslation } from 'react-i18next';
import { agentApi } from '../api/domains/agents';
import { triggerApi } from '../api/domains/triggers';
import { useAuthStore } from '../stores';
import { useAssistantTenant } from './assistant/assistantScope';
import { triggerScheduleLabel } from './agent-detail/wakePolicyForm';

export default function ScheduledWork({ agentId }: { agentId?: string }) {
  const { t } = useTranslation();
  const user = useAuthStore(state => state.user);
  const tenant = useAssistantTenant(user?.tenant_id || null);
  const client = useQueryClient();
  const queryKey = ['my-scheduled-work', user?.id, tenant, agentId];
  const [stopping, setStopping] = useState<string>();
  const [error, setError] = useState(false);
  const tasks = useQuery({ queryKey, enabled: !!user?.id && !!tenant, queryFn: async () => {
    const agents = agentId ? [await agentApi.getById(agentId)] : await agentApi.list();
    const readable = agents.filter(agent => agent.access_level !== 'operator');
    const lists = await Promise.allSettled(readable.map(async agent =>
      (await triggerApi.list(agent.id)).filter(task => task.config?.created_by === user?.id
      ).map(task => ({ ...task, agent }))));
    return {
      items: lists.flatMap(result => result.status === 'fulfilled' ? result.value : []),
      unavailable: readable.filter((_, index) => lists[index].status === 'rejected'),
    };
  } });
  return <section>
    {!agentId && <header className="swb-header"><h2>{t('memberSettings.schedules')}</h2></header>}
    {(tasks.isError || error) && <p role="alert">{t('memberSettings.failed')}</p>}
    {tasks.isPending && <p>{t('common.loading')}</p>}
    {tasks.data?.unavailable.map(agent => <p role="alert" key={agent.id}>{t('memberSettings.botFailed', { name: agent.name })}</p>)}
    {tasks.data?.items.length === 0 && tasks.data.unavailable.length === 0 && <p>{t('memberSettings.noSchedules')}</p>}
    {tasks.data?.items.map(task => {
      const schedule = triggerScheduleLabel(task, t);
      return <section className="card" key={task.id}>
      <strong>{task.display_title || task.name}</strong>
      <p>{task.agent.name} · {task.type === 'webhook' ? t('memberSettings.onRequest')
        : schedule && schedule !== task.type ? schedule : t('agent.aware.scheduleUnavailable')}</p>
      {task.is_enabled ? <button className="btn btn-secondary" type="button" disabled={!!stopping}
        onClick={async () => {
          setStopping(task.id); setError(false);
          try {
            await triggerApi.update(task.agent.id, task.id, { is_enabled: false });
            await client.invalidateQueries({ queryKey });
          } catch { setError(true); }
          finally { setStopping(undefined); }
        }}>{t('memberSettings.stop')}</button> : <span>{t('memberSettings.stopped')}</span>}
    </section>;
    })}
  </section>;
}
