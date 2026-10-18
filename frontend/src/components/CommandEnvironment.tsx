import { useEffect, useState } from 'react';
import { useQuery, useQueryClient } from '@tanstack/react-query';
import { useTranslation } from 'react-i18next';
import { environmentApi, type EnvironmentScope, type EnvironmentView } from '../api/domains/environments';
import { useAuthStore } from '../stores';

export default function CommandEnvironment({ agentId }: { agentId: string }) {
  const user = useAuthStore((state) => state.user);
  const [generation, setGeneration] = useState(0);
  useEffect(() => {
    const changed = (event: StorageEvent) => { if (!event.key || event.key === 'current_tenant_id') setGeneration((value) => value + 1); };
    window.addEventListener('storage', changed);
    return () => window.removeEventListener('storage', changed);
  }, []);
  let tenant = user?.tenant_id || '';
  try { tenant = localStorage.getItem('current_tenant_id') || tenant; } catch { /* Storage is optional. */ }
  if (!user || !tenant) return null;
  const key = `${user.id}:${user.role}:${tenant}:${agentId}:${generation}`;
  return <ResourceEditor key={key} agentId={agentId} scopeKey={key} scope={{ expected_tenant_id: tenant, expected_user_id: user.id }} />;
}

function ResourceEditor({ agentId, scopeKey, scope }: { agentId: string; scopeKey: string; scope: EnvironmentScope }) {
  const { t } = useTranslation();
  const client = useQueryClient();
  const [open, setOpen] = useState(false);
  const [busy, setBusy] = useState(false);
  const [unknown, setUnknown] = useState(false);
  const key = ['command-environment', scopeKey];
  function check(value: EnvironmentView) {
    if (value.agent_id !== agentId || value.tenant_id !== scope.expected_tenant_id) throw new Error('Resource scope changed');
    return value;
  }
  const query = useQuery({ queryKey: key, enabled: open, retry: false,
    queryFn: async ({ signal }) => check(await environmentApi.read(agentId, scope, signal)) });
  async function refresh() { const result = await query.refetch(); if (!result.isError) setUnknown(false); }
  async function control(action: 'stop' | 'resume') {
    if (!query.data?.environment) return;
    setBusy(true);
    try {
      const value = check(await environmentApi.control(agentId, scope, action, query.data.environment.revision));
      client.setQueryData(key, value); await refresh();
    } catch { setUnknown(true); } finally { setBusy(false); }
  }
  const view = !query.isError && !unknown ? query.data : undefined;
  const env = view?.environment;
  function stateLabel() {
    switch (env?.observed_state) {
      case 'ready': return t('commandEnvironment.ready', 'Ready for supervised commands');
      case 'stopping': return t('commandEnvironment.stopping', 'Stopping: waiting for active commands and work settlement');
      case 'stopped': return t('commandEnvironment.stopped', 'Stopped');
      case 'lost': return t('commandEnvironment.lost', 'Resource completion is unknown. Inspect the original work or ask an administrator before resuming.');
      case 'failed': return t('commandEnvironment.failed', 'The command resource is unavailable. Review its configuration before retrying.');
      default: return t('commandEnvironment.notStarted', 'The first authorized command creates this private logical environment.');
    }
  }
  return <details className="card" open={open} onToggle={(event) => setOpen(event.currentTarget.open)}>
    <summary>{t('commandEnvironment.title', 'Command environment')}</summary>
    {open && <>
      <p>{t('commandEnvironment.scope', 'Controls running commands. This profile does not provide a persistent computer, background service management or browser control. Saved work remains in the employee workspace.')}</p>
      {query.isPending && <p role="status">{t('common.loading', 'Loading…')}</p>}
      {(query.isError || unknown) && <p role="alert">{t('commandEnvironment.unknown', 'Could not confirm the current resource state. Refresh before trying another action.')}</p>}
      {view && <>
        <p role="status">{stateLabel()}</p>
        {env && <p>{t('commandEnvironment.provider', 'Provider')}: {env.provider_key} · {t('commandEnvironment.generation', 'Generation')}: {env.generation} · {t('commandEnvironment.active', 'Active commands')}: {env.active_commands}</p>}
        {env && view.can_manage && <div className="agent-settings-header-actions">
          <button type="button" className="btn btn-secondary" disabled={busy || query.isFetching || env.desired_state === 'stopped'} onClick={() => { void control('stop'); }}>{t('commandEnvironment.stop', 'Stop supervised commands')}</button>
          <button type="button" className="btn btn-primary" disabled={busy || query.isFetching || env.active_commands > 0 || env.observed_state === 'ready'} onClick={() => { void control('resume'); }}>{t('commandEnvironment.resume', 'Resume command environment')}</button>
        </div>}
      </>}
      <button type="button" className="btn btn-secondary" disabled={busy || query.isFetching} onClick={() => { void refresh(); }}>{t('common.refresh', 'Refresh')}</button>
    </>}
  </details>;
}
