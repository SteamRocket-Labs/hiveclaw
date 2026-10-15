import { useEffect, useRef, useState } from 'react';

import { useQuery, useQueryClient } from '@tanstack/react-query';
import { useTranslation } from 'react-i18next';

import { ApiError } from '../../api/core';
import {
  enterpriseApi,
  type IdentityDryRunReport,
  type IdentityLinkMember,
  type IdentityLinkRecord,
} from '../../api/domains/enterprise';
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

function kindLabel(kind: string, t: (key: string, fallback: string) => string): string {
  if (kind === 'provider_open_id') return t('enterprise.org.identityKindProviderOpenId', 'Provider open id');
  if (kind === 'provider_union_id') return t('enterprise.org.identityKindProviderUnionId', 'Provider union id');
  if (kind === 'provider_user_id') return t('enterprise.org.identityKindProviderUserId', 'Provider user id');
  return kind;
}

function statusLabel(status: string, t: (key: string, fallback: string) => string): string {
  if (status === 'active') return t('enterprise.org.identityStatusActive', 'Active');
  if (status === 'conflict') return t('enterprise.org.identityStatusConflict', 'Conflict');
  if (status === 'inactive_user') return t('enterprise.org.identityStatusInactiveUser', 'Inactive user');
  if (status === 'revoked') return t('enterprise.org.identityStatusRevoked', 'Revoked');
  if (status === 'unregistered') return t('enterprise.org.identityStatusUnregistered', 'No stored link');
  return t(`enterprise.org.reason.${status}`, status);
}

function actionableLinks(member: IdentityLinkMember | undefined): IdentityLinkRecord[] {
  if (!member) return [];
  return member.links.filter((link) => link.stored_status !== 'revoked' && link.provider_id && link.subject_kind);
}

