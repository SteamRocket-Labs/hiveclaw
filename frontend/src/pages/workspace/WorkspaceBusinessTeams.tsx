import { useEffect, useRef, useState } from 'react';
import { useQuery, useQueryClient } from '@tanstack/react-query';
import { useTranslation } from 'react-i18next';
import { businessTeamsApi, type BusinessTeamDetail, type BusinessTeamList, type BusinessTeamPeople } from '../../api/domains/businessTeams';
import { chatApi } from '../../api/domains/chat';
import { ApiError } from '../../api/core/errors';
import type { Agent } from '../../types';
import { useAuthStore } from '../../stores';
import WorkspaceApprovalsSection from './WorkspaceApprovalsSection';
import TeamRuntimeBudgets from './TeamRuntimeBudgets';

type SignedInUser = {
  id?: string;
  role?: string;
  tenant_id?: string | null;
  department_id?: string | null;
  display_name?: string;
} | null;

export function privateWorkSessionHref(agentId: string, sessionId: string): string {
  return `/agents/${encodeURIComponent(agentId)}?session_id=${encodeURIComponent(sessionId)}#chat`;
}

export function normalizeBusinessTeamList(data: unknown): BusinessTeamList | null {
  if (!data || Array.isArray(data) || typeof data !== 'object') return null;
  const record = data as Partial<BusinessTeamList>;
  if (!Array.isArray(record.teams) || typeof record.tenant_id !== 'string' || !record.caller) return null;
  return record as BusinessTeamList;
}

export function normalizePeople(data: unknown): BusinessTeamPeople | null {
  if (!data || Array.isArray(data) || typeof data !== 'object') return null;
  const record = data as Partial<BusinessTeamPeople>;
  if (!Array.isArray(record.people)) return null;
  return { tenant_id: String(record.tenant_id || ''), people: record.people };
}

export function entryAllowed(
  agent: Pick<Agent, 'access_level' | 'action_capabilities'> | undefined,
  execution: string,
  callerCanUse?: boolean | null,
): boolean {
  // Team execution stays the team grant. This caller's current authority can
  // still come from an independent company, user, or department grant.
  if (typeof callerCanUse === 'boolean') return callerCanUse;
  if (agent) {
    const canUse = agent.action_capabilities?.can_use;
    if (typeof canUse === 'boolean') return canUse;
    return agent.access_level === 'use' || agent.access_level === 'manage';
  }
  return execution === 'allowed';
}

function viewerOwnsEmployee(agent: Agent, userId: string | undefined): boolean {
  if (!userId) return false;
  if (agent.owner_user_id) return agent.owner_user_id === userId;
  return agent.is_owner === true;
}

export function staleTenantResponse(
  startedTenant: string | null,
  currentTenant: string | null,
  payloadTenant: string | null,
  role: string | null,
): boolean {
  if (startedTenant !== currentTenant) return true;
  if (role === 'platform_admin') return false;
  return Boolean(payloadTenant && currentTenant && payloadTenant !== currentTenant);
}

type RequestScope = { company: string | null; userId: string | null };

function readStoredCompany(): string | null {
  try {
    return localStorage.getItem('current_tenant_id') || null;
  } catch {
    return null;
  }
}

function readRequestScope(fallbackCompany: string | null, fallbackUserId: string | null): RequestScope {
  const store = useAuthStore as unknown as { getState?: () => { user?: SignedInUser } };
  const live = typeof store.getState === 'function' ? store.getState()?.user : undefined;
  return {
    company: readStoredCompany() ?? live?.tenant_id ?? fallbackCompany,
    userId: live?.id ?? fallbackUserId,
  };
}

function requestScopeMoved(
  started: RequestScope,
  current: RequestScope,
  payloadTenant: string | null,
  role: string | null,
): boolean {
  if (started.userId !== current.userId) return true;
  return staleTenantResponse(started.company, current.company, payloadTenant, role);
}

function errorText(error: unknown, fallback: string): string {
  if (error instanceof ApiError && error.message) return error.message;
  return fallback;
}

function personLabel(person: { display_name: string; department_name?: string | null; email?: string | null }): string {
  const department = person.department_name ? ` · ${person.department_name}` : '';
  return `${person.display_name}${department}`;
}

