import React from 'react';
import { useTranslation } from 'react-i18next';

import { ccParityApi } from '../../api/domains/ccParity';
import type { RuntimeSectionItemModel } from './timelineModel';
import './SessionAgentTeamControls.css';

const RUNNING_STATES = new Set(['created', 'pending', 'queued', 'running', 'started', 'in_progress', 'resuming']);
const RESUMABLE_STATES = new Set(['idle', 'completed', 'failed', 'blocked', 'cancelled', 'canceled', 'stopped']);

function normalizedStatus(value: unknown): string {
  return String(value || '').trim().toLowerCase();
}

function memberOutcome(member: RuntimeSectionItemModel): string {
  const metadata = member.raw.metadata && typeof member.raw.metadata === 'object'
    ? member.raw.metadata as Record<string, unknown>
    : {};
  return normalizedStatus(
    member.raw.last_turn_status
      || member.raw.lastTurnStatus
      || metadata.last_turn_status
      || metadata.lastTurnStatus,
  );
}

export function teamMemberActionState(
  teamStatus: string,
  member: RuntimeSectionItemModel,
): { canEnter: boolean; canSend: boolean; canResume: boolean } {
  const teamIsActive = normalizedStatus(teamStatus) === 'active';
  const status = normalizedStatus(member.status || member.state);
  const outcome = memberOutcome(member);
  const running = RUNNING_STATES.has(status);
  const closed = status === 'closed';
  return {
    canEnter: Boolean(member.enterable && member.childSessionId),
    canSend: teamIsActive && !closed,
    canResume: teamIsActive && !closed && !running && (RESUMABLE_STATES.has(status) || RESUMABLE_STATES.has(outcome)),
  };
}

function errorMessage(cause: unknown, fallback: string): string {
  return cause instanceof Error && cause.message ? cause.message : fallback;
}

export function SessionAgentTeamMemberControls({
  agentId,
  teamId,
  teamStatus,
  member,
  onEnter,
  onChanged,
}: {
  agentId: string;
  teamId: string;
  teamStatus: string;
  member: RuntimeSectionItemModel;
  onEnter?: () => void | Promise<unknown>;
  onChanged?: () => void | Promise<unknown>;
}) {
  const { t } = useTranslation();
  const actions = teamMemberActionState(teamStatus, member);
  const [showMessage, setShowMessage] = React.useState(false);
  const [message, setMessage] = React.useState('');
  const [pending, setPending] = React.useState<'send' | 'resume' | null>(null);
  const [error, setError] = React.useState('');

  const send = async (event: React.FormEvent) => {
    event.preventDefault();
    const content = message.trim();
    if (!content || pending) return;
    setPending('send');
    setError('');
    try {
      await ccParityApi.messageTeamMember(agentId, teamId, member.id, {
        message: content,
        display_content: content,
        interrupt: false,
      });
      setMessage('');
      setShowMessage(false);
      await onChanged?.();
    } catch (cause) {
      setError(errorMessage(cause, t('sessionWorkbench.team.messageFailed', 'Could not send work to this member.')));
    } finally {
      setPending(null);
    }
  };

  const resume = async () => {
    if (!actions.canResume || pending) return;
    setPending('resume');
    setError('');
    try {
      const instruction = 'Continue your assigned role using the existing member-session context. Review previous findings and return the next concrete result to the lead agent.';
      await ccParityApi.startTeamMemberRun(agentId, teamId, member.id, {
        content: instruction,
        display_content: t('sessionWorkbench.team.resumeDisplay', 'Resume {{member}}', { member: member.label }),
      });
      await onChanged?.();
    } catch (cause) {
      setError(errorMessage(cause, t('sessionWorkbench.team.resumeFailed', 'Could not resume this member.')));
    } finally {
      setPending(null);
    }
  };

  return (
    <div className="session-agent-team-control-stack">
      <span className="session-runtime-actions" data-testid="session-agent-team-member-actions">
        <button
          type="button"
          className="session-runtime-action-button"
          disabled={!actions.canEnter || Boolean(pending)}
          title={actions.canEnter
            ? t('sessionWorkbench.rightPanel.memberEnterTitle', 'Open member session')
            : t('sessionWorkbench.rightPanel.memberEnterDisabled', 'No member session is available')}
          onClick={() => void onEnter?.()}
        >
          {t('sessionWorkbench.rightPanel.memberEnter', 'Enter')}
        </button>
        <button
          type="button"
          className="session-runtime-action-button"
          disabled={!actions.canSend || Boolean(pending)}
          onClick={() => setShowMessage((value) => !value)}
        >
          {pending === 'send' ? t('sessionWorkbench.team.sending', 'Sending…') : t('sessionWorkbench.rightPanel.memberSend', 'Send')}
        </button>
        <button
          type="button"
          className="session-runtime-action-button"
          disabled={!actions.canResume || Boolean(pending)}
          onClick={() => void resume()}
        >
          {pending === 'resume' ? t('sessionWorkbench.team.resuming', 'Resuming…') : t('sessionWorkbench.rightPanel.memberResume', 'Resume')}
        </button>
      </span>
      {showMessage && actions.canSend && (
        <form className="session-agent-team-message-form" onSubmit={send}>
          <label>
            <span className="sr-only">{t('sessionWorkbench.team.messageLabel', 'Work for team member')}</span>
            <textarea
              rows={2}
              autoFocus
              value={message}
              placeholder={t('sessionWorkbench.team.messagePlaceholder', 'Send follow-up work…')}
              onChange={(event) => setMessage(event.target.value)}
            />
          </label>
          <button type="submit" disabled={!message.trim() || Boolean(pending)}>
            {t('sessionWorkbench.team.sendAction', 'Send work')}
          </button>
        </form>
      )}
      {error && <p className="session-agent-team-control-error" role="alert">{error}</p>}
    </div>
  );
}

