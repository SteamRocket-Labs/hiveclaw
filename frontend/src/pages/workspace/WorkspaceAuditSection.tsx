import { useState } from 'react';

import { useQuery } from '@tanstack/react-query';
import { useTranslation } from 'react-i18next';

import { enterpriseApi } from '../../api/domains/enterprise';

interface WorkspaceAuditSectionProps {
  selectedTenantId: string;
}

type AuditFilter = 'all' | 'background' | 'actions';

interface WorkspaceAuditLog {
  id: string;
  action: string;
  created_at: string;
  agent_id?: string | null;
  details?: Record<string, unknown> | null;
}

const BACKGROUND_ACTIONS = [
  'supervision_tick',
  'supervision_fire',
  'supervision_error',
  'schedule_tick',
  'schedule_fire',
  'schedule_error',
  'heartbeat_tick',
  'heartbeat_fire',
  'heartbeat_error',
  'server_startup',
];

export default function WorkspaceAuditSection({
  selectedTenantId,
}: WorkspaceAuditSectionProps) {
  const { t } = useTranslation();
  const [auditFilter, setAuditFilter] = useState<AuditFilter>('all');
  const { data: auditLogs = [] } = useQuery({
    queryKey: ['audit-logs', selectedTenantId],
    queryFn: () => enterpriseApi.getAuditLogs(`limit=200${selectedTenantId ? `&tenant_id=${selectedTenantId}` : ''}`),
  });

  const filteredAuditLogs = (auditLogs as WorkspaceAuditLog[]).filter((log) => {
    if (auditFilter === 'background') return BACKGROUND_ACTIONS.includes(log.action);
    if (auditFilter === 'actions') return !BACKGROUND_ACTIONS.includes(log.action);
    return true;
  });

  return (
    <div style={{ display: 'flex', flexDirection: 'column', gap: '4px' }}>
      <div style={{ display: 'flex', gap: '8px', padding: '8px 12px', borderBottom: '1px solid var(--border-color)' }}>
        {([
          ['all', t('enterprise.audit.filterAll', 'All')],
          ['background', t('enterprise.audit.filterBackground', 'Background')],
          ['actions', t('enterprise.audit.filterActions', 'Actions')],
        ] as const).map(([key, label]) => (
          <button
            key={key}
            onClick={() => setAuditFilter(key)}
            style={{
              padding: '4px 14px',
              borderRadius: '12px',
              fontSize: '12px',
              fontWeight: 500,
              border: auditFilter === key ? '1px solid var(--accent-primary)' : '1px solid var(--border-subtle)',
              background: auditFilter === key ? 'var(--accent-primary)' : 'transparent',
              color: auditFilter === key ? '#fff' : 'var(--text-secondary)',
              cursor: 'pointer',
              transition: 'all 0.15s',
            }}
          >
            {label}
          </button>
        ))}
        <span style={{ marginLeft: 'auto', fontSize: '11px', color: 'var(--text-tertiary)', alignSelf: 'center' }}>
          {t('enterprise.audit.records', { count: filteredAuditLogs.length })}
        </span>
      </div>
      {filteredAuditLogs.map((log) => {
        const isBackgroundAction = BACKGROUND_ACTIONS.includes(log.action);
        const details =
          log.details && typeof log.details === 'object' && Object.keys(log.details).length > 0
            ? log.details
            : null;

        return (
          <div key={log.id} style={{ borderBottom: '1px solid var(--border-subtle)', padding: '6px 12px' }}>
            <div style={{ display: 'flex', gap: '12px', fontSize: '13px', alignItems: 'center' }}>
              <span style={{ color: 'var(--text-tertiary)', whiteSpace: 'nowrap', fontFamily: 'var(--font-mono)', fontSize: '11px' }}>
                {new Date(log.created_at).toLocaleString()}
              </span>
              <span
                style={{
                  padding: '1px 8px',
                  borderRadius: '4px',
                  fontSize: '11px',
                  fontWeight: 500,
                  background: isBackgroundAction ? 'rgba(99,102,241,0.12)' : 'rgba(34,197,94,0.12)',
                  color: isBackgroundAction ? 'var(--accent-color)' : 'rgb(34,197,94)',
                }}
              >
                {isBackgroundAction ? '⚙️' : '👤'}
              </span>
              <span style={{ flex: 1, fontWeight: 500 }}>{log.action}</span>
              <span style={{ color: 'var(--text-tertiary)', fontSize: '11px' }}>{log.agent_id?.slice(0, 8) || '-'}</span>
            </div>
            {details ? (
              <div style={{ marginLeft: '100px', marginTop: '2px', fontSize: '11px', color: 'var(--text-tertiary)', fontFamily: 'var(--font-mono)' }}>
                {Object.entries(details).map(([key, value]) => (
                  <span key={key} style={{ marginRight: '12px' }}>
                    {key}={typeof value === 'string' ? value : JSON.stringify(value)}
                  </span>
                ))}
              </div>
            ) : null}
          </div>
        );
      })}
      {filteredAuditLogs.length === 0 ? (
        <div style={{ textAlign: 'center', padding: '40px', color: 'var(--text-tertiary)' }}>
          {t('common.noData', 'No data')}
        </div>
      ) : null}
    </div>
  );
}
