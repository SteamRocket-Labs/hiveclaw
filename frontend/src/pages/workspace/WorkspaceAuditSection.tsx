import { useState } from 'react';
import { useQuery } from '@tanstack/react-query';
import { useTranslation } from 'react-i18next';

import { enterpriseApi, type AuditLog } from '../../api/domains/enterprise';
import { usersApi } from '../../api/domains/users';

interface WorkspaceAuditSectionProps {
  selectedTenantId: string;
}

type AuditFilter = 'all' | 'background' | 'actions';
const BACKGROUND_ACTIONS = ['schedule_tick', 'schedule_fire', 'schedule_error', 'heartbeat_tick', 'heartbeat_fire', 'heartbeat_error', 'server_startup'];

export default function WorkspaceAuditSection({ selectedTenantId }: WorkspaceAuditSectionProps) {
  const { t } = useTranslation();
  const [auditFilter, setAuditFilter] = useState<AuditFilter>('all');
  const { data: auditLogs = [] } = useQuery({
    queryKey: ['audit-logs', selectedTenantId],
    queryFn: () => enterpriseApi.getAuditLogs(`limit=200${selectedTenantId ? `&tenant_id=${selectedTenantId}` : ''}`),
  });
  const { data: securityAuditEvents } = useQuery({
    queryKey: ['security-audit-events', selectedTenantId],
    queryFn: () => enterpriseApi.getSecurityAuditEvents(selectedTenantId),
  });
  const { data: users = [] } = useQuery({
    queryKey: ['users', selectedTenantId, 'audit-display'],
    queryFn: () => usersApi.list(selectedTenantId || undefined),
  });
  const userName = (id: unknown) => users.find((user) => user.id === id)?.display_name
    || users.find((user) => user.id === id)?.email || t('enterprise.audit.unknownMember', 'Unknown member');
  const roleName = (role: unknown) => role === 'org_admin'
    ? t('userManagement.roleAdmin', 'Company administrator') : role === 'member'
      ? t('userManagement.roleMember', 'Member') : t('enterprise.audit.unknownRole', 'Unknown role');
  const actionLabels: Record<string, string> = {
    'user:offboarded': t('enterprise.audit.offboarded', 'Member offboarded'),
    'user:role_changed': t('enterprise.audit.roleChanged', 'Role changed'),
    schedule_tick: t('enterprise.audit.scheduleCheck', 'Scheduled task check'),
    schedule_fire: t('enterprise.audit.scheduleRun', 'Scheduled task run'),
    schedule_error: t('enterprise.audit.scheduleError', 'Scheduled task failed'),
    heartbeat_tick: t('enterprise.audit.periodicCheck', 'Periodic check'),
    heartbeat_fire: t('enterprise.audit.periodicRun', 'Periodic work'),
    heartbeat_error: t('enterprise.audit.periodicError', 'Periodic work failed'),
    server_startup: t('enterprise.audit.startup', 'Service started'),
    test_llm_model_completed: t('enterprise.audit.modelTest', 'Model test'),
  };
  const combinedAuditLogs: AuditLog[] = [
    ...auditLogs,
    ...(securityAuditEvents?.items || []).map((event) => ({ ...event, id: `security:${event.id}`, user_id: event.actor_id || undefined })),
  ].sort((left, right) => Date.parse(right.created_at) - Date.parse(left.created_at));
  const filteredAuditLogs = combinedAuditLogs.filter((log) => auditFilter === 'all'
    || BACKGROUND_ACTIONS.includes(log.action) === (auditFilter === 'background'));

  return (
    <div style={{ display: 'flex', flexDirection: 'column', gap: 8 }}>
      <div style={{ display: 'flex', flexWrap: 'wrap', gap: 8, padding: 12 }}>
        {([
          ['all', t('enterprise.audit.filterAll', 'All')],
          ['background', t('enterprise.audit.filterBackground', 'Background')],
          ['actions', t('enterprise.audit.filterActions', 'Actions')],
        ] as const).map(([key, label]) => <button key={key} className={`btn ${auditFilter === key ? 'btn-primary' : 'btn-secondary'}`}
          onClick={() => setAuditFilter(key)}>{label}</button>)}
        <span style={{ marginLeft: 'auto', alignSelf: 'center' }}>{t('enterprise.audit.records', { count: filteredAuditLogs.length })}</span>
      </div>
      {filteredAuditLogs.map((log) => {
        const details = log.details || {};
        const management = log.action === 'user:offboarded' || log.action === 'user:role_changed';
        const failed = details.success === false || ['failed', 'error', 'denied'].includes(details.status) || log.action.endsWith('_error');
        const completed = management || details.success === true || ['completed', 'succeeded', 'success'].includes(details.status);
        return <article key={log.id} style={{ padding: 12, borderBottom: '1px solid var(--border-subtle)' }}>
          <div style={{ display: 'flex', flexWrap: 'wrap', gap: 12, alignItems: 'center' }}>
            <time dateTime={log.created_at} style={{ color: 'var(--text-tertiary)', fontSize: 12 }}>{new Date(log.created_at).toLocaleString()}</time>
            <strong>{actionLabels[log.action] || t('enterprise.audit.activity', 'Activity recorded')}</strong>
            <span>{failed ? t('enterprise.audit.failed', 'Failed') : completed ? t('enterprise.audit.completed', 'Completed') : t('enterprise.audit.recorded', 'Recorded')}</span>
          </div>
          <p>{t('enterprise.audit.actor', 'By {{name}}', { name: userName(log.user_id) })}</p>
          {log.action === 'user:offboarded' && <p>{t('enterprise.audit.offboardingResult', '{{member}} left; company work handed to {{successor}}.',
            { member: userName(details.target_user_id), successor: userName(details.successor_user_id) })}</p>}
          {log.action === 'user:role_changed' && <p>{userName(details.target_user_id)} · {roleName(details.from_role)} → {roleName(details.to_role)}</p>}
          {!management && <p>{[details.provider, details.model].filter((value) => typeof value === 'string').join(' · ')}</p>}
        </article>;
      })}
      {filteredAuditLogs.length === 0 && <p style={{ textAlign: 'center', padding: 40 }}>{t('common.noData', 'No data')}</p>}
    </div>
  );
}
