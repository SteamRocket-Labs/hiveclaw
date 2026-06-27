import React from 'react';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { useTranslation } from 'react-i18next';
import { IconBellRinging, IconDownload, IconGitCommit, IconHierarchy, IconSettings, IconTargetArrow, IconUsersGroup } from '@tabler/icons-react';

import { ccParityApi, type AgentTeam } from '../../api/domains/ccParity';
import type { SessionIndex } from '../../api/domains/chat';
import { buildCompletionWakeModel } from './timelineModel';

function panelStyle(): React.CSSProperties {
  return {
    border: '1px solid var(--border-subtle)',
    borderRadius: '8px',
    background: 'var(--bg-secondary)',
    padding: '10px',
    display: 'grid',
    gap: '8px',
  };
}

function inputStyle(): React.CSSProperties {
  return {
    width: '100%',
    border: '1px solid var(--border-subtle)',
    borderRadius: '6px',
    background: 'var(--bg-primary)',
    color: 'var(--text-primary)',
    fontSize: '11px',
    padding: '7px 8px',
    boxSizing: 'border-box',
  };
}

function buttonStyle(): React.CSSProperties {
  return {
    border: '1px solid var(--border-subtle)',
    borderRadius: '6px',
    background: 'var(--bg-elevated)',
    color: 'var(--text-secondary)',
    fontSize: '11px',
    fontWeight: 650,
    padding: '6px 8px',
    cursor: 'pointer',
  };
}

function PanelTitle({ icon, title }: { icon: React.ReactNode; title: string }) {
  return (
    <div style={{ display: 'flex', alignItems: 'center', gap: '6px', color: 'var(--text-primary)', fontSize: '12px', fontWeight: 700 }}>
      {icon}
      <span>{title}</span>
    </div>
  );
}

function checkpointLabel(checkpoint: Record<string, unknown>, index: number): string {
  const raw = checkpoint.checkpoint_kind ?? checkpoint.kind ?? checkpoint.type ?? checkpoint.id;
  return raw == null ? `checkpoint-${index + 1}` : String(raw);
}

function asObject<T extends Record<string, unknown>>(value: unknown): T | null {
  return value && typeof value === 'object' && !Array.isArray(value) ? (value as T) : null;
}

function downloadJson(data: unknown, sessionId: string) {
  if (typeof window === 'undefined' || typeof document === 'undefined') return;
  const blob = new Blob([JSON.stringify(data, null, 2)], { type: 'application/json' });
  const url = window.URL.createObjectURL(blob);
  const anchor = document.createElement('a');
  anchor.href = url;
  anchor.download = `hive-session-${sessionId}.json`;
  anchor.click();
  window.URL.revokeObjectURL(url);
}

