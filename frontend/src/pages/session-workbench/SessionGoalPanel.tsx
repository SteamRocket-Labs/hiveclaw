import React from 'react';
import { useTranslation } from 'react-i18next';
import { IconPlayerPause, IconPlayerPlay, IconTargetArrow, IconX } from '@tabler/icons-react';

import { ccParityApi, type SessionGoal } from '../../api/domains/ccParity';
import './SessionGoalPanel.css';

function compactDuration(seconds: number): string {
  const safe = Math.max(0, Math.floor(seconds));
  if (safe < 60) return '<1m';
  if (safe < 3600) return `${Math.floor(safe / 60)}m`;
  const hours = Math.floor(safe / 3600);
  const minutes = Math.floor((safe % 3600) / 60);
  return minutes > 0 ? `${hours}h ${minutes}m` : `${hours}h`;
}

export function goalProgressFacts(goal: Pick<
  SessionGoal,
  'remaining_tokens' | 'remaining_continuation_turns' | 'remaining_time_seconds'
>): string[] {
  return [
    typeof goal.remaining_tokens === 'number' ? `${goal.remaining_tokens.toLocaleString()} tokens left` : null,
    typeof goal.remaining_continuation_turns === 'number' ? `${goal.remaining_continuation_turns} turns left` : null,
    typeof goal.remaining_time_seconds === 'number' ? `${compactDuration(goal.remaining_time_seconds)} left` : null,
  ].filter((value): value is string => Boolean(value));
}

function goalStatusLabel(status: string): string {
  const labels: Record<string, string> = {
    active: 'Active',
    paused: 'Paused',
    blocked: 'Needs attention',
    usage_limited: 'Usage limit reached',
    budget_limited: 'Budget reached',
    complete: 'Complete',
    cancelled: 'Stopped',
  };
  return labels[status] || status;
}

export function SessionGoalPanel({
  agentId,
  sessionId,
  goals,
  onChanged,
  readOnly = false,
}: {
  agentId: string;
  sessionId: string;
  goals: SessionGoal[];
  onChanged?: (goal: SessionGoal) => void | Promise<unknown>;
  readOnly?: boolean;
}) {
  const { t } = useTranslation();
  const [localGoals, setLocalGoals] = React.useState(goals);
  const [pending, setPending] = React.useState<string | null>(null);
  const [error, setError] = React.useState('');

  React.useEffect(() => setLocalGoals(goals), [goals]);

  const nonTerminal = localGoals.filter((goal) => !['complete', 'cancelled'].includes(goal.status));
  const visibleGoals = (nonTerminal.length > 0 ? nonTerminal : localGoals.slice(0, 1)).slice(0, 3);
  if (visibleGoals.length === 0) return null;

  const transition = async (goal: SessionGoal, action: 'pause' | 'resume' | 'stop') => {
    const operationKey = `${goal.id}:${action}`;
    setPending(operationKey);
    setError('');
    try {
      const updated = await ccParityApi.transitionGoal(agentId, sessionId, goal.id, action);
      setLocalGoals((current) => current.map((item) => (item.id === updated.id ? updated : item)));
      await onChanged?.(updated);
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : t('sessionGoal.transitionFailed', 'Could not update this goal.'));
    } finally {
      setPending(null);
    }
  };

  return (
    <section className="session-goal-panel" aria-label={t('sessionGoal.title', 'Goal')}>
      <div className="session-goal-panel-heading">
        <IconTargetArrow size={15} aria-hidden="true" />
        <strong>{t('sessionGoal.title', 'Goal')}</strong>
      </div>
      {visibleGoals.map((goal) => {
        const facts = goalProgressFacts(goal);
        const controls = goal.controls || { can_pause: false, can_resume: false, can_stop: false };
        return (
          <article key={goal.id} className="session-goal-card" data-goal-status={goal.status}>
            <div className="session-goal-card-title">
              <span>{goal.objective}</span>
              <small>{t(`sessionGoal.status.${goal.status}`, goalStatusLabel(goal.status))}</small>
            </div>
            {facts.length > 0 && (
              <div className="session-goal-facts">
                {facts.map((fact) => <span key={fact}>{fact}</span>)}
              </div>
            )}
            {goal.blocked_reason && <p className="session-goal-reason">{goal.blocked_reason}</p>}
            {goal.completion_summary && <p className="session-goal-summary">{goal.completion_summary}</p>}
            {!readOnly && (controls.can_pause || controls.can_resume || controls.can_stop) && (
              <div className="session-goal-actions">
                {controls.can_pause && (
                  <button type="button" disabled={Boolean(pending)} onClick={() => transition(goal, 'pause')}>
                    <IconPlayerPause size={13} aria-hidden="true" />
                    {t('sessionGoal.pause', 'Pause')}
                  </button>
                )}
                {controls.can_resume && (
                  <button type="button" disabled={Boolean(pending)} onClick={() => transition(goal, 'resume')}>
                    <IconPlayerPlay size={13} aria-hidden="true" />
                    {t('sessionGoal.resume', 'Resume')}
                  </button>
                )}
                {controls.can_stop && (
                  <button type="button" disabled={Boolean(pending)} onClick={() => transition(goal, 'stop')}>
                    <IconX size={13} aria-hidden="true" />
                    {t('sessionGoal.stop', 'Stop')}
                  </button>
                )}
              </div>
            )}
          </article>
        );
      })}
      {error && <p className="session-goal-error" role="alert">{error}</p>}
    </section>
  );
}
