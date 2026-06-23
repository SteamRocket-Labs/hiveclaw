import React from 'react';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { useTranslation } from 'react-i18next';
import { IconGitCommit, IconHierarchy, IconTargetArrow, IconUsersGroup } from '@tabler/icons-react';

import { ccParityApi, type AgentTeam } from '../../api/domains/ccParity';
import type { SessionIndex } from '../../api/domains/chat';

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
  const enabled = Boolean(agentId && sessionId);

  const teamsQuery = useQuery({
    queryKey: ['session-workbench-teams', agentId, sessionId],
    queryFn: () => ccParityApi.listTeams(agentId!, sessionId),
    enabled,
    staleTime: 10_000,
  });
  const teams = Array.isArray(teamsQuery.data) ? teamsQuery.data : [];

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
    onSuccess: () => {
      setTeamName('');
      setMemberRole('');
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
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ['session-workbench-teams', agentId, sessionId] }),
  });

  const checkpoints = sessionIndex?.checkpoints || [];

  return (
    <section data-testid="session-native-controls" style={{ display: 'grid', gap: '10px' }}>
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