export default function SessionNativeControls({
  agentId,
  sessionId,
  sessionIndex,
  onEnterSession,
}: {
  agentId: string | null;
  sessionId: string | null;
  sessionIndex?: SessionIndex | null;
  onEnterSession?: (sessionId: string) => void | Promise<unknown>;
}) {
  const { t } = useTranslation();
  const queryClient = useQueryClient();
  const [goalObjective, setGoalObjective] = React.useState('');
  const [planObjective, setPlanObjective] = React.useState('');
  const [teamName, setTeamName] = React.useState('');
  const [memberRole, setMemberRole] = React.useState('');
  const [selectedTeamId, setSelectedTeamId] = React.useState<string | null>(null);
  const enabled = Boolean(agentId && sessionId);

  const workbenchQuery = useQuery({
    queryKey: ['session-workbench-control-plane', agentId, sessionId],
    queryFn: () => ccParityApi.getSessionWorkbench(agentId!, sessionId!),
    enabled,
    staleTime: 10_000,
  });
  const sessionWorkbench = asObject(workbenchQuery.data);

  const hooksQuery = useQuery({
    queryKey: ['session-workbench-hooks', agentId],
    queryFn: () => ccParityApi.listHooks(agentId!),
    enabled: Boolean(agentId),
    staleTime: 15_000,
  });
  const hookControlPlane = asObject(hooksQuery.data);
  const hookEvents = Array.isArray(hookControlPlane?.events) ? hookControlPlane.events : [];
  const hookRegistrations = Array.isArray(hookControlPlane?.registrations) ? hookControlPlane.registrations : [];

  const teamsQuery = useQuery({
    queryKey: ['session-workbench-teams', agentId, sessionId],
    queryFn: () => ccParityApi.listTeams(agentId!, sessionId),
    enabled,
    staleTime: 10_000,
  });
  const teams = Array.isArray(teamsQuery.data) ? teamsQuery.data : [];
  const effectiveTeamId = selectedTeamId || teams[0]?.id || null;
  const teamWorkbenchQuery = useQuery({
    queryKey: ['session-workbench-team-detail', agentId, effectiveTeamId],
    queryFn: () => ccParityApi.getTeamWorkbench(agentId!, effectiveTeamId!),
    enabled: Boolean(agentId && effectiveTeamId),
    staleTime: 10_000,
  });
  const selectedTeamWorkbench = asObject(teamWorkbenchQuery.data);

  const startGoal = useMutation({
    mutationFn: () => ccParityApi.startGoal(agentId!, sessionId!, { objective: goalObjective.trim() }),
    onSuccess: () => {
      setGoalObjective('');
      queryClient.invalidateQueries({ queryKey: ['chat-session-index', agentId, sessionId] });
    },
  });
  const startPlan = useMutation({
    mutationFn: () => ccParityApi.startAdvancedPlan(agentId!, sessionId!, { objective: planObjective.trim() }),
    onSuccess: () => setPlanObjective(''),
  });
  const createTeam = useMutation({
    mutationFn: () =>
      ccParityApi.createTeam(agentId!, {
        parent_session_id: sessionId!,
        name: teamName.trim(),
        members: [
          {
            name: memberRole.trim() || t('sessionWorkbench.defaultTeamMember', 'Specialist'),
            role: memberRole.trim() || t('sessionWorkbench.defaultTeamRole', 'Investigate and report back'),
          },
        ],
      }),
    onSuccess: (team) => {
      setTeamName('');
      setMemberRole('');
      setSelectedTeamId(team.id);
      queryClient.invalidateQueries({ queryKey: ['session-workbench-teams', agentId, sessionId] });
    },
  });
  const enterMember = useMutation({
    mutationFn: ({ teamId, memberId }: { teamId: string; memberId: string }) => ccParityApi.enterTeamMember(agentId!, teamId, memberId),
    onSuccess: (result) => {
      if (result.chat_session_id) {
        void onEnterSession?.(result.chat_session_id);
      }
    },
  });
  const closeTeam = useMutation({
    mutationFn: (teamId: string) => ccParityApi.closeTeam(agentId!, teamId),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['session-workbench-teams', agentId, sessionId] });
      queryClient.invalidateQueries({ queryKey: ['session-workbench-team-detail', agentId, effectiveTeamId] });
    },
  });
  const exportJson = useMutation({
    mutationFn: () => ccParityApi.exportSessionJson(agentId!, sessionId!),
    onSuccess: (payload) => downloadJson(payload, sessionId!),
  });
  const updateHook = useMutation({
    mutationFn: ({ hookKey, enabled }: { hookKey: string; enabled: boolean }) =>
      ccParityApi.updateHookRuntimeConfig(agentId!, hookKey, { enabled }),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ['session-workbench-hooks', agentId] }),
  });

  const checkpoints = sessionIndex?.checkpoints || [];
  const turn = asObject(sessionWorkbench?.turn);
  const runtimeTasks = Array.isArray(sessionWorkbench?.runtime_tasks) ? sessionWorkbench.runtime_tasks : [];
  const goals = Array.isArray(sessionWorkbench?.goals) ? sessionWorkbench.goals : [];
  const completionWakeModel = buildCompletionWakeModel(sessionWorkbench);
  const blockingHookCount = hookEvents.filter((event) => asObject(event)?.blocking_supported === true).length;
  const selectedTeamSummary = asObject(selectedTeamWorkbench?.summary);

  return (
    <section data-testid="session-native-controls" style={{ display: 'grid', gap: '10px' }}>
      <div style={panelStyle()}>
        <PanelTitle icon={<IconDownload size={14} />} title={t('sessionWorkbench.jsonExport', 'JSON export')} />
        <div style={{ display: 'grid', gap: '4px', fontSize: '11px', color: 'var(--text-secondary)' }}>
          <div>
            {t('sessionWorkbench.truthSource', 'truth source')}: {String(turn?.truth_source ?? '-')}
          </div>
          <div>
            {t('sessionWorkbench.events', 'events')}: {Number(turn?.event_count ?? 0)} · {t('sessionWorkbench.runtimeTasks', 'runtime tasks')}: {runtimeTasks.length}
          </div>
          <div>
            {t('sessionWorkbench.goals', 'goals')}: {goals.length} · {t('sessionWorkbench.agentTeams', 'agent teams')}: {teams.length}
          </div>
        </div>
        <button type="button" style={buttonStyle()} disabled={!enabled || exportJson.isPending} onClick={() => exportJson.mutate()}>
          {t('sessionWorkbench.exportJson', 'Export JSON')}
        </button>
      </div>

      <div style={panelStyle()}>
        <PanelTitle icon={<IconBellRinging size={14} />} title={t('sessionWorkbench.completionWakeInbox', 'Completion wakes')} />
        <div style={{ display: 'grid', gap: '4px', fontSize: '11px', color: 'var(--text-secondary)' }}>
          <div>
            {t('sessionWorkbench.pending', 'pending')}: {completionWakeModel.summary.pending} · {t('sessionWorkbench.running', 'running')}:{' '}
            {completionWakeModel.summary.running}
          </div>
          <div>
            {t('sessionWorkbench.completed', 'completed')}: {completionWakeModel.summary.completed} · {t('sessionWorkbench.failed', 'failed')}:{' '}
            {completionWakeModel.summary.failed}
          </div>
        </div>
        {completionWakeModel.items.length === 0 ? (
          <div style={{ fontSize: '11px', color: 'var(--text-tertiary)' }}>
            {t('sessionWorkbench.noCompletionWakes', 'No background completions yet')}
          </div>
        ) : (
          <div style={{ display: 'grid', gap: '6px' }}>
            {completionWakeModel.items.slice(0, 5).map((wake) => (
              <div key={wake.id} style={{ borderTop: '1px solid var(--border-subtle)', paddingTop: '6px', display: 'grid', gap: '3px' }}>
                <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', gap: '8px', fontSize: '11px' }}>
                  <strong style={{ minWidth: 0, color: 'var(--text-primary)', overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>
                    {wake.label}
                  </strong>
                  <span style={{ color: wake.state === 'failed' ? 'rgb(220,38,38)' : 'var(--text-tertiary)' }}>
                    {t(`sessionWorkbench.${wake.state}`, wake.state)}
                  </span>
                </div>
                <div style={{ color: 'var(--text-tertiary)', fontSize: '10px' }}>
                  {wake.kind}
                  {wake.source ? ` · ${wake.source}` : ''}
                </div>
                {wake.summary && (
                  <div style={{ color: 'var(--text-secondary)', fontSize: '11px', lineHeight: 1.4 }}>
                    {wake.summary}
                  </div>
                )}
              </div>
            ))}
          </div>
        )}
      </div>

      <div style={panelStyle()}>
        <PanelTitle icon={<IconSettings size={14} />} title={t('sessionWorkbench.hookManagement', 'Hook management')} />
        <div style={{ display: 'grid', gap: '4px', fontSize: '11px', color: 'var(--text-secondary)' }}>
          <div>
            {t('sessionWorkbench.hookEvents', 'hook events')}: {hookEvents.length} · {t('sessionWorkbench.blockingHooks', 'blocking')}: {blockingHookCount}
          </div>
          <div>
            {t('sessionWorkbench.registeredHooks', 'registered')}: {hookRegistrations.length}
          </div>
        </div>
        {hookRegistrations.slice(0, 3).map((registration) => {
          const hookKey = String(asObject(registration)?.key ?? '');
          const runtimeConfig = asObject(asObject(registration)?.runtime_config);
          const hookEnabled = runtimeConfig?.enabled !== false;
          if (!hookKey) return null;
          return (
            <div key={hookKey} style={{ display: 'flex', alignItems: 'center', gap: '6px', fontSize: '11px' }}>
              <span style={{ flex: 1, minWidth: 0, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap', color: 'var(--text-secondary)' }}>
                {hookKey}
              </span>
              <button
                type="button"
                style={buttonStyle()}
                disabled={!agentId || updateHook.isPending}
                onClick={() => updateHook.mutate({ hookKey, enabled: !hookEnabled })}
              >
                {hookEnabled ? t('sessionWorkbench.disableHook', 'Disable') : t('sessionWorkbench.enableHook', 'Enable')}
              </button>
            </div>
          );
        })}
      </div>

      <div style={panelStyle()}>
        <PanelTitle icon={<IconTargetArrow size={14} />} title={t('sessionWorkbench.startGoal', 'Start goal')} />
        <input
          value={goalObjective}
          onChange={(event) => setGoalObjective(event.target.value)}
          placeholder={t('sessionWorkbench.goalPlaceholder', 'Define the session objective')}
          style={inputStyle()}
          disabled={!enabled}
        />
        <button type="button" style={buttonStyle()} disabled={!enabled || !goalObjective.trim()} onClick={() => startGoal.mutate()}>
          {t('sessionWorkbench.startGoal', 'Start goal')}
        </button>
      </div>

      <div style={panelStyle()}>
        <PanelTitle icon={<IconHierarchy size={14} />} title={t('sessionWorkbench.advancedPlan', 'Advanced plan')} />
        <input
          value={planObjective}
          onChange={(event) => setPlanObjective(event.target.value)}
          placeholder={t('sessionWorkbench.planPlaceholder', 'Plan objective')}
          style={inputStyle()}
          disabled={!enabled}
        />
        <button type="button" style={buttonStyle()} disabled={!enabled || !planObjective.trim()} onClick={() => startPlan.mutate()}>
          {t('sessionWorkbench.createPlan', 'Create plan')}
        </button>
      </div>

      <div style={panelStyle()}>
        <PanelTitle icon={<IconUsersGroup size={14} />} title={t('sessionWorkbench.createTeam', 'Create team')} />
        <input
          value={teamName}
          onChange={(event) => setTeamName(event.target.value)}
          placeholder={t('sessionWorkbench.teamNamePlaceholder', 'Team name')}
          style={inputStyle()}
          disabled={!enabled}
        />
        <input
          value={memberRole}
          onChange={(event) => setMemberRole(event.target.value)}
          placeholder={t('sessionWorkbench.teamMemberPlaceholder', 'First member role')}
          style={inputStyle()}
          disabled={!enabled}
        />
        <button type="button" style={buttonStyle()} disabled={!enabled || !teamName.trim()} onClick={() => createTeam.mutate()}>
          {t('sessionWorkbench.createTeam', 'Create team')}
        </button>
        {teams.length > 0 && (
          <div style={{ display: 'grid', gap: '6px' }}>
            {teams.map((team: AgentTeam) => (
              <div key={team.id} style={{ borderTop: '1px solid var(--border-subtle)', paddingTop: '7px', display: 'grid', gap: '5px' }}>
                <div style={{ display: 'flex', justifyContent: 'space-between', gap: '8px', fontSize: '11px' }}>
                  <strong style={{ color: 'var(--text-primary)' }}>{team.name}</strong>
                  <span style={{ color: 'var(--text-tertiary)' }}>{team.status}</span>
                </div>
                <button type="button" style={buttonStyle()} onClick={() => setSelectedTeamId(team.id)}>
                  {t('sessionWorkbench.teamWorkbench', 'Workbench')}
                </button>
                {effectiveTeamId === team.id && selectedTeamSummary && (
                  <div style={{ fontSize: '11px', color: 'var(--text-tertiary)' }}>
                    {t('sessionWorkbench.events', 'events')}: {Number(selectedTeamSummary.event_count ?? 0)} · {t('sessionWorkbench.activeMembers', 'active members')}: {Number(selectedTeamSummary.active_member_count ?? 0)}
                  </div>
                )}
                {team.members.map((member) => (
                  <div key={member.id} style={{ display: 'flex', alignItems: 'center', gap: '6px', fontSize: '11px' }}>
                    <span style={{ flex: 1, minWidth: 0, color: 'var(--text-secondary)', overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>
                      {member.member_name}
                    </span>
                    <button type="button" style={buttonStyle()} onClick={() => enterMember.mutate({ teamId: team.id, memberId: member.id })}>
                      {t('sessionWorkbench.enter', 'Enter')}
                    </button>
                  </div>
                ))}
                {team.status !== 'closed' && (
                  <button type="button" style={buttonStyle()} onClick={() => closeTeam.mutate(team.id)}>
                    {t('sessionWorkbench.closeTeam', 'Close team')}
                  </button>
                )}
              </div>
            ))}
          </div>
        )}
      </div>

      <div style={panelStyle()}>
        <PanelTitle icon={<IconGitCommit size={14} />} title={t('sessionWorkbench.checkpointList', 'Checkpoints')} />
        {checkpoints.length === 0 ? (
          <div style={{ fontSize: '11px', color: 'var(--text-tertiary)' }}>
            {t('sessionWorkbench.noCheckpoints', 'No checkpoints yet')}
          </div>
        ) : (
          <div style={{ display: 'grid', gap: '5px' }}>
            {checkpoints.map((checkpoint, index) => (
              <div key={String(checkpoint.id ?? index)} style={{ fontSize: '11px', color: 'var(--text-secondary)' }}>
                {checkpointLabel(checkpoint, index)}
              </div>
            ))}
          </div>
        )}
      </div>
    </section>
  );
}
