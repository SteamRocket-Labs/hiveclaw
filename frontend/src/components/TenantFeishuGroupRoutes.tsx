import { useEffect, useId, useState } from 'react';
import { useQuery } from '@tanstack/react-query';
import { useTranslation } from 'react-i18next';
import { agentApi } from '../api/domains/agents';
import { channelApi, type TenantChannelScope, type TenantFeishuGroupRoute, type TenantFeishuGroupRoutes as RouteSettings } from '../api/domains/channels';

export default function TenantFeishuGroupRoutes({ scope, scopeKey }: { scope: TenantChannelScope; scopeKey: string }) {
  const { t } = useTranslation();
  const id = useId();
  const [open, setOpen] = useState(false);
  const [base, setBase] = useState<RouteSettings | null>(null);
  const [draft, setDraft] = useState<TenantFeishuGroupRoute[]>([]);
  const [busy, setBusy] = useState(false);
  const [outcome, setOutcome] = useState<'saved' | 'unknown' | null>(null);
  const query = useQuery({ queryKey: ['tenant-feishu-group-routes', scopeKey], enabled: open, retry: false,
    queryFn: async ({ signal }) => {
      const [settings, employees] = await Promise.all([channelApi.tenantFeishuGroupRoutes(scope, signal), agentApi.list(scope.expected_tenant_id)]);
      if (settings.tenant_id !== scope.expected_tenant_id) throw new Error('Company scope changed');
      return { settings, employees: employees.filter((employee) => employee.tenant_id === scope.expected_tenant_id) };
    } });
  useEffect(() => {
    if (query.data && base === null) { setBase(query.data.settings); setDraft(query.data.settings.routes); }
  }, [query.data, base]);
  async function refresh() {
    const result = await query.refetch();
    if (result.data && !result.isError) {
      if (base === null) setDraft(result.data.settings.routes);
      setBase(result.data.settings); setOutcome(null);
    }
  }
  async function save() {
    if (!base) return;
    setBusy(true); setOutcome(null);
    try {
      const saved = await channelApi.saveTenantFeishuGroupRoutes(scope, base.revision, draft);
      if (saved.tenant_id !== scope.expected_tenant_id) throw new Error('Company scope changed');
      setBase(saved); setDraft(saved.routes); setOutcome('saved'); await query.refetch();
    } catch { setOutcome('unknown'); } finally { setBusy(false); }
  }
  const changed = Boolean(base && query.data && base.revision !== query.data.settings.revision);
  return <details open={open} onToggle={(event) => setOpen(event.currentTarget.open)}>
    <summary>{t('tenantFeishuGroups.title', 'Company shared group work')}</summary>
    {open && <>
      <p>{t('tenantFeishuGroups.description', 'Choose one employee for each group. Each sender still needs verified company identity, current group membership and permission to use that employee. Private messages keep using My Assistant.')}</p>
      <p>{t('feishuSharedGroups.history', 'Existing private history stays private. Removing a group changes new message entry; accepted work and earlier shared history remain available to authorized members.')}</p>
      {query.isPending && <p role="status">{t('common.loading', 'Loading…')}</p>}
      {query.isError && <p role="alert">{t('feishuSharedGroups.readFailed', 'Could not read the current setting. Your draft is kept; refresh to retry.')}</p>}
      {(changed || outcome === 'unknown') && <p role="alert">{t('tenantFeishuGroups.review', 'Could not confirm these routes. Refresh, compare the saved routes with your draft, then save again if needed.')}</p>}
      {query.data && !query.isError && <p>{t('tenantFeishuGroups.savedRoutes', 'Saved routes')}: {query.data.settings.routes.map((row) => `${row.chat_id} → ${query.data.employees.find((employee) => employee.id === row.agent_id)?.name || row.agent_id}`).join(', ') || t('feishuSharedGroups.none', 'None')}</p>}
      <fieldset disabled={busy || !base} className="tenant-feishu-fields">
        {draft.map((row, index) => <div key={index}>
          <label htmlFor={`${id}-chat-${index}`}>{t('tenantFeishuGroups.groupId', 'Group ID')} {index + 1}</label>
          <input id={`${id}-chat-${index}`} className="form-input" value={row.chat_id} onChange={(event) => {
            setDraft(draft.map((item, at) => at === index ? { ...item, chat_id: event.target.value } : item)); setOutcome(null);
          }} />
          <label htmlFor={`${id}-employee-${index}`}>{t('tenantFeishuGroups.employee', 'Employee')} {index + 1}</label>
          <select id={`${id}-employee-${index}`} className="form-input" value={row.agent_id} onChange={(event) => {
            setDraft(draft.map((item, at) => at === index ? { ...item, agent_id: event.target.value } : item)); setOutcome(null);
          }}>
            <option value="">{t('tenantFeishuGroups.choose', 'Choose an employee')}</option>
            {row.agent_id && !query.data?.employees.some((employee) => employee.id === row.agent_id) && <option value={row.agent_id}>{row.agent_id}</option>}
            {query.data?.employees.map((employee) => <option key={employee.id} value={employee.id}>{employee.name}</option>)}
          </select>
          <button type="button" className="btn btn-secondary" onClick={() => { setDraft(draft.filter((_, at) => at !== index)); setOutcome(null); }}>{t('common.remove', 'Remove')}</button>
        </div>)}
        <button type="button" className="btn btn-secondary" onClick={() => { setDraft([...draft, { chat_id: '', agent_id: '' }]); setOutcome(null); }}>{t('tenantFeishuGroups.add', 'Add a group route')}</button>
      </fieldset>
      <div className="tenant-feishu-actions">
        <button type="button" className="btn btn-secondary" disabled={busy || query.isFetching} onClick={() => { void refresh(); }}>{t('common.refresh', 'Refresh')}</button>
        <button type="button" className="btn btn-primary" disabled={busy || !base || query.isFetching || query.isError || changed || draft.some((row) => !row.chat_id.trim() || !row.agent_id)} onClick={() => { void save(); }}>{t('tenantFeishuGroups.save', 'Save group routes')}</button>
      </div>
      {outcome === 'saved' && <p role="status">{t('tenantFeishuGroups.confirmed', 'Company group routes saved.')}</p>}
    </>}
  </details>;
}
