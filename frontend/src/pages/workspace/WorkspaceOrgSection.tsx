import { useEffect, useState } from 'react';

import { useQuery, useQueryClient } from '@tanstack/react-query';
import { useTranslation } from 'react-i18next';

import { enterpriseApi } from '../../api/domains/enterprise';
import { toolsApi, type FeishuRuntimeStatus } from '../../api/domains/tools';
import FeishuRuntimeStatusCard from '../../components/FeishuRuntimeStatusCard';

interface WorkspaceOrgSectionProps {
  selectedTenantId: string;
}

interface WorkspaceDepartment {
  id: string;
  name: string;
  parent_id: string | null;
  member_count: number;
}

interface WorkspaceMember {
  id: string;
  name: string;
  title?: string | null;
  department_path?: string | null;
  email?: string | null;
}

interface DeptTreeProps {
  departments: WorkspaceDepartment[];
  parentId: string | null;
  selectedDept: string | null;
  onSelect: (id: string | null) => void;
  level: number;
}

function DeptTree({
  departments,
  parentId,
  selectedDept,
  onSelect,
  level,
}: DeptTreeProps) {
  const children = departments.filter((department) =>
    parentId === null ? !department.parent_id : department.parent_id === parentId,
  );

  if (children.length === 0) {
    return null;
  }

  return (
    <>
      {children.map((department) => (
        <div key={department.id}>
          <div
            style={{
              padding: '5px 8px',
              paddingLeft: `${8 + level * 16}px`,
              borderRadius: '4px',
              cursor: 'pointer',
              fontSize: '13px',
              marginBottom: '1px',
              background: selectedDept === department.id ? 'rgba(224,238,238,0.12)' : 'transparent',
            }}
            onClick={() => onSelect(department.id)}
          >
            <span style={{ color: 'var(--text-tertiary)', marginRight: '4px', fontSize: '11px' }}>
              {departments.some((child) => child.parent_id === department.id) ? '▸' : '·'}
            </span>
            {department.name}
            {department.member_count > 0 ? (
              <span style={{ fontSize: '10px', color: 'var(--text-tertiary)', marginLeft: '4px' }}>
                ({department.member_count})
              </span>
            ) : null}
          </div>
          <DeptTree
            departments={departments}
            parentId={department.id}
            selectedDept={selectedDept}
            onSelect={onSelect}
            level={level + 1}
          />
        </div>
      ))}
    </>
  );
}

