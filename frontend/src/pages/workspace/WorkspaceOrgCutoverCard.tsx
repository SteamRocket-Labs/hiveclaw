import { useEffect, useRef, useState } from 'react';

import { useQuery, useQueryClient } from '@tanstack/react-query';
import type { TFunction } from 'i18next';
import { useTranslation } from 'react-i18next';

import {
  enterpriseApi,
  type CutoverJournalReport,
  type DepartmentDirectoryReport,
  type IdentityBackfillPreview,
  type IdentityBackfillRow,
  type IdentityCutoverResult,
  type IdentityLinkList,
} from '../../api/domains/enterprise';

function cutoverOutcome(outcome: string, t: TFunction): string {
  switch (outcome) {
    case 'applied':
      return t('enterprise.org.cutoverOutcomeApplied', 'Applied');
    case 'noop':
      return t('enterprise.org.cutoverOutcomeUnchanged', 'Unchanged');
    case 'skipped':
      return t('enterprise.org.cutoverOutcomeSkipped', 'Skipped');
    case 'conflict':
      return t('enterprise.org.cutoverOutcomeConflict', 'Conflict');
    case 'unavailable':
      return t('enterprise.org.cutoverOutcomeUnavailable', 'Unavailable');
    case 'changed':
      return t('enterprise.org.cutoverOutcomeChanged', 'Changed');
    case 'restored':
      return t('enterprise.org.cutoverOutcomeRestored', 'Restored');
    case 'created':
      return t('enterprise.org.cutoverOutcomeCreated', 'Created');
    case 'mapped':
      return t('enterprise.org.cutoverOutcomeMapped', 'Mapped');
    case 'revoked':
      return t('enterprise.org.cutoverOutcomeRevoked', 'Revoked');
    default:
      return t('enterprise.org.cutoverNeedsReview', 'This row needs review.');
  }
}

function cutoverReason(reason: string, t: TFunction): string {
  switch (reason) {
    case 'department_id_points_elsewhere':
      return t('enterprise.org.reason.department_id_points_elsewhere');
    case 'stale_preview':
      return t('enterprise.org.reason.stale_preview');
    case 'email_only_candidate':
      return t('enterprise.org.reason.email_only_candidate');
    case 'rollback_conflict':
      return t('enterprise.org.cutoverRollbackRefused');
    case 'dependent_link_retained':
      return t('enterprise.org.cutoverDependentKept');
    case 'multiple_directory_departments':
      return t('enterprise.org.reason.multiple_directory_departments');
    case 'name_collision':
      return t('enterprise.org.reason.name_collision');
    case 'missing_parent':
      return t('enterprise.org.reason.missing_parent');
    case 'parent_conflict':
      return t('enterprise.org.reason.parent_conflict');
    case 'already_mapped':
      return t('enterprise.org.reason.already_mapped');
    case 'active_link_conflict':
      return t('enterprise.org.reason.active_link_conflict');
    case 'revoked':
      return t('enterprise.org.reason.revoked');
    case 'member_not_in_tenant':
      return t('enterprise.org.reason.member_not_in_tenant');
    case 'provider_unproven':
      return t('enterprise.org.reason.provider_unproven');
    case 'ambiguous_identity':
      return t('enterprise.org.reason.ambiguous_identity');
    case 'inactive_user':
    case 'authenticated_user_inactive':
      return t('enterprise.org.reason.inactive_user');
    case 'provider_namespace':
      return t('enterprise.org.reason.provider_namespace');
    case 'directory_connection_inactive':
    case 'directory_namespace_changed':
      return t('companyDirectory.mappingUnavailable');
    case 'unregistered':
      return t('enterprise.org.reason.unregistered');
    case 'active':
      return t('enterprise.org.identityStatusActive');
    default:
      return t('enterprise.org.cutoverNeedsReview', 'This row needs review.');
  }
}

