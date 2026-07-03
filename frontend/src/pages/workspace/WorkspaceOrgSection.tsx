import { useEffect, useState } from 'react';

import { useQuery, useQueryClient } from '@tanstack/react-query';
import { useTranslation } from 'react-i18next';

import { enterpriseApi } from '../../api/domains/enterprise';
import { toolsApi, type FeishuRuntimeStatus } from '../../api/domains/tools';
import FeishuRuntimeStatusCard from '../../components/FeishuRuntimeStatusCard';

import './WorkspaceOrgSection.css';

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
            className={`ws-org-dept-item ${selectedDept === department.id ? 'is-selected' : ''}`}
            style={{ paddingLeft: `${8 + level * 16}px` }}
            onClick={() => onSelect(department.id)}
          >
            <span className="ws-org-dept-caret">
              {departments.some((child) => child.parent_id === department.id) ? '▸' : '·'}
            </span>
            {department.name}
            {department.member_count > 0 ? (
              <span className="ws-org-dept-count">
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
    queryClient.invalidateQueries({ queryKey: ['feishu-runtime-status'] });
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
      queryClient.invalidateQueries({ queryKey: ['feishu-runtime-status'] });
    } catch (error: any) {
      setSyncResult({ error: error.message });
    }
    setSyncing(false);
  };

  return (
    <div>
      <div className="card ws-org-card">
        <h4 className="ws-org-card-title">{t('enterprise.org.feishuSync', 'Feishu Sync')}</h4>
        <p className="ws-org-card-desc">
          {t('enterprise.org.feishuSyncDesc', 'Sync department and member data from Feishu/Lark.')}
        </p>
        <div className="ws-org-form-row">
          <div className="ws-org-field">
            <label className="ws-org-label">App ID</label>
            <input
              className="input"
              value={syncForm.app_id}
              onChange={(event) => setSyncForm({ ...syncForm, app_id: event.target.value })}
              placeholder="cli_xxxxxxxx"
            />
          </div>
          <div className="ws-org-field">
            <label className="ws-org-label">App Secret</label>
            <input
              className="input"
              type="password"
              value={syncForm.app_secret}
              onChange={(event) => setSyncForm({ ...syncForm, app_secret: event.target.value })}
              placeholder=""
            />
          </div>
        </div>
        <div className="ws-org-actions">
          <button className="btn btn-primary" onClick={triggerSync} disabled={syncing || !syncForm.app_id}>
            {syncing ? t('enterprise.org.syncing', 'Syncing...') : t('enterprise.org.syncNow', 'Sync Now')}
          </button>
          {config?.value?.last_synced_at ? (
            <span className="ws-org-meta">
              Last sync: {new Date(config.value.last_synced_at).toLocaleString()}
            </span>
          ) : null}
        </div>
        {syncResult ? (
          <div className={`ws-org-sync-result ${syncResult.error ? 'is-error' : 'is-ok'}`}>
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
        <h4 className="ws-org-card-title">{t('enterprise.org.orgBrowser', 'Org Browser')}</h4>
        <div className="ws-org-browser">
          <div className="ws-org-dept-panel">
            <div className="ws-org-panel-title">
              {t('enterprise.org.allDepartments', 'All Departments')}
            </div>
            <div
              className={`ws-org-dept-item ${!selectedDept ? 'is-selected' : ''}`}
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
              <div className="ws-org-empty-sm">
                {t('common.noData', 'No data')}
              </div>
            ) : null}
          </div>

          <div className="ws-org-field">
            <input
              className="input ws-org-search"
              placeholder={t('enterprise.org.searchMembers', 'Search members')}
              value={memberSearch}
              onChange={(event) => setMemberSearch(event.target.value)}
            />
            <div className="ws-org-member-list">
              {(members as WorkspaceMember[]).map((member) => (
                <div key={member.id} className="ws-org-member">
                  <div className="ws-org-avatar">
                    {member.name?.[0] || '?'}
                  </div>
                  <div>
                    <div className="ws-org-member-name">{member.name}</div>
                    <div className="ws-org-member-meta">
                      {member.title || '-'} · {member.department_path || '-'}
                      {member.email ? ` · ${member.email}` : ''}
                    </div>
                  </div>
                </div>
              ))}
              {members.length === 0 ? (
                <div className="ws-org-empty">
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
