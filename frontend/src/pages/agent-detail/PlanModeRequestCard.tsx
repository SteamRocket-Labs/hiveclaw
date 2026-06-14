/**
 * Plan-Mode-Request card — the user-facing approval surface (CC EnterPlanMode).
 *
 * Rendered inline in chat by `StructuredToolResultBody` whenever the
 * `request_plan_mode` tool returns `status: "plan_mode_entry_requested"`. The
 * agent has ended its turn and is waiting for the user to decide whether to enter
 * Plan Mode. The user is the gate: nothing flips into Plan Mode until they approve.
 *
 * Approve → the chat sends a message carrying `plan_mode_requested=true`, which
 * drives the existing entry path (`_maybe_handle_plan_mode_entry` →
 * `classify_plan_mode_entry` → `_activate_interactive_plan_mode`) — Plan Mode
 * starts and the agent drafts a confirmable plan next turn.
 * Decline → the chat sends a normal message so the agent continues without Plan
 * Mode.
 */

import React from 'react';
import { useTranslation } from 'react-i18next';

interface PlanModeRequestCardProps {
  /** The agent name, shown in the title ("{agent} wants to enter Plan Mode…"). */
  agentName?: string | null;
  /** Why the agent thinks planning this task first helps. */
  reason: string;
  /** Fired when the user approves entering Plan Mode. */
  onApprove: () => void | Promise<unknown>;
  /** Fired when the user declines — the agent continues without Plan Mode. */
  onDecline: () => void | Promise<unknown>;
  /** Compact spacing for inline chat rendering. */
  dense?: boolean;
  /** Marks the card as already decided (post-submit, disabled state). */
  submitted?: boolean;
}

/**
 * Pure approve/decline router with a single-decision guard. The component wires
 * its buttons to these handlers; keeping the routing pure lets it be unit-tested
 * without a DOM (the codebase has no jsdom/testing-library), mirroring
 * AskUserQuestionCard's pure interaction helpers.
 */
export function makeDecisionHandlers(params: {
  onApprove: () => void | Promise<unknown>;
  onDecline: () => void | Promise<unknown>;
  decided: boolean;
  setDecided: (value: boolean) => void;
}): { approve: () => void; decline: () => void } {
  const guard = (action: () => void | Promise<unknown>) => () => {
    if (params.decided) return;
    params.setDecided(true);
    void action();
  };
  return {
    approve: guard(params.onApprove),
    decline: guard(params.onDecline),
  };
}

export default function PlanModeRequestCard({
  agentName,
  reason,
  onApprove,
  onDecline,
  dense = false,
  submitted = false,
}: PlanModeRequestCardProps) {
  const { t } = useTranslation();
  const [decided, setDecided] = React.useState(submitted);

  const { approve, decline } = makeDecisionHandlers({ onApprove, onDecline, decided, setDecided });

  const agentLabel = (agentName || '').trim() || t('agent.plan.request.agentFallback', 'The agent');

  return (
    <div
      data-testid="plan-mode-request-card"
      style={{
        border: '1px solid var(--border-subtle)',
        borderRadius: '10px',
        padding: dense ? '12px 14px' : '16px 18px',
        background: 'var(--bg-primary)',
        display: 'grid',
        gap: dense ? '10px' : '12px',
      }}
    >
      <div style={{ display: 'flex', alignItems: 'center', gap: '8px', flexWrap: 'wrap' }}>
        <span
          style={{
            fontSize: '10px',
            fontWeight: 700,
            padding: '2px 8px',
            borderRadius: '999px',
            background: 'var(--bg-secondary)',
            border: '1px solid var(--border-subtle)',
            color: 'var(--accent-text, var(--accent-primary))',
            textTransform: 'uppercase',
            letterSpacing: '0.4px',
          }}
        >
          {t('agent.plan.request.badge', 'Plan Mode request')}
        </span>
      </div>

      <div style={{ fontSize: dense ? '13px' : '14px', fontWeight: 600, color: 'var(--text-primary)', lineHeight: 1.5 }}>
        {t('agent.plan.request.title', '{{agent}} wants to enter Plan Mode to plan this first:', { agent: agentLabel })}
      </div>

      <div style={{ fontSize: '13px', color: 'var(--text-secondary)', lineHeight: 1.6, whiteSpace: 'pre-wrap' }}>
        {reason}
      </div>

      {decided ? (
        <div
          role="status"
          style={{
            fontSize: '12px',
            color: 'var(--text-secondary)',
            background: 'var(--bg-secondary)',
            border: '1px solid var(--border-subtle)',
            borderRadius: '6px',
            padding: '8px 10px',
          }}
        >
          {t('agent.plan.request.sent', 'Your decision was sent.')}
        </div>
      ) : (
        <div style={{ display: 'flex', justifyContent: 'flex-end', gap: '8px', flexWrap: 'wrap' }}>
          <button
            type="button"
            className="btn btn-ghost"
            style={{ fontSize: '12px', padding: '6px 14px' }}
            disabled={decided}
            onClick={decline}
          >
            {t('agent.plan.request.decline', 'Not needed')}
          </button>
          <button
            type="button"
            className="btn btn-primary"
            style={{ fontSize: '12px', padding: '6px 14px' }}
            disabled={decided}
            onClick={approve}
          >
            {t('agent.plan.request.approve', 'Approve and enter Plan Mode')}
          </button>
        </div>
      )}
    </div>
  );
}