export function WorkspaceBusinessTeams({ agents }: { agents: Agent[] }) {
  const { t } = useTranslation();
  const queryClient = useQueryClient();
  const user = useAuthStore((state) => state.user) as SignedInUser;
  const [companyGeneration, setCompanyGeneration] = useState(0);
  const scope = readRequestScope(user?.tenant_id ?? null, user?.id ?? null);
  const scopeKey = `${scope.company ?? ''}|${scope.userId ?? ''}|${companyGeneration}`;
  const seenScope = useRef<string | null>(null);
  const listQuery = useQuery({
    queryKey: ['business-teams', scope.company, scope.userId],
    queryFn: () => businessTeamsApi.list(),
  });
  const leadQuery = useQuery({
    queryKey: ['business-team-lead-candidates', scope.company, scope.userId],
    queryFn: () => businessTeamsApi.leadCandidates(),
    enabled: user?.role === 'org_admin' || user?.role === 'platform_admin',
  });
  const [detail, setDetail] = useState<BusinessTeamDetail | null>(null);
  const [approvalTeamId, setApprovalTeamId] = useState<string | null>(null);
  const [budgetTeamId, setBudgetTeamId] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [pending, setPending] = useState(false);
  const [stale, setStale] = useState(false);
  const [name, setName] = useState('');
  const [alignDepartment, setAlignDepartment] = useState(false);
  const [installName, setInstallName] = useState('');
  const [leadId, setLeadId] = useState('');
  const [memberId, setMemberId] = useState('');
  const [memberRole, setMemberRole] = useState<'member' | 'lead'>('member');
  const [teamStatus, setTeamStatus] = useState('active');
  const [agentId, setAgentId] = useState('');
  const [sessionHref, setSessionHref] = useState<string | null>(null);
  const listed = normalizeBusinessTeamList(listQuery.data);
  const selectedId = detail?.id ?? null;
  const candidateQuery = useQuery({
    queryKey: ['business-team-member-candidates', scope.company, scope.userId, selectedId],
    queryFn: () => businessTeamsApi.memberCandidates(selectedId || ''),
    enabled: Boolean(selectedId && detail?.can_manage),
  });
  const role = user?.role ?? null;
  const canInstall = Boolean(listed?.caller.can_install || role === 'org_admin' || role === 'platform_admin');
  const canCreate = listed?.caller.can_create !== false;
  const leads = normalizePeople(leadQuery.data);
  const candidates = normalizePeople(candidateQuery.data);
  const ownedAgents = (Array.isArray(agents) ? agents : []).filter((agent) => (
    canInstall || viewerOwnsEmployee(agent, user?.id)
  ));

  useEffect(() => {
    function onStorage(event: StorageEvent) {
      if (event.key !== 'current_tenant_id') return;
      setCompanyGeneration((value) => value + 1);
    }
    window.addEventListener('storage', onStorage);
    return () => window.removeEventListener('storage', onStorage);
  }, []);

  useEffect(() => {
    if (seenScope.current === null) {
      seenScope.current = scopeKey;
      return;
    }
    if (seenScope.current === scopeKey) return;
    seenScope.current = scopeKey;
    setDetail(null);
    setSessionHref(null);
    setError(null);
    setName('');
    setAlignDepartment(false);
    setInstallName('');
    setLeadId('');
    setMemberId('');
    setMemberRole('member');
    setTeamStatus('active');
    setAgentId('');
    setPending(false);
  }, [scopeKey]);

  function currentScope(): RequestScope {
    return readRequestScope(user?.tenant_id ?? null, user?.id ?? null);
  }

  function liveRole(): string | null {
    const store = useAuthStore as unknown as { getState?: () => { user?: SignedInUser } };
    const live = typeof store.getState === 'function' ? store.getState()?.user : undefined;
    return live?.role ?? role;
  }

  function scopeMovedSince(started: RequestScope, payloadTenant: string | null): boolean {
    return requestScopeMoved(started, currentScope(), payloadTenant, liveRole());
  }

  async function accept<T extends { tenant_id?: string; visible?: boolean; access?: string }>(task: () => Promise<T>): Promise<T | null> {
    const started = currentScope();
    setPending(true);
    setError(null);
    try {
      const result = await task();
      if (scopeMovedSince(started, result.tenant_id ?? null)) {
        setStale(true);
        return null;
      }
      if (result.visible === false || result.access === 'lost') {
        setDetail(null);
        setSessionHref(null);
        setMemberId('');
        setAgentId('');
        setError(t('featureHub.businessTeams.accessLost', 'Your change was saved, but this team is no longer available to you. Refresh to continue.'));
        await queryClient.invalidateQueries({ queryKey: ['business-teams'] });
        if (scopeMovedSince(started, result.tenant_id ?? null)) {
          setError(null);
          setStale(true);
        }
        return null;
      }
      await queryClient.invalidateQueries({ queryKey: ['business-teams'] });
      await queryClient.invalidateQueries({ queryKey: ['agents'] });
      if (scopeMovedSince(started, result.tenant_id ?? null)) {
        setDetail(null);
        setSessionHref(null);
        setStale(true);
        return null;
      }
      return result;
    } catch (caught) {
      if (scopeMovedSince(started, null)) {
        setStale(true);
        return null;
      }
      setError(errorText(caught, t('featureHub.businessTeams.retry', 'Could not save this change. Refresh and try again.')));
      return null;
    } finally {
      setPending(false);
    }
  }

  async function createTeam() {
    const created = await accept(() => businessTeamsApi.create({
      name,
      department_id: alignDepartment && user?.department_id ? user.department_id : null,
    }));
    if (created) {
      setDetail(created);
      setTeamStatus(created.status);
      setName('');
      setSessionHref(null);
    }
  }

  async function installTeam() {
    const created = await accept(() => businessTeamsApi.install({ name: installName, first_lead_user_id: leadId }));
    if (created) {
      setDetail(created);
      setTeamStatus(created.status);
      setInstallName('');
      setSessionHref(null);
    }
  }

  async function openTeam(teamId: string) {
    const next = await accept(() => businessTeamsApi.read(teamId));
    if (next) {
      setDetail(next);
      setTeamStatus(next.status);
      setSessionHref(null);
    }
  }

  async function refresh() {
    setStale(false);
    setError(null);
    await queryClient.invalidateQueries({ queryKey: ['business-teams'] });
    if (detail) await openTeam(detail.id);
  }

  async function openConversation(employeeId: string) {
    const started = currentScope();
    setPending(true);
    setError(null);
    try {
      const session = await chatApi.createSession(employeeId);
      if (requestScopeMoved(started, currentScope(), null, role)) {
        setStale(true);
        return;
      }
      setSessionHref(privateWorkSessionHref(employeeId, String(session.id)));
    } catch (caught) {
      if (requestScopeMoved(started, currentScope(), null, role)) {
        setStale(true);
        return;
      }
      setError(errorText(caught, t('featureHub.businessTeams.executionDenied', 'You can see this employee, but use is denied.')));
    } finally {
      setPending(false);
    }
  }

  function statusLabel(status: string): string {
    if (status === 'active') return t('featureHub.businessTeams.statusActive', 'Active');
    if (status === 'suspended') return t('featureHub.businessTeams.statusSuspended', 'Suspended');
    if (status === 'archived') return t('featureHub.businessTeams.statusArchived', 'Archived');
    if (status === 'left') return t('featureHub.businessTeams.statusLeft', 'Left');
    return t('featureHub.businessTeams.statusUnknown', 'Unavailable');
  }

  function roleLabel(value: string | null): string {
    if (value === 'lead') return t('featureHub.businessTeams.roleLead', 'Lead');
    if (value === 'member') return t('featureHub.businessTeams.roleMember', 'Member');
    return t('featureHub.businessTeams.statusUnknown', 'Unavailable');
  }

  if (stale) {
    return (
      <section className="business-team-panel" aria-labelledby="business-team-stale">
        <h2 id="business-team-stale">{t('featureHub.businessTeams.title', 'Business teams')}</h2>
        <p>{t('featureHub.businessTeams.tenantChanged', 'The company changed before this result arrived. Refresh to load the current company.')}</p>
        <button type="button" onClick={() => { setStale(false); void refresh(); }}>
          {t('featureHub.businessTeams.refresh', 'Refresh')}
        </button>
      </section>
    );
  }

  return (
    <section className="business-team-panel" aria-labelledby="business-team-title">
      <div className="business-team-heading">
        <h2 id="business-team-title">{t('featureHub.businessTeams.title', 'Business teams')}</h2>
        <p>{t('featureHub.businessTeams.description', 'Create a team, choose its people, and open your own private conversation with an employee the team is allowed to use.')}</p>
      </div>
      {error && <p className="business-team-error" role="alert">{error}</p>}
      {pending && <p className="business-team-pending" role="status">{t('featureHub.businessTeams.pending', 'Saving...')}</p>}
      {listQuery.isLoading && !listed && <p>{t('featureHub.businessTeams.loading', 'Loading business teams...')}</p>}
      <div className="business-team-layout">
        <div className="business-team-column">
          {canCreate && (
            <form className="business-team-form" onSubmit={(event) => { event.preventDefault(); void createTeam(); }}>
              <h3>{t('featureHub.businessTeams.createTitle', 'Create a team')}</h3>
              <label htmlFor="business-team-name">{t('featureHub.businessTeams.nameLabel', 'Team name')}</label>
              <input id="business-team-name" className="form-input" value={name} onChange={(event) => setName(event.target.value)} required />
              {user?.department_id && (
                <label className="business-team-check">
                  <input type="checkbox" checked={alignDepartment} onChange={(event) => setAlignDepartment(event.target.checked)} />
                  {t('featureHub.businessTeams.alignDepartment', 'Align this team with my department')}
                </label>
              )}
              <button type="submit" className="btn btn-primary" disabled={pending || !name.trim()}>
                {t('featureHub.businessTeams.create', 'Create team')}
              </button>
            </form>
          )}
          {canInstall && (
            <form className="business-team-form" onSubmit={(event) => { event.preventDefault(); void installTeam(); }}>
              <h3>{t('featureHub.businessTeams.installTitle', 'Install a team for a lead')}</h3>
              <label htmlFor="business-team-install-name">{t('featureHub.businessTeams.nameLabel', 'Team name')}</label>
              <input id="business-team-install-name" className="form-input" value={installName} onChange={(event) => setInstallName(event.target.value)} required />
              <label htmlFor="business-team-lead">{t('featureHub.businessTeams.leadLabel', 'First lead')}</label>
              <select id="business-team-lead" className="form-input" value={leadId} onChange={(event) => setLeadId(event.target.value)} required>
                <option value="">{t('featureHub.businessTeams.leadLabel', 'First lead')}</option>
                {(leads?.people ?? []).map((person) => (
                  <option key={person.id} value={person.id}>{personLabel(person)}</option>
                ))}
              </select>
              <button type="submit" className="btn btn-primary" disabled={pending || !installName.trim() || !leadId}>
                {t('featureHub.businessTeams.install', 'Install team')}
              </button>
            </form>
          )}
          <h3 id="business-team-list">{t('featureHub.businessTeams.teamList', 'Your teams')}</h3>
          {!listQuery.isLoading && (listed?.teams.length ?? 0) === 0 && (
            <p>{t('featureHub.businessTeams.empty', 'No business team yet. Create one to start working with an employee.')}</p>
          )}
          <ul className="business-team-list" aria-labelledby="business-team-list">
            {(listed?.teams ?? []).map((team) => (
              <li key={team.id}>
                <button type="button" aria-pressed={detail?.id === team.id} onClick={() => { void openTeam(team.id); }} disabled={pending}>
                  <strong>{team.name}</strong>
                  <small>
                    {statusLabel(team.status)}
                    {' · '}
                    {t('featureHub.businessTeams.createdBy', 'Created by {{name}}', { name: team.created_by_display_name })}
                    {team.department_name ? ` · ${team.department_name}` : ''}
                  </small>
                </button>
              </li>
            ))}
          </ul>
          <button type="button" className="btn btn-ghost" onClick={() => { void refresh(); }} disabled={pending}>
            {t('featureHub.businessTeams.refresh', 'Refresh')}
          </button>
        </div>
        {detail && (
          <div className="business-team-detail">
            <h3>{detail.name}</h3>
            <p>
              {statusLabel(detail.status)}
              {detail.department_name ? ` · ${detail.department_name}` : ''}
              {' · '}
              {t('featureHub.businessTeams.createdBy', 'Created by {{name}}', { name: detail.created_by_display_name })}
            </p>
            {detail.can_manage && (
              <div>
                <button type="button" className="btn btn-ghost" aria-expanded={approvalTeamId === detail.id}
                  onClick={() => setApprovalTeamId(approvalTeamId === detail.id ? null : detail.id)}>
                  {t('agent.tabs.approvals', 'Approvals')}
                </button>
                {approvalTeamId === detail.id && <WorkspaceApprovalsSection key={`${scopeKey}:${detail.id}`}
                  selectedTenantId={detail.tenant_id} businessTeamId={detail.id} />}
                <button type="button" className="btn btn-ghost" aria-expanded={budgetTeamId === detail.id}
                  onClick={() => setBudgetTeamId(budgetTeamId === detail.id ? null : detail.id)}>
                  {t('teamBudgets.title', 'Team runtime budgets')}
                </button>
                {budgetTeamId === detail.id && <TeamRuntimeBudgets key={`${scopeKey}:${detail.id}`}
                  teamId={detail.id} tenantId={detail.tenant_id} />}
              </div>
            )}
            {detail.can_manage && (
              <form className="business-team-form" onSubmit={(event) => {
                event.preventDefault();
                void accept(() => businessTeamsApi.update(detail.id, { status: teamStatus })).then((next) => {
                  if (next) setDetail(next);
                });
              }}>
                <label htmlFor="business-team-status">{t('featureHub.businessTeams.teamStatus', 'Team status')}</label>
                <select id="business-team-status" className="form-input" value={teamStatus} onChange={(event) => setTeamStatus(event.target.value)}>
                  <option value="active">{statusLabel('active')}</option>
                  <option value="suspended">{statusLabel('suspended')}</option>
                  <option value="archived">{statusLabel('archived')}</option>
                </select>
                <button type="submit" className="btn btn-ghost" disabled={pending}>{t('featureHub.businessTeams.saveStatus', 'Save status')}</button>
              </form>
            )}
            <h4>{t('featureHub.businessTeams.membersTitle', 'People')}</h4>
            <ul className="business-team-members">
              {detail.members.map((member) => (
                <li key={member.id}>
                  <strong>{member.display_name}</strong>
                  <small>
                    {roleLabel(member.role)}
                    {' · '}
                    {statusLabel(member.status)}
                    {' · '}
                    {member.registered
                      ? (member.authority
                        ? t('featureHub.businessTeams.hasAuthority', 'Can act for this team')
                        : t('featureHub.businessTeams.noAuthority', 'No team authority'))
                      : t('featureHub.businessTeams.unregistered', 'Unregistered directory member')}
                    {member.email ? ` · ${member.email}` : ''}
                  </small>
                  {detail.can_manage && member.status === 'active' && (
                    <span className="business-team-actions">
                      {member.registered && member.role !== 'lead' && (
                        <button type="button" disabled={pending} onClick={() => {
                          void accept(() => businessTeamsApi.updateMember(detail.id, member.id, { role: 'lead' })).then((next) => next && setDetail(next));
                        }}>{t('featureHub.businessTeams.makeLead', 'Make lead')}</button>
                      )}
                      {member.role === 'lead' && (
                        <button type="button" disabled={pending} onClick={() => {
                          void accept(() => businessTeamsApi.updateMember(detail.id, member.id, { role: 'member' })).then((next) => next && setDetail(next));
                        }}>{t('featureHub.businessTeams.makeMember', 'Make member')}</button>
                      )}
                      <button type="button" disabled={pending} onClick={() => {
                        void accept(() => businessTeamsApi.updateMember(detail.id, member.id, { status: 'suspended' })).then((next) => next && setDetail(next));
                      }}>{t('featureHub.businessTeams.suspendMember', 'Suspend')}</button>
                      <button type="button" disabled={pending} onClick={() => {
                        void accept(() => businessTeamsApi.updateMember(detail.id, member.id, { status: 'left' })).then((next) => next && setDetail(next));
                      }}>{t('featureHub.businessTeams.markLeft', 'Mark left')}</button>
                    </span>
                  )}
                </li>
              ))}
            </ul>
            {detail.can_manage && (
              <form className="business-team-form" onSubmit={(event) => {
                event.preventDefault();
                void accept(() => businessTeamsApi.addMember(detail.id, { user_id: memberId, role: memberRole })).then((next) => next && setDetail(next));
              }}>
                <label htmlFor="business-team-person">{t('featureHub.businessTeams.addPerson', 'Add a person')}</label>
                <select id="business-team-person" className="form-input" value={memberId} onChange={(event) => setMemberId(event.target.value)} required>
                  <option value="">{t('featureHub.businessTeams.addPerson', 'Add a person')}</option>
                  {(candidates?.people ?? []).map((person) => (
                    <option key={person.id} value={person.id}>{personLabel(person)}</option>
                  ))}
                </select>
                <label htmlFor="business-team-role">{t('featureHub.businessTeams.roleMember', 'Member')}</label>
                <select id="business-team-role" className="form-input" value={memberRole} onChange={(event) => setMemberRole(event.target.value as 'member' | 'lead')}>
                  <option value="member">{roleLabel('member')}</option>
                  <option value="lead">{roleLabel('lead')}</option>
                </select>
                <button type="submit" className="btn btn-primary" disabled={pending || !memberId}>
                  {t('featureHub.businessTeams.addMember', 'Add to team')}
                </button>
              </form>
            )}
            <h4>{t('featureHub.businessTeams.employeesTitle', 'Employees this team can use')}</h4>
            {detail.affiliations.length === 0 && <p>{t('featureHub.businessTeams.noEmployees', 'No employee has been shared with this team.')}</p>}
            <ul className="business-team-employees">
              {detail.affiliations.map((affiliation) => {
                const agent = agents.find((item) => item.id === affiliation.agent_id);
                const allowed = entryAllowed(agent, affiliation.execution, affiliation.caller_can_use);
                const canChangeShare = canInstall || (agent ? viewerOwnsEmployee(agent, user?.id) : false);
                const effectiveUse = allowed ? 'read_and_execute' : 'limited';
                return (
                  <li key={affiliation.agent_id}>
                    <strong>{affiliation.agent_name}</strong>
                    <small>
                      {t('featureHub.businessTeams.ownerLabel', 'Owner: {{name}}', { name: affiliation.owner_display_name })}
                      {' · '}
                      {effectiveUse === 'read_and_execute'
                        ? t('featureHub.businessTeams.useAccess', 'Use access: members can read and start their own work')
                        : t('featureHub.businessTeams.limitedAccess', 'Limited access: use is not currently allowed')}
                    </small>
                    <p>{t('featureHub.businessTeams.privateSession', 'This is your private conversation. Opening it from the team does not share it with other members.')}</p>
                    {allowed ? (
                      <button type="button" disabled={pending} onClick={() => { void openConversation(affiliation.agent_id); }}>
                        {t('featureHub.businessTeams.openConversation', 'Open my conversation')}
                      </button>
                    ) : (
                      <div>
                        <p>{t('featureHub.businessTeams.executionDenied', 'You can see this employee, but use is denied.')}</p>
                        <button type="button" onClick={() => { void refresh(); }}>{t('featureHub.businessTeams.refresh', 'Refresh')}</button>
                      </div>
                    )}
                    {canChangeShare && (
                      <button type="button" disabled={pending} onClick={() => {
                        void accept(() => businessTeamsApi.unshareAgent(detail.id, affiliation.agent_id)).then((next) => next && setDetail(next));
                      }}>{t('featureHub.businessTeams.removeShare', 'Remove from this team')}</button>
                    )}
                  </li>
                );
              })}
            </ul>
            {sessionHref && (
              <a href={sessionHref}>{t('featureHub.businessTeams.continueConversation', 'Continue in my private conversation')}</a>
            )}
            {ownedAgents.length > 0 && (
              <form className="business-team-form" onSubmit={(event) => {
                event.preventDefault();
                void accept(() => businessTeamsApi.shareAgent(detail.id, agentId)).then((next) => next && setDetail(next));
              }}>
                <h4>{t('featureHub.businessTeams.shareTitle', 'Share an employee with this team')}</h4>
                {agentId && (
                  <p>{t('featureHub.businessTeams.shareEffect', '{{team}} members get use access to {{employee}}. Ownership does not move, and existing conversations stay private.', {
                    team: detail.name,
                    employee: ownedAgents.find((agent) => agent.id === agentId)?.name || t('featureHub.businessTeams.employeeLabel', 'Employee'),
                  })}</p>
                )}
                <label htmlFor="business-team-employee">{t('featureHub.businessTeams.employeeLabel', 'Employee')}</label>
                <select id="business-team-employee" className="form-input" value={agentId} onChange={(event) => setAgentId(event.target.value)} required>
                  <option value="">{t('featureHub.businessTeams.employeeLabel', 'Employee')}</option>
                  {ownedAgents.map((agent) => (
                    <option key={agent.id} value={agent.id}>{agent.name}</option>
                  ))}
                </select>
                <button type="submit" className="btn btn-primary" disabled={pending || !agentId}>
                  {t('featureHub.businessTeams.share', 'Share with this team')}
                </button>
              </form>
            )}
          </div>
        )}
      </div>
    </section>
  );
}