export default function WorkspaceOrgSection({
  selectedTenantId,
}: WorkspaceOrgSectionProps) {
  const { t } = useTranslation();
  const queryClient = useQueryClient();
  const [syncForm, setSyncForm] = useState({ app_id: '', app_secret: '' });
  const [syncing, setSyncing] = useState(false);
  const [syncResult, setSyncResult] = useState<{ departments?: number; members?: number; error?: string } | null>(null);
  const [memberSearch, setMemberSearch] = useState('');
  const [selectedDept, setSelectedDept] = useState<string | null>(null);

  const { data: config } = useQuery({
    queryKey: ['system-settings', 'feishu_org_sync'],
    queryFn: () => enterpriseApi.getSetting('feishu_org_sync'),
  });
  const { data: departments = [] } = useQuery({
    queryKey: ['org-departments', selectedTenantId],
    queryFn: () => enterpriseApi.getDepartments(selectedTenantId || undefined),
  });
  const { data: members = [] } = useQuery({
    queryKey: ['org-members', selectedDept, memberSearch, selectedTenantId],
    queryFn: () => enterpriseApi.getOrgMembers({
      ...(selectedDept ? { departmentId: selectedDept } : {}),
      ...(memberSearch ? { search: memberSearch } : {}),
      ...(selectedTenantId ? { tenantId: selectedTenantId } : {}),
    }),
  });
  const { data: feishuRuntimeStatus } = useQuery<FeishuRuntimeStatus | null>({
    queryKey: ['feishu-runtime-status'],
    queryFn: () => toolsApi.getFeishuRuntimeStatus(),
  });

  useEffect(() => {
    if (config?.value?.app_id) {
      setSyncForm({ app_id: config.value.app_id, app_secret: '' });
    }
  }, [config]);

  const saveConfig = async () => {
    await enterpriseApi.updateSetting('feishu_org_sync', {
      app_id: syncForm.app_id,
      app_secret: syncForm.app_secret,
    });
    queryClient.invalidateQueries({ queryKey: ['system-settings', 'feishu_org_sync'] });
  };

  const triggerSync = async () => {
    setSyncing(true);
    setSyncResult(null);
    try {
      if (syncForm.app_secret) {
        await saveConfig();
      }
      const result = await enterpriseApi.syncOrg(selectedTenantId || undefined);
      setSyncResult(result);
      queryClient.invalidateQueries({ queryKey: ['org-departments'] });
      queryClient.invalidateQueries({ queryKey: ['org-members'] });
    } catch (error: any) {
      setSyncResult({ error: error.message });
    }
    setSyncing(false);
  };

  return (
    <div>
      <div className="card" style={{ marginBottom: '16px' }}>
        <h4 style={{ marginBottom: '12px' }}>{t('enterprise.org.feishuSync', 'Feishu Sync')}</h4>
        <p style={{ fontSize: '12px', color: 'var(--text-tertiary)', marginBottom: '12px' }}>
          {t('enterprise.org.feishuSyncDesc', 'Sync department and member data from Feishu/Lark.')}
        </p>
        <div style={{ display: 'flex', gap: '12px', marginBottom: '12px' }}>
          <div style={{ flex: 1 }}>
            <label style={{ fontSize: '12px', fontWeight: 500, display: 'block', marginBottom: '4px' }}>App ID</label>
            <input
              className="input"
              value={syncForm.app_id}
              onChange={(event) => setSyncForm({ ...syncForm, app_id: event.target.value })}
              placeholder="cli_xxxxxxxx"
            />
          </div>
          <div style={{ flex: 1 }}>
            <label style={{ fontSize: '12px', fontWeight: 500, display: 'block', marginBottom: '4px' }}>App Secret</label>
            <input
              className="input"
              type="password"
              value={syncForm.app_secret}
              onChange={(event) => setSyncForm({ ...syncForm, app_secret: event.target.value })}
              placeholder=""
            />
          </div>
        </div>
        <div style={{ display: 'flex', gap: '8px', alignItems: 'center' }}>
          <button className="btn btn-primary" onClick={triggerSync} disabled={syncing || !syncForm.app_id}>
            {syncing ? t('enterprise.org.syncing', 'Syncing...') : t('enterprise.org.syncNow', 'Sync Now')}
          </button>
          {config?.value?.last_synced_at ? (
            <span style={{ fontSize: '12px', color: 'var(--text-tertiary)' }}>
              Last sync: {new Date(config.value.last_synced_at).toLocaleString()}
            </span>
          ) : null}
        </div>
        {syncResult ? (
          <div
            style={{
              marginTop: '12px',
              padding: '8px 12px',
              borderRadius: '6px',
              fontSize: '12px',
              background: syncResult.error ? 'rgba(255,0,0,0.1)' : 'rgba(0,200,0,0.1)',
            }}
          >
            {syncResult.error
              ? syncResult.error
              : t('enterprise.org.syncComplete', {
                  departments: syncResult.departments,
                  members: syncResult.members,
                })}
          </div>
        ) : null}
      </div>

      {feishuRuntimeStatus ? (
        <FeishuRuntimeStatusCard status={feishuRuntimeStatus} isAdmin />
      ) : null}

      <div className="card">
        <h4 style={{ marginBottom: '12px' }}>{t('enterprise.org.orgBrowser', 'Org Browser')}</h4>
        <div style={{ display: 'flex', gap: '16px' }}>
          <div
            style={{
              width: '260px',
              borderRight: '1px solid var(--border-subtle)',
              paddingRight: '16px',
              maxHeight: '500px',
              overflowY: 'auto',
            }}
          >
            <div style={{ fontSize: '12px', fontWeight: 600, marginBottom: '8px', color: 'var(--text-secondary)' }}>
              {t('enterprise.org.allDepartments', 'All Departments')}
            </div>
            <div
              style={{
                padding: '6px 8px',
                borderRadius: '4px',
                cursor: 'pointer',
                fontSize: '13px',
                marginBottom: '2px',
                background: !selectedDept ? 'rgba(224,238,238,0.1)' : 'transparent',
              }}
              onClick={() => setSelectedDept(null)}
            >
              {t('common.all', 'All')}
            </div>
            <DeptTree
              departments={departments as WorkspaceDepartment[]}
              parentId={null}
              selectedDept={selectedDept}
              onSelect={setSelectedDept}
              level={0}
            />
            {departments.length === 0 ? (
              <div style={{ fontSize: '12px', color: 'var(--text-tertiary)', padding: '8px' }}>
                {t('common.noData', 'No data')}
              </div>
            ) : null}
          </div>

          <div style={{ flex: 1 }}>
            <input
              className="input"
              placeholder={t('enterprise.org.searchMembers', 'Search members')}
              value={memberSearch}
              onChange={(event) => setMemberSearch(event.target.value)}
              style={{ marginBottom: '12px', fontSize: '13px' }}
            />
            <div style={{ display: 'flex', flexDirection: 'column', gap: '4px', maxHeight: '400px', overflowY: 'auto' }}>
              {(members as WorkspaceMember[]).map((member) => (
                <div
                  key={member.id}
                  style={{
                    display: 'flex',
                    alignItems: 'center',
                    gap: '10px',
                    padding: '8px',
                    borderRadius: '6px',
                    border: '1px solid var(--border-subtle)',
                  }}
                >
                  <div
                    style={{
                      width: '32px',
                      height: '32px',
                      borderRadius: '50%',
                      background: 'rgba(224,238,238,0.15)',
                      display: 'flex',
                      alignItems: 'center',
                      justifyContent: 'center',
                      fontSize: '14px',
                      fontWeight: 600,
                    }}
                  >
                    {member.name?.[0] || '?'}
                  </div>
                  <div>
                    <div style={{ fontWeight: 500, fontSize: '13px' }}>{member.name}</div>
                    <div style={{ fontSize: '11px', color: 'var(--text-tertiary)' }}>
                      {member.title || '-'} · {member.department_path || '-'}
                      {member.email ? ` · ${member.email}` : ''}
                    </div>
                  </div>
                </div>
              ))}
              {members.length === 0 ? (
                <div style={{ textAlign: 'center', padding: '24px', color: 'var(--text-tertiary)', fontSize: '13px' }}>
                  {t('enterprise.org.noMembers', 'No members')}
                </div>
              ) : null}
            </div>
          </div>
        </div>
      </div>
    </div>
  );
}