function cutoverKind(kind: string, t: TFunction): string {
  switch (kind) {
    case 'user_department':
      return t('enterprise.org.cutoverKindAssignment');
    case 'org_member_user_link':
      return t('enterprise.org.cutoverKindLink');
    case 'department_directory_link':
      return t('enterprise.org.cutoverKindDepartment');
    default:
      return t('enterprise.org.cutoverNeedsReview');
  }
}

function sameTenant(body: { tenant_id?: string } | null, tenantId: string): boolean {
  return !tenantId || body?.tenant_id === tenantId;
}

function shortToken(value: string): string {
  const compact = value.replace(/-/g, '');
  return (compact || value).slice(0, 8);
}

function repeatedNames(names: string[]): Set<string> {
  const counts = new Map<string, number>();
  names.forEach((name) => {
    if (!name) return;
    counts.set(name, (counts.get(name) || 0) + 1);
  });
  return new Set([...counts].filter(([, count]) => count > 1).map(([name]) => name));
}

function subjectKind(kind: string, t: TFunction): string {
  if (kind === 'provider_open_id') return t('enterprise.org.identityKindProviderOpenId');
  if (kind === 'provider_union_id') return t('enterprise.org.identityKindProviderUnionId');
  if (kind === 'provider_user_id') return t('enterprise.org.identityKindProviderUserId');
  return kind;
}

function statusText(status: string, t: TFunction): string {
  if (status === 'active') return t('enterprise.org.identityStatusActive');
  if (status === 'revoked') return t('enterprise.org.identityStatusRevoked');
  if (status === 'conflict') return t('enterprise.org.identityStatusConflict');
  if (status === 'inactive_user') return t('enterprise.org.identityStatusInactiveUser');
  if (status === 'unregistered') return t('enterprise.org.identityStatusUnregistered');
  return t('enterprise.org.cutoverNeedsReview');
}

function distinguishable(t: TFunction, name: string, id: string, repeated: boolean): string {
  if (!name || repeated) {
    return t('enterprise.org.cutoverShortId', {
      name: name || t('enterprise.org.identityKindMember'),
      id: shortToken(id),
    });
  }
  return name;
}

function departmentText(directory: DepartmentDirectoryReport | null, id: string, t: TFunction): string {
  if (!id) return t('enterprise.org.cutoverNoDepartment');
  const found = directory?.departments.find((department) => department.id === id);
  if (found?.name) return found.name;
  return t('enterprise.org.cutoverShortId', {
    name: t('enterprise.org.cutoverKindDepartment'),
    id: shortToken(id),
  });
}

function journalSide(
  value: Record<string, unknown> | null,
  directory: DepartmentDirectoryReport | null,
  t: TFunction,
): string {
  if (!value) return t('enterprise.org.cutoverNoDepartment');
  const parts: string[] = [];
  if ('department_id' in value) {
    parts.push(departmentText(directory, value.department_id ? String(value.department_id) : '', t));
  }
  if (typeof value.status === 'string') parts.push(statusText(value.status, t));
  if (typeof value.subject_kind === 'string') parts.push(subjectKind(value.subject_kind, t));
  if (typeof value.match_subject === 'string' && value.match_subject) parts.push(value.match_subject);
  const orgId = value.org_department_id ? String(value.org_department_id) : '';
  const org = directory?.org_departments.find((row) => row.org_department_id === orgId);
  if (org?.name) parts.push(org.name);
  return parts.filter(Boolean).join(' · ') || t('enterprise.org.cutoverNeedsReview');
}

function appliedWhen(value: string | null): string {
  if (!value) return '';
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return value;
  return `${date.toISOString().replace('T', ' ').slice(0, 19)} UTC`;
}

function nestedIdentity(row: Record<string, unknown>): Record<string, unknown> | null {
  const observed = row.observed;
  if (observed && typeof observed === 'object') return observed as Record<string, unknown>;
  const expected = row.expected_after;
  if (expected && typeof expected === 'object') return expected as Record<string, unknown>;
  return null;
}

