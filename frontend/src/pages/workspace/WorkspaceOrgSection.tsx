import { useEffect, useRef, useState } from 'react';

import { useQuery, useQueryClient } from '@tanstack/react-query';
import { useTranslation } from 'react-i18next';

import { enterpriseApi, type IdentityDryRunReport } from '../../api/domains/enterprise';
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

function identityKindLabel(kind: string, t: (key: string, fallback: string) => string): string {
  if (kind === 'org_member') return t('enterprise.org.identityKindMember', 'Directory person');
  if (kind === 'org_department') return t('enterprise.org.identityKindDepartment', 'Directory department');
  if (kind === 'runtime_task') return t('enterprise.org.identityKindTask', 'Task requester');
  return kind;
}

function IdentityCheckCard({ tenantId }: { tenantId: string }) {
  const { t } = useTranslation();
  const mounted = useRef(true);
  const [state, setState] = useState<'idle' | 'loading' | 'error' | 'ready'>('idle');
  const [report, setReport] = useState<IdentityDryRunReport | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    mounted.current = true;
    return () => {
      mounted.current = false;
    };
  }, []);

  const checkIdentity = async () => {
    setState('loading');
    setError(null);
    try {
      const next = await enterpriseApi.dryRunIdentityLinks(tenantId || undefined);
      if (!mounted.current) return;
      setReport(next);
      setState('ready');
    } catch (caught: unknown) {
      if (!mounted.current) return;
      setReport(null);
      const message = caught instanceof Error ? caught.message : '';
      setError(message || t('enterprise.org.identityUnavailable', 'The identity check is unavailable.'));
      setState('error');
    }
  };

  const complete = Boolean(report?.inputs?.stored_rows && report?.inputs?.incoming_provider_subjects);
  const buttonLabel = state === 'loading'
    ? t('enterprise.org.identityChecking', 'Checking identity links…')
    : state === 'error'
      ? t('enterprise.org.identityRetry', 'Retry identity check')
      : t('enterprise.org.identityCheck', 'Check identity links');

  return (
    <div className="card ws-org-card">
      <h4 className="ws-org-card-title">{t('enterprise.org.identityCheck', 'Check identity links')}</h4>
      <p className="ws-org-card-desc">
        {t(
          'enterprise.org.identityCheckDesc',
          'Compare stored people with the configured directory. This does not sync, save the secret, or apply links.',
        )}
      </p>
      <div className="ws-org-actions">
        <button className="btn btn-secondary" type="button" onClick={checkIdentity} disabled={state === 'loading'}>
          {buttonLabel}
        </button>
      </div>
      {state === 'error' ? (
        <div className="ws-org-sync-result is-error" role="alert">
          {error}
        </div>
      ) : null}
      {state === 'ready' && report ? (
        <div className="ws-org-identity" role="status">
          <div className={report.applied === false ? 'ws-org-sync-result is-ok' : 'ws-org-sync-result is-error'}>
            {report.applied === false
              ? t('enterprise.org.identityAppliedNone', 'Nothing was applied. No users, departments, or links were changed.')
              : t('enterprise.org.identityAppliedUnexpected', 'This response did not confirm that nothing was applied.')}
          </div>
          <p className="ws-org-card-desc">
            {complete
              ? t('enterprise.org.identityCoverage', 'Stored rows and the incoming provider snapshot were both included.')
              : t(
                  'enterprise.org.identityIncompleteInputs',
                  'This check did not include both stored rows and the provider snapshot, so it is not a complete result.',
                )}
          </p>
          <div className="ws-org-meta">Revision {report.source_revision}</div>
          {complete ? (
            <div className="ws-org-identity-counts">
              {Object.entries(report.counts).map(([key, count]) => (
                <span key={key}>
                  {t(`enterprise.org.disposition.${key}`, key)} {count}
                </span>
              ))}
            </div>
          ) : null}
          {complete && report.rows.length === 0 ? (
            <div className="ws-org-empty">{t('enterprise.org.identityEmpty', 'No identity rows to review.')}</div>
          ) : null}
          {complete && (report.department_proposals?.length || 0) > 0 ? (
            <p className="ws-org-card-desc">
              {t('enterprise.org.identityProposals', {
                count: report.department_proposals?.length || 0,
                defaultValue: 'Departments that could be created later: {{count}}. They were not created.',
              })}
            </p>
          ) : null}
          {complete ? (
            <div className="ws-org-identity-list">
              {report.rows.map((row) => (
                <div
                  key={`${row.object_kind}:${row.object_id}:${row.disposition}:${row.reason}`}
                  className="ws-org-identity-row"
                  data-disposition={row.disposition}
                >
                  <div className="ws-org-member-name">
                    {identityKindLabel(row.object_kind, t)}
                    {' · '}
                    {t(`enterprise.org.disposition.${row.disposition}`, row.disposition)}
                  </div>
                  <div className="ws-org-member-meta">{row.object_id}</div>
                  <div>{t(`enterprise.org.reason.${row.reason}`, row.reason)}</div>
                  <div>{t(`enterprise.org.remedy.${row.disposition}`, row.disposition)}</div>
                  {row.user_id ? (
                    <div className="ws-org-member-meta">
                      {t('enterprise.org.identityMatchedUser', 'Matched user')} {row.user_id}
                    </div>
                  ) : null}
                  {row.global_unique_blocked ? (
                    <div>
                      {row.reason === 'local_subject_collision'
                        ? t(
                            'enterprise.org.identityBlockedLocal',
                            'This subject is already used by another record in this company. Nothing was changed.',
                          )
                        : t(
                            'enterprise.org.identityBlocked',
                            'This subject is already used outside this company. The other company is not shown.',
                          )}
                    </div>
                  ) : null}
                </div>
              ))}
            </div>
          ) : null}
        </div>
      ) : null}
    </div>
  );
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

      <IdentityCheckCard key={selectedTenantId} tenantId={selectedTenantId} />

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