function IdentityRepairCard({ tenantId }: { tenantId: string }) {
  const { t } = useTranslation();
  const queryClient = useQueryClient();
  const links = useQuery({
    queryKey: ['org-identity-links', tenantId],
    queryFn: () => enterpriseApi.listIdentityLinks(tenantId || undefined),
  });
  const [personId, setPersonId] = useState('');
  const [accountId, setAccountId] = useState('');
  const [providerId, setProviderId] = useState('');
  const [kind, setKind] = useState('');
  const [selectedLinkId, setSelectedLinkId] = useState('');
  const [busy, setBusy] = useState<'save' | 'revoke' | 'reload' | null>(null);
  const [notice, setNotice] = useState<{ tone: 'ok' | 'error'; text: string; retryable: boolean } | null>(null);
  const [recovery, setRecovery] = useState<'save' | 'revoke' | null>(null);
  const [needsReview, setNeedsReview] = useState(false);
  const [reviewed, setReviewed] = useState(false);

  const members = links.data?.members ?? [];
  const accounts = links.data?.accounts ?? [];
  const providers = links.data?.providers ?? [];
  const person = members.find((member) => member.org_member_id === personId);
  const account = accounts.find((item) => item.user_id === accountId);
  const provider = providers.find((item) => item.id === providerId);
  const choices = actionableLinks(person);
  const stored = choices.length === 1
    ? choices[0]
    : choices.find((link) => link.id === selectedLinkId) ?? null;
  const mustChooseLink = choices.length > 1 && !stored;
  const canChoose = Boolean(person && account && provider && kind && person.subject_kinds.includes(kind));

  const clearTenantResult = () => {
    setNotice(null);
    setRecovery(null);
    setNeedsReview(false);
    setReviewed(false);
    setSelectedLinkId('');
    setPersonId('');
    setAccountId('');
    setProviderId('');
    setKind('');
  };

  useEffect(() => {
    clearTenantResult();
  }, [tenantId]);

  const refresh = async () => {
    await queryClient.invalidateQueries({ queryKey: ['org-identity-links', tenantId] });
    await queryClient.invalidateQueries({ queryKey: ['company-knowledge-grant-identities'] });
  };

  const openRecovery = (action: 'save' | 'revoke', text: string) => {
    setRecovery(action);
    setNeedsReview(true);
    setReviewed(false);
    setNotice({ tone: 'error', text, retryable: true });
  };

  const reloadForReview = async () => {
    setBusy('reload');
    try {
      await refresh();
      setReviewed(true);
      setNeedsReview(false);
    } catch (caught: unknown) {
      const message = caught instanceof Error ? caught.message : '';
      setNotice({
        tone: 'error',
        text: message || t('enterprise.org.identityRepairUnavailable', 'Stored identity links are unavailable.'),
        retryable: true,
      });
    }
    setBusy(null);
  };

  const save = async () => {
    if (needsReview || !person || !account || !provider || !kind || mustChooseLink) return;
    const createBody = {
      org_member_id: person.org_member_id,
      user_id: account.user_id,
      provider_id: provider.id,
      subject_kind: kind,
    };
    setBusy('save');
    setNotice(null);
    try {
      const result = stored?.provider_id && stored.subject_kind
        ? await enterpriseApi.replaceIdentityLink(stored.id, {
          user_id: account.user_id,
          provider_id: provider.id,
          subject_kind: kind,
          expected_version: stored.version,
          expected_user_id: stored.user_id,
          expected_provider_id: stored.provider_id,
          expected_subject_kind: stored.subject_kind,
          expected_status: stored.stored_status,
        }, tenantId || undefined)
        : await enterpriseApi.createIdentityLink(createBody, tenantId || undefined);
      if (result.retryable) {
        openRecovery('save', result.message);
      } else {
        setRecovery(null);
        setNeedsReview(false);
        setReviewed(false);
        setNotice({
          tone: result.applied ? 'ok' : 'error',
          text: result.applied
            ? `${t('enterprise.org.identityRepairSaved', 'The link was saved.')} ${result.link?.id ?? ''}`.trim()
            : result.message,
          retryable: false,
        });
      }
      await refresh();
    } catch (caught: unknown) {
      const message = caught instanceof Error ? caught.message : '';
      const retryable = caught instanceof ApiError && Boolean(
        caught.data && typeof caught.data === 'object' && (caught.data as { retryable?: boolean }).retryable,
      );
      if (retryable) {
        openRecovery(
          'save',
          message || t('enterprise.org.identityRepairStale', 'The stored link changed. Reload it and try again.'),
        );
      } else {
        setRecovery(null);
        setNeedsReview(false);
        setReviewed(false);
        setNotice({
          tone: 'error',
          text: message || t('enterprise.org.identityRepairUnavailable', 'Stored identity links are unavailable.'),
          retryable: false,
        });
      }
    }
    setBusy(null);
  };

  const revoke = async () => {
    if (needsReview || !stored?.provider_id || !stored.subject_kind) return;
    setBusy('revoke');
    setNotice(null);
    try {
      const result = await enterpriseApi.revokeIdentityLink(stored.id, {
        expected_version: stored.version,
        expected_user_id: stored.user_id,
        expected_provider_id: stored.provider_id,
        expected_subject_kind: stored.subject_kind,
        expected_status: stored.stored_status,
      }, tenantId || undefined);
      if (result.retryable) {
        openRecovery('revoke', result.message || t('enterprise.org.identityRepairStale', 'The stored link changed. Reload it and try again.'));
      } else {
        setRecovery(null);
        setNeedsReview(false);
        setReviewed(false);
        setNotice({
          tone: result.applied || result.code === 'revoked' ? 'ok' : 'error',
          text: result.message || t('enterprise.org.identityRepairRevoked', 'The link was revoked.'),
          retryable: false,
        });
      }
      await refresh();
    } catch (caught: unknown) {
      const message = caught instanceof Error ? caught.message : '';
      const retryable = caught instanceof ApiError && Boolean(
        caught.data && typeof caught.data === 'object' && (caught.data as { retryable?: boolean }).retryable,
      );
      if (retryable) {
        openRecovery(
          'revoke',
          message || t('enterprise.org.identityRepairStale', 'The stored link changed. Reload it and try again.'),
        );
      } else {
        setRecovery(null);
        setNeedsReview(false);
        setReviewed(false);
        setNotice({
          tone: 'error',
          text: message || t('enterprise.org.identityRepairUnavailable', 'Stored identity links are unavailable.'),
          retryable: false,
        });
      }
    }
    setBusy(null);
  };

  const forbidden = links.error instanceof ApiError && links.error.status === 403;
  const saveLabel = busy === 'save'
    ? t('enterprise.org.identityRepairSaving', 'Saving this link…')
    : t('enterprise.org.identityRepairConfirm', 'Save this link');

  return (
    <div className="card ws-org-card ws-org-identity-repair">
      <h4 className="ws-org-card-title">{t('enterprise.org.identityRepair', 'Stored identity links')}</h4>
      <p className="ws-org-card-desc">
        {t(
          'enterprise.org.identityRepairDesc',
          'Link a stored directory person to an account in this company. The subject is copied from the directory record. This does not change roles, passwords, or grants.',
        )}
      </p>
      {links.isLoading ? (
        <div className="ws-org-empty">{t('enterprise.org.identityRepairLoading', 'Loading stored identity links…')}</div>
      ) : null}
      {forbidden ? (
        <div className="ws-org-sync-result is-error" role="alert">
          {t('enterprise.org.identityRepairForbidden', 'You do not have permission to repair identity links for this company.')}
        </div>
      ) : null}
      {links.isError && !forbidden ? (
        <div className="ws-org-actions">
          <div className="ws-org-sync-result is-error" role="alert">
            {links.error instanceof Error
              ? links.error.message
              : t('enterprise.org.identityRepairUnavailable', 'Stored identity links are unavailable.')}
          </div>
          <button className="btn btn-secondary" type="button" onClick={() => links.refetch()}>
            {t('enterprise.org.identityRepairRetryLoad', 'Retry loading identity links')}
          </button>
        </div>
      ) : null}
      {links.isSuccess && members.length === 0 ? (
        <div className="ws-org-empty">{t('enterprise.org.identityRepairEmpty', 'No stored directory people to link.')}</div>
      ) : null}
      {links.isSuccess && members.length > 0 ? (
        <>
          <div className="ws-org-form-row">
            <div className="ws-org-field">
              <label className="ws-org-label" htmlFor="identity-repair-person">
                {t('enterprise.org.identityRepairPerson', 'Directory person')}
              </label>
              <select
                id="identity-repair-person"
                className="input"
                value={personId}
                onChange={(event) => {
                  const next = members.find((member) => member.org_member_id === event.target.value);
                  setPersonId(event.target.value);
                  setKind(next?.subject_kinds[0] ?? '');
                  setSelectedLinkId('');
                  setNotice(null);
                  setRecovery(null);
                  setNeedsReview(false);
                  setReviewed(false);
                }}
              >
                <option value="">{t('enterprise.org.identityRepairPerson', 'Directory person')}</option>
                {members.map((member) => (
                  <option key={member.org_member_id} value={member.org_member_id}>
                    {member.label} · {statusLabel(member.reason, t)}
                  </option>
                ))}
              </select>
            </div>
            <div className="ws-org-field">
              <label className="ws-org-label" htmlFor="identity-repair-account">
                {t('enterprise.org.identityRepairAccount', 'Account')}
              </label>
              <select
                id="identity-repair-account"
                className="input"
                value={accountId}
                onChange={(event) => {
                  setAccountId(event.target.value);
                  setNotice(null);
                }}
              >
                <option value="">{t('enterprise.org.identityRepairAccount', 'Account')}</option>
                {accounts.map((item) => (
                  <option key={item.user_id} value={item.user_id}>
                    {item.label}
                    {item.is_active ? '' : ` · ${t('enterprise.org.identityRepairInactiveAccount', 'Inactive')}`}
                  </option>
                ))}
              </select>
            </div>
          </div>
          <div className="ws-org-form-row">
            <div className="ws-org-field">
              <label className="ws-org-label" htmlFor="identity-repair-provider">
                {t('enterprise.org.identityRepairProvider', 'Provider')}
              </label>
              <select
                id="identity-repair-provider"
                className="input"
                value={providerId}
                onChange={(event) => {
                  setProviderId(event.target.value);
                  setNotice(null);
                }}
              >
                <option value="">{t('enterprise.org.identityRepairProvider', 'Provider')}</option>
                {providers.map((item) => (
                  <option key={item.id} value={item.id}>
                    {item.name} · {item.provider_type}
                    {item.is_active ? '' : ` · ${t('enterprise.org.identityRepairInactiveAccount', 'Inactive')}`}
                  </option>
                ))}
              </select>
            </div>
            <div className="ws-org-field">
              <label className="ws-org-label" htmlFor="identity-repair-kind">
                {t('enterprise.org.identityRepairKind', 'Subject kind')}
              </label>
              <select
                id="identity-repair-kind"
                className="input"
                value={kind}
                onChange={(event) => {
                  setKind(event.target.value);
                  setNotice(null);
                }}
              >
                <option value="">{t('enterprise.org.identityRepairKind', 'Subject kind')}</option>
                {(person?.subject_kinds ?? []).map((item) => (
                  <option key={item} value={item}>{kindLabel(item, t)}</option>
                ))}
              </select>
            </div>
          </div>
          {choices.length > 1 ? (
            <div className="ws-org-form-row">
              <div className="ws-org-field">
                <label className="ws-org-label" htmlFor="identity-repair-current-link">
                  {t('enterprise.org.identityRepairCurrentLink', 'Current link')}
                </label>
                <select
                  id="identity-repair-current-link"
                  className="input"
                  value={selectedLinkId}
                  onChange={(event) => {
                    setSelectedLinkId(event.target.value);
                    setNotice(null);
                    setRecovery(null);
                    setNeedsReview(false);
                    setReviewed(false);
                  }}
                >
                  <option value="">{t('enterprise.org.identityRepairChooseLink', 'Choose the current link')}</option>
                  {choices.map((link) => (
                    <option key={link.id} value={link.id}>
                      {accounts.find((item) => item.user_id === link.user_id)?.label || link.user_id}
                      {' · '}
                      {kindLabel(link.subject_kind || '', t)}
                      {' · '}
                      {statusLabel(link.stored_status, t)}
                      {' · '}
                      {link.id}
                    </option>
                  ))}
                </select>
              </div>
            </div>
          ) : null}
          {providers.length === 0 ? (
            <p className="ws-org-card-desc">{t('enterprise.org.identityRepairNoProviders', 'This company has no identity provider to link.')}</p>
          ) : null}
          {person && person.subject_kinds.length === 0 ? (
            <p className="ws-org-card-desc">{t('enterprise.org.identityRepairNoSubject', 'This directory person has no stored subject to link.')}</p>
          ) : null}
          {canChoose && person && account && provider ? (
            <p className="ws-org-card-desc" role="status">
              {t('enterprise.org.identityRepairPreview', {
                person: person.label,
                account: account.label,
                provider: provider.name,
                kind: kindLabel(kind, t),
                defaultValue: 'This will link {{person}} to {{account}} through {{provider}} ({{kind}}).',
              })}
            </p>
          ) : null}
          {mustChooseLink ? (
            <p className="ws-org-card-desc">{t('enterprise.org.identityRepairChooseLink', 'Choose the current link')}</p>
          ) : null}
          {reviewed ? (
            <p className="ws-org-card-desc" role="status">
              {stored
                ? t('enterprise.org.identityRepairCurrentMapping', {
                  account: accounts.find((item) => item.user_id === stored.user_id)?.label || stored.user_id,
                  kind: kindLabel(stored.subject_kind || '', t),
                  status: statusLabel(stored.stored_status, t),
                  version: stored.version,
                  defaultValue: 'Current mapping: {{account}} · {{kind}} · {{status}} · {{version}}',
                })
                : t('enterprise.org.identityRepairChooseLink', 'Choose the current link')}
            </p>
          ) : null}
          {stored && person ? (
            <p className="ws-org-card-desc">
              {t('enterprise.org.identityRepairRevokePreview', {
                person: person.label,
                account: accounts.find((item) => item.user_id === stored.user_id)?.label || stored.user_id,
                kind: kindLabel(stored.subject_kind || '', t),
                defaultValue: 'This will revoke the stored link for {{person}} to {{account}} ({{kind}}).',
              })}
              {' · '}
              {statusLabel(stored.stored_status, t)}
            </p>
          ) : null}
          <div className="ws-org-actions">
            <button className="btn btn-primary" type="button" onClick={save} disabled={!canChoose || busy !== null || mustChooseLink || needsReview || providers.length === 0}>
              {saveLabel}
            </button>
            {stored ? (
              <button className="btn btn-secondary" type="button" onClick={revoke} disabled={busy !== null || needsReview || !stored.provider_id}>
                {busy === 'revoke'
                  ? t('enterprise.org.identityRepairRevoking', 'Revoking this link…')
                  : t('enterprise.org.identityRepairRevoke', 'Revoke this link')}
              </button>
            ) : null}
            {notice?.retryable && needsReview ? (
              <button className="btn btn-secondary" type="button" onClick={reloadForReview} disabled={busy !== null}>
                {t('enterprise.org.identityRepairReload', 'Reload the current link')}
              </button>
            ) : null}
            {notice?.retryable && reviewed && recovery === 'save' ? (
              <button className="btn btn-secondary" type="button" onClick={save} disabled={busy !== null || mustChooseLink}>
                {t('enterprise.org.identityRepairRetry', 'Retry saving this link')}
              </button>
            ) : null}
            {notice?.retryable && reviewed && recovery === 'revoke' ? (
              <button className="btn btn-secondary" type="button" onClick={revoke} disabled={busy !== null || !stored}>
                {t('enterprise.org.identityRepairRetryRevoke', 'Retry revoking this link')}
              </button>
            ) : null}
          </div>
          {notice ? (
            <div className={`ws-org-sync-result ${notice.tone === 'ok' ? 'is-ok' : 'is-error'}`} role={notice.tone === 'error' ? 'alert' : 'status'}>
              {notice.text}
            </div>
          ) : null}
        </>
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
      <IdentityRepairCard key={`repair-${selectedTenantId}`} tenantId={selectedTenantId} />

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