function personTitle(
  links: IdentityLinkList | undefined,
  memberId: string,
  userId: string,
  repeated: Set<string>,
  t: TFunction,
): string {
  const member = links?.members.find((item) => item.org_member_id === memberId);
  const account = links?.accounts.find((item) => item.user_id === userId);
  const name = member?.label || account?.label || '';
  return distinguishable(t, name, memberId || userId, repeated.has(name));
}

function previewContext(row: IdentityBackfillRow, links: IdentityLinkList | undefined, t: TFunction): string {
  const member = links?.members.find((item) => item.org_member_id === row.org_member_id)?.label || '';
  const account = links?.accounts.find((item) => item.user_id === row.user_id)?.label || '';
  const kinds = row.subject_kinds.map((kind) => subjectKind(kind, t)).filter(Boolean).join(', ');
  return [account && account !== member ? account : '', kinds].filter(Boolean).join(' · ');
}

export default function WorkspaceOrgCutoverCard({
  tenantId,
  transportNotice,
  revision = 0,
}: {
  tenantId: string;
  transportNotice?: string;
  revision?: number;
}) {
  const { t } = useTranslation();
  const queryClient = useQueryClient();
  const { data: identityLinks } = useQuery({
    queryKey: ['org-identity-links', tenantId],
    queryFn: () => enterpriseApi.listIdentityLinks(tenantId || undefined),
  });
  const mounted = useRef(true);
  const generation = useRef(0);
  const tenantRef = useRef(tenantId);
  tenantRef.current = tenantId;
  const [phase, setPhase] = useState<'idle' | 'loading' | 'ready' | 'error'>('idle');
  const [error, setError] = useState<string | null>(null);
  const [preview, setPreview] = useState<IdentityBackfillPreview | null>(null);
  const [directory, setDirectory] = useState<DepartmentDirectoryReport | null>(null);
  const [journal, setJournal] = useState<CutoverJournalReport | null>(null);
  const [selectedPeople, setSelectedPeople] = useState<string[]>([]);
  const [selectedJournals, setSelectedJournals] = useState<string[]>([]);
  const [choices, setChoices] = useState<Record<string, string>>({});
  const [result, setResult] = useState<IdentityCutoverResult | null>(null);
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    mounted.current = true;
    return () => {
      mounted.current = false;
    };
  }, []);

  const current = () => mounted.current && tenantRef.current === tenantId;

  const invalidate = async () => {
    await queryClient.invalidateQueries({ queryKey: ['org-identity-links', tenantId] });
    await queryClient.invalidateQueries({ queryKey: ['org-departments', tenantId] });
    await queryClient.invalidateQueries({ queryKey: ['org-department-links', tenantId] });
    await queryClient.invalidateQueries({ queryKey: ['org-cutover-journal', tenantId] });
    await queryClient.invalidateQueries({
      predicate: (query) => query.queryKey[0] === 'org-members' && query.queryKey.includes(tenantId),
    });
    await queryClient.invalidateQueries({ queryKey: ['company-knowledge-grant-identities'] });
  };

  const loadRef = useRef<() => Promise<void>>(async () => {});
  const appliedRevision = useRef(revision);

  const load = async () => {
    const ticket = generation.current + 1;
    generation.current = ticket;
    const requested = tenantId;
    setPhase('loading');
    setError(null);
    try {
      const [nextPreview, nextDirectory, nextJournal] = await Promise.all([
        enterpriseApi.previewIdentityBackfill(requested || undefined),
        enterpriseApi.listDepartmentLinks(requested || undefined),
        enterpriseApi.listCutoverJournal(requested || undefined),
      ]);
      if (!mounted.current || ticket !== generation.current || tenantRef.current !== requested) return;
      if (!sameTenant(nextPreview, requested) || !sameTenant(nextDirectory, requested) || !sameTenant(nextJournal, requested)) {
        return;
      }
      setPreview(nextPreview);
      setDirectory(nextDirectory);
      setJournal(nextJournal);
      setSelectedPeople([]);
      setSelectedJournals([]);
      setPhase('ready');
    } catch (caught: unknown) {
      if (!mounted.current || ticket !== generation.current || tenantRef.current !== requested) return;
      setPreview(null);
      setDirectory(null);
      setJournal(null);
      setError(caught instanceof Error ? caught.message : t('enterprise.org.cutoverUnavailable'));
      setPhase('error');
    }
  };
  loadRef.current = load;

  useEffect(() => {
    if (revision === appliedRevision.current || phase !== 'ready') return;
    appliedRevision.current = revision;
    void loadRef.current();
  }, [revision, phase]);

  const run = async (work: () => Promise<IdentityCutoverResult>) => {
    const ticket = generation.current;
    const requested = tenantId;
    setBusy(true);
    setError(null);
    try {
      const next = await work();
      if (!current() || ticket !== generation.current || !sameTenant(next, requested)) return;
      setResult(next);
      await invalidate();
      await load();
    } catch (caught: unknown) {
      if (!current()) return;
      setError(caught instanceof Error ? caught.message : t('enterprise.org.cutoverUnavailable'));
    } finally {
      if (current()) setBusy(false);
    }
  };

  const toggle = (id: string, selected: string[], setSelected: (ids: string[]) => void) => {
    setSelected(selected.includes(id) ? selected.filter((item) => item !== id) : [...selected, id]);
  };

  const counts = result?.counts;
  const linkedPeople = (identityLinks?.members ?? []).filter((member) => member.valid && member.links.some((link) => link.valid));
  const linkedPersonIds = new Set(linkedPeople.map((member) => member.org_member_id));
  const peopleRows: IdentityBackfillRow[] = preview ? [...preview.rows,
    ...linkedPeople.filter((member) => !preview.rows.some((row) => row.org_member_id === member.org_member_id))
      .map((member) => ({ org_member_id: member.org_member_id, disposition: 'trusted', reason: 'active',
        user_id: member.links.find((link) => link.valid)?.user_id || null,
        subject_kinds: member.subject_kinds, selectable: false })),
  ] : [];
  const selectedBackfill = selectedPeople.filter((id) => preview?.rows.some((row) => row.org_member_id === id && row.selectable));
  const peopleNames = peopleRows.map((row) => (
    identityLinks?.members.find((member) => member.org_member_id === row.org_member_id)?.label
    || identityLinks?.accounts.find((account) => account.user_id === row.user_id)?.label
    || ''
  ));
  const repeatedPeople = repeatedNames(peopleNames);
  const repeatedDepartments = repeatedNames((directory?.org_departments ?? []).map((row) => row.name));

  return (
    <div id="org-department-cutover" className="card ws-org-cutover">
      <h4 className="ws-org-card-title">{t('enterprise.org.cutoverTitle')}</h4>
      <p className="ws-org-card-desc">{t('enterprise.org.cutoverDesc')}</p>
      {transportNotice ? <p className="ws-org-card-desc">{transportNotice}</p> : null}
      <div className="ws-org-actions">
        <button type="button" className="btn btn-secondary" onClick={load} disabled={phase === 'loading' || busy}>
          {phase === 'loading' ? t('enterprise.org.cutoverLoading') : t('enterprise.org.cutoverReview')}
        </button>
        {phase !== 'idle' ? (
          <button type="button" className="btn btn-secondary" onClick={load} disabled={phase === 'loading' || busy}>
            {t('enterprise.org.cutoverRetry')}
          </button>
        ) : null}
      </div>
      {error ? <p className="ws-org-card-desc">{error}</p> : null}
      {counts ? (
        <div>
          <p>
            {t('enterprise.org.cutoverCountsLinks', {
              applied: counts.link_backfill.applied ?? 0,
              noop: counts.link_backfill.noop,
              skipped: counts.link_backfill.skipped,
              conflict: counts.link_backfill.conflict,
              unavailable: counts.link_backfill.unavailable,
            })}
          </p>
          <p>
            {t('enterprise.org.cutoverCountsDepartments', {
              created: counts.department_mappings.created ?? 0,
              mapped: counts.department_mappings.mapped ?? 0,
              revoked: counts.department_mappings.revoked ?? 0,
              noop: counts.department_mappings.noop,
              skipped: counts.department_mappings.skipped,
              conflict: counts.department_mappings.conflict,
              unavailable: counts.department_mappings.unavailable,
            })}
          </p>
          <p>
            {t('enterprise.org.cutoverCountsAssignments', {
              changed: counts.user_assignments.changed ?? 0,
              noop: counts.user_assignments.noop,
              skipped: counts.user_assignments.skipped,
              conflict: counts.user_assignments.conflict,
              unavailable: counts.user_assignments.unavailable,
            })}
          </p>
        </div>
      ) : null}
      {preview ? (
        <div>
          <h5>{t('enterprise.org.cutoverPeople')}</h5>
          {peopleRows.length === 0 ? <p>{t('enterprise.org.cutoverEmptyPeople')}</p> : null}
          {peopleRows.map((row) => (
            <label key={row.org_member_id} className="ws-org-choice">
              <input
                type="checkbox"
                disabled={(!row.selectable && !linkedPersonIds.has(row.org_member_id)) || busy}
                checked={selectedPeople.includes(row.org_member_id)}
                onChange={() => toggle(row.org_member_id, selectedPeople, setSelectedPeople)}
              />
              <span>
                <span>{personTitle(identityLinks, row.org_member_id, row.user_id || '', repeatedPeople, t)}</span>
                {' '}
                <span>{previewContext(row, identityLinks, t)}</span>
                {' '}
                <span>{row.selectable ? t('enterprise.org.disposition.trusted')
                  : linkedPersonIds.has(row.org_member_id)
                    ? t('enterprise.org.identityStatusActive') : cutoverReason(row.reason, t)}</span>
              </span>
            </label>
          ))}
          <button
            type="button"
            className="btn btn-primary"
            disabled={busy || selectedBackfill.length === 0}
            onClick={() => run(() => enterpriseApi.applyIdentityBackfill(selectedBackfill, tenantId || undefined))}
          >
            {t('enterprise.org.cutoverApplyLinks')}
          </button>
          {selectedPeople.length === 0 ? <p>{t('enterprise.org.cutoverNoSelection')}</p> : null}
        </div>
      ) : null}
      {directory ? (
        <div>
          <h5>{t('enterprise.org.cutoverDepartments')}</h5>
          {directory.org_departments.map((row) => {
            const selectId = `cutover-dept-${row.org_department_id}`;
            const mappedId = row.link?.department_id || '';
            return (
            <div key={row.org_department_id} className="ws-org-form-row">
              <div>
                {distinguishable(t, row.name, row.org_department_id, repeatedDepartments.has(row.name))}
                {row.provider ? ` · ${row.provider === 'dingtalk' ? t('companyDirectory.dingtalk')
                  : row.provider === 'wecom' ? t('companyDirectory.wecom') : 'Feishu / Lark'}` : ''}
                {row.provider_department_id ? ` · ${row.provider_department_id}` : ''}
              </div>
              {row.proposal ? (
                <div>
                  {t('enterprise.org.cutoverProposal')}
                  {' '}
                  {row.proposal.selectable ? row.name : cutoverReason(row.proposal.reason, t)}
                </div>
              ) : null}
              {row.link ? (
                <>
                  <div>{row.link.valid === false ? cutoverReason(row.reason || '', t) : t('enterprise.org.identityStatusActive')}</div>
                  <div>
                    {t('enterprise.org.cutoverCurrentDepartment', {
                      department: departmentText(directory, mappedId, t),
                    })}
                  </div>
                </>
              ) : null}
              <label className="ws-org-label" htmlFor={selectId}>{t('enterprise.org.cutoverChooseDepartment')}</label>
              <select
                id={selectId}
                aria-label={t('enterprise.org.cutoverChooseDepartment')}
                value={choices[row.org_department_id] || ''}
                onChange={(event) => setChoices({ ...choices, [row.org_department_id]: event.target.value })}
              >
                <option value="">{t('enterprise.org.cutoverChooseDepartment')}</option>
                {directory.departments.map((department) => (
                  <option key={department.id} value={department.id}>{department.name}</option>
                ))}
              </select>
              <div className="ws-org-actions">
                {row.proposal?.selectable ? (
                  <button
                    type="button"
                    className="btn btn-secondary"
                    disabled={busy}
                    onClick={() => run(() => enterpriseApi.applyDepartmentLinks([
                      { org_department_id: row.org_department_id, action: 'create' },
                    ], tenantId || undefined))}
                  >
                    {t('enterprise.org.cutoverCreate')}
                  </button>
                ) : null}
                {!row.link && choices[row.org_department_id] ? (
                  <button
                    type="button"
                    className="btn btn-secondary"
                    disabled={busy || Boolean(row.reason)}
                    onClick={() => run(() => enterpriseApi.applyDepartmentLinks([
                      {
                        org_department_id: row.org_department_id,
                        action: 'link',
                        department_id: choices[row.org_department_id],
                      },
                    ], tenantId || undefined))}
                  >
                    {t('enterprise.org.cutoverLink')}
                  </button>
                ) : null}
                {row.link && choices[row.org_department_id] ? (
                  <button
                    type="button"
                    className="btn btn-secondary"
                    disabled={busy || row.link.valid === false}
                    onClick={() => run(() => enterpriseApi.applyDepartmentLinks([
                      { action: 'repair', link_id: row.link?.id, department_id: choices[row.org_department_id] },
                    ], tenantId || undefined))}
                  >
                    {t('enterprise.org.cutoverRepairMapping')}
                  </button>
                ) : null}
                {row.link ? (
                  <button
                    type="button"
                    className="btn btn-secondary"
                    disabled={busy}
                    onClick={() => run(() => enterpriseApi.applyDepartmentLinks([
                      { action: 'revoke', link_id: row.link?.id },
                    ], tenantId || undefined))}
                  >
                    {t('enterprise.org.cutoverRevokeMapping')}
                  </button>
                ) : null}
              </div>
            </div>
            );
          })}
          <button
            type="button"
            className="btn btn-primary"
            disabled={busy || selectedPeople.length === 0}
            onClick={() => run(() => enterpriseApi.applyDepartmentCutover(selectedPeople, tenantId || undefined))}
          >
            {t('enterprise.org.cutoverApplyAssignments')}
          </button>
        </div>
      ) : null}
      {result ? (
        <div>
          {result.rows.map((row, index) => {
            const outcome = String(row.outcome || '');
            const reason = String(row.reason || '');
            const nested = nestedIdentity(row);
            const objectKind = String(row.object_kind || '');
            const memberId = String(row.org_member_id || nested?.org_member_id || '');
            const userId = String(row.user_id || nested?.user_id || (objectKind === 'org_member_user_link' ? '' : row.object_id || ''));
            const person = String(row.person_label || '')
              || personTitle(identityLinks, memberId, userId, repeatedPeople, t);
            const currentName = String(row.department_name || '');
            const mappedName = String(row.mapped_department_name || '');
            const currentId = String(row.department_id || '');
            const targetId = String(row.mapped_department_id || '');
            const conflict = outcome === 'conflict' && reason === 'department_id_points_elsewhere';
            const currentShown = conflict
              ? (currentName || (currentId ? departmentText(directory, currentId, t) : t('enterprise.org.cutoverNoDepartment')))
              : '';
            const targetShown = mappedName
              || (targetId ? departmentText(directory, targetId, t) : '')
              || (conflict ? '' : currentName);
            const repairId = `cutover-repair-${userId || index}`;
            return (
              <div key={`${userId}-${index}`}>
                <div>{person}</div>
                <div>{cutoverOutcome(outcome, t)}</div>
                <div>{cutoverReason(reason, t)}</div>
                {currentShown ? (
                  <div>{t('enterprise.org.cutoverCurrentDepartment', { department: currentShown })}</div>
                ) : null}
                {targetShown ? (
                  <div>{t('enterprise.org.cutoverTargetDepartment', { department: targetShown })}</div>
                ) : null}
                {conflict ? (
                  <div className="ws-org-actions">
                    <label className="ws-org-label" htmlFor={repairId}>{t('enterprise.org.cutoverChooseDepartment')}</label>
                    <select
                      id={repairId}
                      aria-label={t('enterprise.org.cutoverCorrect')}
                      value={choices[userId] || ''}
                      onChange={(event) => setChoices({ ...choices, [userId]: event.target.value })}
                    >
                      <option value="">{t('enterprise.org.cutoverChooseDepartment')}</option>
                      {(directory?.departments || []).map((department) => (
                        <option key={department.id} value={department.id}>{department.name}</option>
                      ))}
                    </select>
                    <button
                      type="button"
                      className="btn btn-secondary"
                      disabled={busy || !choices[userId]}
                      onClick={() => run(() => enterpriseApi.repairDepartmentAssignment(userId, choices[userId], tenantId || undefined))}
                    >
                      {t('enterprise.org.cutoverCorrect')}
                    </button>
                  </div>
                ) : null}
              </div>
            );
          })}
        </div>
      ) : null}
      {journal ? (
        <div>
          <h5>{t('enterprise.org.cutoverHistory')}</h5>
          {journal.rows.length === 0 ? <p>{t('enterprise.org.cutoverEmptyHistory')}</p> : null}
          {journal.rows.map((row) => {
            const facts = { ...(row.before || {}), ...(row.after || {}) };
            const memberId = String(facts.org_member_id || '');
            const userId = row.object_kind === 'user_department' ? row.object_id : String(facts.user_id || '');
            const orgDepartmentId = String(facts.org_department_id || '');
            const orgDepartment = directory?.org_departments.find((item) => item.org_department_id === orgDepartmentId);
            const provider = identityLinks?.providers.find((item) => item.id === String(facts.provider_id || ''));
            const subject = row.object_kind === 'department_directory_link'
              ? distinguishable(
                t,
                orgDepartment?.name || t('enterprise.org.cutoverKindDepartment'),
                orgDepartmentId || row.object_id,
                repeatedDepartments.has(orgDepartment?.name || ''),
              )
              : personTitle(identityLinks, memberId, userId, repeatedPeople, t);
            const who = [
              subject,
              provider?.name || '',
              facts.subject_kind ? subjectKind(String(facts.subject_kind), t) : '',
            ].filter(Boolean).join(' · ');
            const when = appliedWhen(row.applied_at);
            const named = row.actor_label || (row.actor_id ? shortToken(String(row.actor_id)) : '');
            const record = shortToken(row.id);
            return (
              <label key={row.id} className="ws-org-choice">
                <input
                  type="checkbox"
                  disabled={busy}
                  checked={selectedJournals.includes(row.id)}
                  onChange={() => toggle(row.id, selectedJournals, setSelectedJournals)}
                />
                <span>
                  <span>{cutoverKind(row.object_kind, t)}</span>
                  {' '}
                  <span>{who}</span>
                  {' '}
                  <span>
                    {t('enterprise.org.cutoverJournalTransition', {
                      before: journalSide(row.before, directory, t),
                      after: journalSide(row.after, directory, t),
                    })}
                  </span>
                  {' '}
                  <span>
                    {named
                      ? t('enterprise.org.cutoverJournalActor', { actor: named, time: when, record })
                      : t('enterprise.org.cutoverJournalUnknown', { time: when, record })}
                  </span>
                </span>
              </label>
            );
          })}
          <button
            type="button"
            className="btn btn-secondary"
            disabled={busy || selectedJournals.length === 0}
            onClick={() => run(() => enterpriseApi.rollbackCutover(selectedJournals, tenantId || undefined))}
          >
            {t('enterprise.org.cutoverRollback')}
          </button>
        </div>
      ) : null}
    </div>
  );
}
