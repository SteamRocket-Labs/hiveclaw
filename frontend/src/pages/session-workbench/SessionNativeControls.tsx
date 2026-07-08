import React from 'react';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { useTranslation } from 'react-i18next';
import { IconBellRinging, IconDatabase, IconDownload, IconGitCommit, IconHierarchy, IconSettings, IconTargetArrow, IconUsersGroup } from '@tabler/icons-react';

import './SessionNativeControls.css';
import { ccParityApi, type AgentTeam } from '../../api/domains/ccParity';
import type { SessionIndex } from '../../api/domains/chat';
import { buildCompletionWakeModel } from './timelineModel';

function PanelTitle({ icon, title }: { icon: React.ReactNode; title: string }) {
  return (
    <div className="session-native-panel-title">
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

function numericValue(value: unknown): number {
  return typeof value === 'number' && Number.isFinite(value) ? value : 0;
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
  const [selectedTeamId, setSelectedTeamId] = React.useState<string | null>(null);
  const enabled = Boolean(agentId && sessionId);

  const workbenchQuery = useQuery({
    queryKey: ['session-workbench-control-plane', agentId, sessionId],
    queryFn: () => ccParityApi.getSessionWorkbench(agentId!, sessionId!),
    enabled,
    staleTime: 10_000,
  });
  const sessionWorkbench = asObject(workbenchQuery.data);

  const contextUsageQuery = useQuery({
    queryKey: ['session-workbench-context-usage', agentId, sessionId],
    queryFn: () => ccParityApi.getSessionContextUsage(agentId!, sessionId!),
    enabled,
    staleTime: 10_000,
  });
  const contextUsage = asObject(contextUsageQuery.data);
  const contextUsageCounts = asObject(contextUsage?.counts);
  const cacheDecisionCount = Array.isArray(contextUsage?.cache_decision_ledger) ? contextUsage.cache_decision_ledger.length : 0;
  const agentCycleDecisionCount = Array.isArray(contextUsage?.agent_cycle_decision_ledger)
    ? contextUsage.agent_cycle_decision_ledger.length
    : 0;
  const contextArtifactCount = Array.isArray(contextUsage?.context_artifacts) ? contextUsage.context_artifacts.length : 0;

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
      }),
    onSuccess: (team) => {
      setTeamName('');
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
    <section data-testid="session-native-controls" className="session-native-root">
      <div className="session-native-panel">
        <PanelTitle icon={<IconDownload size={14} />} title={t('sessionWorkbench.jsonExport', 'JSON export')} />
        <div className="session-native-stats">
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
        <button type="button" className="session-native-btn" disabled={!enabled || exportJson.isPending} onClick={() => exportJson.mutate()}>
          {t('sessionWorkbench.exportJson', 'Export JSON')}
        </button>
      </div>

      <div className="session-native-panel">
        <PanelTitle icon={<IconDatabase size={14} />} title={t('sessionWorkbench.contextUsage', 'Context usage')} />
        <div className="session-native-stats">
          <div>
            {t('sessionWorkbench.usedTokens', 'used tokens')}: {numericValue(contextUsage?.used_tokens)} · {t('sessionWorkbench.freeTokens', 'free tokens')}:{' '}
            {numericValue(contextUsage?.free_space_tokens)}
          </div>
          <div>
            {t('sessionWorkbench.contextCandidates', 'candidates')}: {numericValue(contextUsageCounts?.context_candidates)} · {t('sessionWorkbench.selectedContexts', 'selected')}:{' '}
            {numericValue(contextUsageCounts?.selected_contexts)}
          </div>
          <div>
            {t('sessionWorkbench.deferredTools', 'deferred tools')}: {numericValue(contextUsageCounts?.deferred_tools)} · {t('sessionWorkbench.loadedSkills', 'skills')}:{' '}
            {numericValue(contextUsageCounts?.skills)}
          </div>
          <div>
            {t('sessionWorkbench.cacheDecisions', 'cache decisions')}: {cacheDecisionCount} · {t('sessionWorkbench.toolResults', 'tool results')}:{' '}
            {Array.isArray(contextUsage?.tool_result_ledger) ? contextUsage.tool_result_ledger.length : 0}
          </div>
          <div>
            {t('sessionWorkbench.agentCycleDecisions', 'agent cycle decisions')}: {agentCycleDecisionCount} · {t('sessionWorkbench.contextArtifacts', 'context artifacts')}:{' '}
            {contextArtifactCount}
          </div>
        </div>
      </div>

      <div className="session-native-panel">
        <PanelTitle icon={<IconBellRinging size={14} />} title={t('sessionWorkbench.completionWakeInbox', 'Completion wakes')} />
        <div className="session-native-stats">
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
          <div className="session-native-empty">
            {t('sessionWorkbench.noCompletionWakes', 'No background completions yet')}
          </div>
        ) : (
          <div className="session-native-wake-list">
            {completionWakeModel.items.slice(0, 5).map((wake) => (
              <div key={wake.id} className="session-native-wake-item">
                <div className="session-native-wake-head">
                  <strong className="session-native-wake-label">
                    {wake.label}
                  </strong>
                  <span className={`session-native-wake-state${wake.state === 'failed' ? ' is-failed' : ''}`}>
                    {t(`sessionWorkbench.${wake.state}`, wake.state)}
                  </span>
                </div>
                <div className="session-native-wake-kind">
                  {wake.kind}
                  {wake.source ? ` · ${wake.source}` : ''}
                </div>
                {wake.summary && (
                  <div className="session-native-wake-summary">
                    {wake.summary}
                  </div>
                )}
              </div>
            ))}
          </div>
        )}
      </div>

      <div className="session-native-panel">
        <PanelTitle icon={<IconSettings size={14} />} title={t('sessionWorkbench.hookManagement', 'Hook management')} />
        <div className="session-native-stats">
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
            <div key={hookKey} className="session-native-hook-row">
              <span className="session-native-hook-key">
                {hookKey}
              </span>
              <button
                type="button"
                className="session-native-btn"
                disabled={!agentId || updateHook.isPending}
                onClick={() => updateHook.mutate({ hookKey, enabled: !hookEnabled })}
              >
                {hookEnabled ? t('sessionWorkbench.disableHook', 'Disable') : t('sessionWorkbench.enableHook', 'Enable')}
              </button>
            </div>
          );
        })}
      </div>

      <div className="session-native-panel">
        <PanelTitle icon={<IconTargetArrow size={14} />} title={t('sessionWorkbench.startGoal', 'Start goal')} />
        <input
          value={goalObjective}
          onChange={(event) => setGoalObjective(event.target.value)}
          placeholder={t('sessionWorkbench.goalPlaceholder', 'Define the session objective')}
          className="session-native-input"
          disabled={!enabled}
        />
        <button type="button" className="session-native-btn" disabled={!enabled || !goalObjective.trim()} onClick={() => startGoal.mutate()}>
          {t('sessionWorkbench.startGoal', 'Start goal')}
        </button>
      </div>

      <div className="session-native-panel">
        <PanelTitle icon={<IconHierarchy size={14} />} title={t('sessionWorkbench.advancedPlan', 'Advanced plan')} />
        <input
          value={planObjective}
          onChange={(event) => setPlanObjective(event.target.value)}
          placeholder={t('sessionWorkbench.planPlaceholder', 'Plan objective')}
          className="session-native-input"
          disabled={!enabled}
        />
        <button type="button" className="session-native-btn" disabled={!enabled || !planObjective.trim()} onClick={() => startPlan.mutate()}>
          {t('sessionWorkbench.createPlan', 'Create plan')}
        </button>
      </div>

      <div className="session-native-panel">
        <PanelTitle icon={<IconUsersGroup size={14} />} title={t('sessionWorkbench.createTeam', 'Create team')} />
        <input
          value={teamName}
          onChange={(event) => setTeamName(event.target.value)}
          placeholder={t('sessionWorkbench.teamNamePlaceholder', 'Team name')}
          className="session-native-input"
          disabled={!enabled}
        />
        <div className="session-native-hint">
          {t(
            'sessionWorkbench.teamCreateContainerOnly',
            'Creates the Team container only. Add teammates with spawn_subagent using team_name and name.',
          )}
        </div>
        <button type="button" className="session-native-btn" disabled={!enabled || !teamName.trim()} onClick={() => createTeam.mutate()}>
          {t('sessionWorkbench.createTeam', 'Create team')}
        </button>
        {teams.length > 0 && (
          <div className="session-native-team-list">
            {teams.map((team: AgentTeam) => (
              <div key={team.id} className="session-native-team-item">
                <div className="session-native-team-head">
                  <strong className="session-native-team-name">{team.name}</strong>
                  <span className="session-native-team-status">{team.status}</span>
                </div>
                <button type="button" className="session-native-btn" onClick={() => setSelectedTeamId(team.id)}>
                  {t('sessionWorkbench.teamWorkbench', 'Workbench')}
                </button>
                {effectiveTeamId === team.id && selectedTeamSummary && (
                  <div className="session-native-team-meta">
                    {t('sessionWorkbench.events', 'events')}: {Number(selectedTeamSummary.event_count ?? 0)} · {t('sessionWorkbench.activeMembers', 'active members')}: {Number(selectedTeamSummary.active_member_count ?? 0)}
                  </div>
                )}
                <div className="session-native-team-hint">
                  {t('sessionWorkbench.teamSpawnHint', 'Teammate entry')}: {team.teammate_creation_tool || 'spawn_subagent'} · team_name=
                  {team.teammate_creation_args?.team_name || team.name}
                </div>
                {team.members.map((member) => (
                  <div key={member.id} className="session-native-member-row">
                    <span className="session-native-member-name">
                      {member.member_name}
                    </span>
                    <button type="button" className="session-native-btn" onClick={() => enterMember.mutate({ teamId: team.id, memberId: member.id })}>
                      {t('sessionWorkbench.enter', 'Enter')}
                    </button>
                  </div>
                ))}
                {team.members.length === 0 && (
                  <div className="session-native-empty">
                    {t('sessionWorkbench.noTeamMembers', 'No teammates yet')}
                  </div>
                )}
                {team.status !== 'closed' && (
                  <button type="button" className="session-native-btn" onClick={() => closeTeam.mutate(team.id)}>
                    {t('sessionWorkbench.closeTeam', 'Close team')}
                  </button>
                )}
              </div>
            ))}
          </div>
        )}
      </div>

      <div className="session-native-panel">
        <PanelTitle icon={<IconGitCommit size={14} />} title={t('sessionWorkbench.checkpointList', 'Checkpoints')} />
        {checkpoints.length === 0 ? (
          <div className="session-native-empty">
            {t('sessionWorkbench.noCheckpoints', 'No checkpoints yet')}
          </div>
        ) : (
          <div className="session-native-checkpoint-list">
            {checkpoints.map((checkpoint, index) => (
              <div key={String(checkpoint.id ?? index)} className="session-native-checkpoint-row">
                {checkpointLabel(checkpoint, index)}
              </div>
            ))}
          </div>
        )}
      </div>
    </section>
  );
}