function teamHasRunningMembers(team: RuntimeSectionItemModel): boolean {
  const projectedCount = Number(team.raw.running_member_count ?? team.raw.runningMemberCount);
  if (Number.isFinite(projectedCount) && projectedCount > 0) return true;
  return team.members.some((member) => RUNNING_STATES.has(normalizedStatus(member.status || member.state)));
}

export function SessionAgentTeamCloseControl({
  agentId,
  team,
  onChanged,
}: {
  agentId: string;
  team: RuntimeSectionItemModel;
  onChanged?: () => void | Promise<unknown>;
}) {
  const { t } = useTranslation();
  const status = normalizedStatus(team.status || team.state);
  const [pending, setPending] = React.useState(false);
  const [error, setError] = React.useState('');
  const projectedError = typeof team.raw.close_failure === 'string' ? team.raw.close_failure.trim() : '';
  const closing = status === 'closing' || pending;
  const canClose = status === 'active' && !teamHasRunningMembers(team) && !pending;

  React.useEffect(() => {
    setPending(false);
  }, [status, projectedError]);

  const close = async () => {
    if (!canClose) return;
    setPending(true);
    setError('');
    try {
      await ccParityApi.closeTeam(agentId, team.id);
      await onChanged?.();
    } catch (cause) {
      setError(errorMessage(cause, t('sessionWorkbench.team.closeFailed', 'Could not close this Team.')));
      setPending(false);
    }
  };

  if (status === 'closed') return null;
  return (
    <span className="session-agent-team-close-control">
      <button
        type="button"
        className="session-runtime-action-button"
        disabled={!canClose}
        title={!canClose && status === 'active' && teamHasRunningMembers(team)
          ? t('sessionWorkbench.team.closeWait', 'Wait for running members before closing the Team')
          : undefined}
        onClick={() => void close()}
      >
        {closing ? t('sessionWorkbench.team.closing', 'Closing…') : t('sessionWorkbench.team.close', 'Close team')}
      </button>
      {(error || projectedError) && (
        <span className="session-agent-team-control-error" role="alert">{error || projectedError}</span>
      )}
    </span>
  );
}
