/**
 * Plan Mode card (§12.1) — the user-facing confirmation surface for a plan.
 *
 * CC-align (docs/plan-mode-cc-alignment.md §4.1/§4.5):
 *   - The plan body is the agent-authored `plan_json.plan_markdown` article when
 *     present (rendered as markdown); the structured governance fields (steps,
 *     wake policy, cost, side effects, …) fold into a collapsed "Plan details"
 *     section instead of being flattened as the primary surface (surface ≠
 *     plumbing). Machine-intercepted plans with no article keep the structured
 *     render.
 *   - The card reflects REAL plan state: empty plumbing (none/unknown/empty) is
 *     not shown; a confirmed plan shows its handoff state (preparing / queued /
 *     executing / failed) and never offers the confirm button again.
 *
 * Reusable across both surfaces:
 *   - chat inline (mounted by `StructuredToolResultBody` via `InlinePlanCard`,
 *     which fetches the real plan by id and refetches), and
 *   - the Aware/Autonomy plan queue (`PlanQueueSection`).
 *
 * Confirm binds to `plan_version` + `plan_hash` (§8.2): the user confirms the
 * exact immutable version shown, never a mutable chat blob. Actions are only
 * offered while the plan is `awaiting_confirmation`.
 */

import React from 'react';
import { useTranslation } from 'react-i18next';

import { ApiError } from '../../api/core';
import { planApi, type PlanRequest, type PlanRiskLevel } from '../../api/domains/plans';
import MarkdownRenderer from '../../components/MarkdownRenderer';
import './PlanCard.css';

interface PlanCardProps {
  agentId: string;
  plan: PlanRequest;
  /** Fired after a confirm/revise/reject so the parent can refetch the queue. */
  onChanged?: () => void | Promise<unknown>;
  /** Compact spacing for inline chat rendering. */
  dense?: boolean;
}

const RISK_COLORS: Record<PlanRiskLevel, string> = {
  low: 'var(--success)',
  medium: 'var(--warning)',
  high: 'var(--error)',
};

function riskColor(level?: string | null): string {
  if (level && (level === 'low' || level === 'medium' || level === 'high')) {
    return RISK_COLORS[level];
  }
  return 'var(--text-tertiary)';
}

/** True for plumbing placeholders that must not be shown as plan content
 * (CC-align §4.5: no `none` / `unknown` / empty). */
function isPlumbing(value: unknown): boolean {
  if (value == null) return true;
  if (typeof value === 'string') {
    const v = value.trim().toLowerCase();
    return v === '' || v === 'none' || v === 'unknown' || v === 'n/a';
  }
  return false;
}

function humanizeWakePolicy(wake: PlanRequest['plan_json']['wake_policy']): string | null {
  if (!wake) return null;
  if (wake.type && isPlumbing(wake.type) && !wake.expr && wake.minutes == null && !wake.at) return null;
  if (wake.expr) {
    return `${wake.type || 'cron'} · ${wake.expr}${wake.timezone ? ` (${wake.timezone})` : ''}`;
  }
  if (typeof wake.minutes === 'number') {
    return wake.minutes >= 60 ? `interval · every ${wake.minutes / 60}h` : `interval · every ${wake.minutes} min`;
  }
  if (wake.at) {
    return `once · ${wake.at}`;
  }
  return isPlumbing(wake.type) ? null : wake.type || null;
}

function humanizeCost(cost: PlanRequest['plan_json']['estimated_cost']): string | null {
  if (!cost) return null;
  const parts = [cost.tokens_per_run, cost.expected_duration].filter((p) => !isPlumbing(p));
  return parts.length ? parts.join(' · ') : null;
}

function planningErrorMessages(metadata: PlanRequest['metadata']): string[] {
  const value = metadata?.planning_errors;
  if (Array.isArray(value)) {
    return value.map((item) => String(item).trim()).filter(Boolean);
  }
  if (typeof value === 'string' && value.trim()) {
    return [value.trim()];
  }
  return [];
}

interface DisplaySideEffect {
  label: string;
  requiresConfirmation?: boolean;
}

const GENERIC_SIDE_EFFECT_LABELS = new Set(['external action', 'external_action', 'external side effect', '外部动作', '外部副作用']);

function stringValue(value: unknown): string {
  return typeof value === 'string' ? value.trim() : '';
}

function displayableSideEffects(value: unknown): DisplaySideEffect[] {
  if (!Array.isArray(value)) return [];
  return value.flatMap((effect) => {
    if (typeof effect === 'string') {
      const label = effect.trim();
      if (GENERIC_SIDE_EFFECT_LABELS.has(label.toLowerCase())) return [];
      return label ? [{ label }] : [];
    }
    if (!effect || typeof effect !== 'object' || Array.isArray(effect)) return [];
    const record = effect as Record<string, unknown>;
    const primary = [record.kind, record.channel, record.audience].map(stringValue).filter(Boolean).join(' · ');
    const fallback = [record.description, record.summary, record.action].map(stringValue).find(Boolean) || '';
    const label = primary || fallback;
    if (GENERIC_SIDE_EFFECT_LABELS.has(label.toLowerCase())) return [];
    if (!label) return [];
    return [{ label, requiresConfirmation: Boolean(record.requires_confirmation) }];
  });
}

type PlanConfirmationApi = Pick<typeof planApi, 'confirmAndHandoff'>;

export async function confirmAndHandoffPlan(
  agentId: string,
  plan: PlanRequest,
  api: PlanConfirmationApi = planApi,
): Promise<void> {
  if (plan.plan_hash == null) {
    throw new Error('missing_plan_hash');
  }
  await api.confirmAndHandoff(agentId, plan.id, {
    plan_version: plan.plan_version,
    plan_hash: plan.plan_hash,
  });
}

export default function PlanCard({ agentId, plan, onChanged, dense = false }: PlanCardProps) {
  const { t } = useTranslation();
  const [busy, setBusy] = React.useState<null | 'confirm' | 'revise' | 'clarify' | 'regenerate' | 'reject' | 'handoff'>(null);
  const [error, setError] = React.useState<string | null>(null);
  const [revisionRequest, setRevisionRequest] = React.useState('');
  const [clarificationAnswers, setClarificationAnswers] = React.useState('');
  const [rejectReason, setRejectReason] = React.useState('');

  const planJson = plan.plan_json || {};
  const isAwaiting = plan.status === 'awaiting_confirmation';
  const isPlanning = plan.status === 'planning';
  const isPlanningFailed = plan.status === 'planning_failed';
  const isConfirmed = plan.status === 'confirmed';
  const authoredBody = typeof planJson.plan_markdown === 'string' ? planJson.plan_markdown.trim() : '';
  const hasAuthoredBody = authoredBody.length > 0;
  const steps = Array.isArray(planJson.steps) ? planJson.steps : [];
  const successCriteria = Array.isArray(planJson.success_criteria) ? planJson.success_criteria : [];
  const sideEffects = displayableSideEffects(planJson.external_side_effects);
  const stopConditions = Array.isArray(planJson.stop_conditions) ? planJson.stop_conditions : [];
  const assumptions = Array.isArray(planJson.assumptions) ? planJson.assumptions : [];
  const openQuestions = Array.isArray(planJson.open_questions) ? planJson.open_questions : [];
  const wakeText = humanizeWakePolicy(planJson.wake_policy);
  const costText = humanizeCost(planJson.estimated_cost);
  const risk = planJson.risk_assessment;
  const planningErrors = planningErrorMessages(plan.metadata);
  const requiresClarification =
    (plan.status === 'needs_clarification' || isAwaiting) && openQuestions.length > 0;

  const runAction = async (
    kind: 'confirm' | 'revise' | 'clarify' | 'regenerate' | 'reject' | 'handoff',
    fn: () => Promise<unknown>,
  ) => {
    if (busy) return;
    setBusy(kind);
    setError(null);
    try {
      await fn();
      if (onChanged) await onChanged();
    } catch (err) {
      const message =
        err instanceof ApiError
          ? err.message
          : err instanceof Error
            ? err.message
            : t('agent.plan.actionFailed', 'Action failed');
      setError(message);
    } finally {
      setBusy(null);
    }
  };

  const onConfirm = () => {
    if (plan.plan_hash == null) {
      setError(t('agent.plan.missingHash', 'This plan has no hash yet and cannot be confirmed.'));
      return;
    }
    return runAction('confirm', () => confirmAndHandoffPlan(agentId, plan));
  };

  const onRequestChanges = (event?: React.FormEvent) => {
    event?.preventDefault();
    const reason = revisionRequest.trim();
    const payload = { fill: reason.trim() ? { revision_request: reason.trim() } : {} };
    return runAction('revise', () =>
      isPlanningFailed
        ? planApi.regenerate(agentId, plan.id, payload)
        : planApi.revise(agentId, plan.id, payload),
    );
  };

  const onAnswerQuestions = (event?: React.FormEvent) => {
    event?.preventDefault();
    const answers = clarificationAnswers.trim();
    if (!answers) {
      setError(t('agent.plan.clarificationAnswerRequired', 'Answer the open questions before continuing.'));
      return;
    }
    return runAction('clarify', () =>
      planApi.revise(agentId, plan.id, {
        fill: {
          revision_request: `Answer the open questions before confirming:\n${answers}`,
          clarification_answers: answers,
          answered_open_questions: openQuestions,
        },
      }),
    );
  };

  const onRegenerate = () => {
    return runAction('regenerate', () => planApi.regenerate(agentId, plan.id, {}));
  };

  const onReject = (event?: React.FormEvent) => {
    event?.preventDefault();
    const reason = rejectReason.trim();
    return runAction('reject', () =>
      planApi.reject(agentId, plan.id, { reason: reason || undefined }),
    );
  };

  const onRetryHandoff = () => runAction('handoff', () => planApi.handoff(agentId, plan.id));

  // Structured governance detail — the canonical execution contract. Primary
  // surface only when there is no agent-authored article; otherwise folded.
  const governanceDetail = (
    <div className={`plan-card-detail${dense ? ' plan-card--dense' : ''}`}>
      {(planJson.objective || planJson.motivation) && (
        <div>
          <div className="plan-card-label">{t('agent.plan.objective', 'Objective')}</div>
          {planJson.objective && (
            <div className="plan-card-objective">{planJson.objective}</div>
          )}
          {planJson.motivation && (
            <div className="plan-card-motivation">
              {planJson.motivation}
            </div>
          )}
        </div>
      )}

      {steps.length > 0 && (
        <div>
          <div className="plan-card-label">{t('agent.plan.steps', 'Steps')}</div>
          <ol className="plan-card-list">
            {steps.map((step, index) => (
              <li key={step.order ?? index} className="plan-card-step">
                {step.description}
                {step.expected_output && (
                  <span className="plan-card-step-output"> — {step.expected_output}</span>
                )}
              </li>
            ))}
          </ol>
        </div>
      )}

      {successCriteria.length > 0 && (
        <div>
          <div className="plan-card-label">{t('agent.plan.successCriteria', 'Success criteria')}</div>
          <ul className="plan-card-list">
            {successCriteria.map((item, index) => (
              <li key={index} className="plan-card-item">{item}</li>
            ))}
          </ul>
        </div>
      )}

      {(wakeText || costText) && (
        <div className="plan-card-meta-grid">
          {wakeText && (
            <div>
              <div className="plan-card-label">{t('agent.plan.wakePolicy', 'Wake policy')}</div>
              <div className="plan-card-mono-value">{wakeText}</div>
            </div>
          )}
          {costText && (
            <div>
              <div className="plan-card-label">{t('agent.plan.estimatedCost', 'Estimated cost')}</div>
              <div className="plan-card-value">{costText}</div>
            </div>
          )}
        </div>
      )}

      {sideEffects.length > 0 && (
        <div>
          <div className="plan-card-label">{t('agent.plan.sideEffects', 'External side effects')}</div>
          <ul className="plan-card-list">
            {sideEffects.map((effect, index) => (
              <li key={index} className="plan-card-item">
                {effect.label}
                {effect.requiresConfirmation && (
                  <span className="plan-card-warn-inline"> — {t('agent.plan.requiresConfirmation', 'requires confirmation')}</span>
                )}
              </li>
            ))}
          </ul>
        </div>
      )}

      {stopConditions.length > 0 && (
        <div>
          <div className="plan-card-label">{t('agent.plan.stopConditions', 'Stop conditions')}</div>
          <ul className="plan-card-list">
            {stopConditions.map((item, index) => (
              <li key={index} className="plan-card-item-muted">{item}</li>
            ))}
          </ul>
        </div>
      )}

      {assumptions.length > 0 && (
        <div>
          <div className="plan-card-label">{t('agent.plan.assumptions', 'Assumptions')}</div>
          <ul className="plan-card-list">
            {assumptions.map((item, index) => (
              <li key={index} className="plan-card-item-muted">{item}</li>
            ))}
          </ul>
        </div>
      )}

      {openQuestions.length > 0 && (
        <div>
          <div className="plan-card-label">{t('agent.plan.openQuestions', 'Open questions')}</div>
          <ul className="plan-card-list">
            {openQuestions.map((item, index) => (
              <li key={index} className="plan-card-item-muted">{item}</li>
            ))}
          </ul>
        </div>
      )}
    </div>
  );

  return (
    <div
      data-testid="plan-card"
      className={`plan-card${dense ? ' plan-card--dense' : ''}`}
    >
      {/* Header: title + status + risk */}
      <div className="plan-card-header">
        <div className="plan-card-header-main">
          <div className="plan-card-badge-row">
            <span className="plan-card-badge">
              {t('agent.plan.badge', 'Plan')}
            </span>
            <span className="plan-card-meta">
              {t('agent.plan.version', 'v{{version}}', { version: plan.plan_version })}
            </span>
            <span className="plan-card-meta">
              {t(`agent.plan.status.${plan.status}`, String(plan.status).replace(/_/g, ' '))}
            </span>
          </div>
          <div className="plan-card-title">
            {planJson.title || plan.original_request}
          </div>
        </div>
        {risk?.level && (
          <span
            className="plan-card-risk"
            style={{
              color: riskColor(risk.level),
              border: `1px solid ${riskColor(risk.level)}`,
            }}
            title={Array.isArray(risk.reasons) ? risk.reasons.join('; ') : undefined}
          >
            {t(`agent.plan.risk.${risk.level}`, String(risk.level))}
          </span>
        )}
      </div>

      {/* Body: agent-authored article (primary) with folded governance, or the
          structured render for machine-intercepted plans. */}
      {hasAuthoredBody ? (
        <>
          <div className="plan-card-body">
            <MarkdownRenderer content={authoredBody} />
          </div>
          <details>
            <summary className="plan-card-label plan-card-details-summary">
              {t('agent.plan.detailsSummary', 'Plan details')}
            </summary>
            <div className="plan-card-details-body">{governanceDetail}</div>
          </details>
        </>
      ) : (
        governanceDetail
      )}

      {/* Confirmed plans show their real handoff/execution state — never a stale
          confirm button (CC-align §4.5/§4.6). */}
      {isConfirmed && (
        <HandoffBanner
          plan={plan}
          dense={dense}
          retrying={busy === 'handoff'}
          onRetry={onRetryHandoff}
        />
      )}

      {requiresClarification && (
        <div
          data-testid="plan-clarification-required"
          role="status"
          className="plan-card-notice plan-card-notice--warning"
        >
          <div className="plan-card-notice-title">
            {t('agent.plan.clarificationRequired', 'Clarification required')}
          </div>
          <div>
            {t(
              'agent.plan.answerBeforeImplementing',
              'Answer the open questions before implementing.',
            )}
          </div>
        </div>
      )}

      {isPlanning && (
        <div role="status" className="plan-card-notice">
          <div className="plan-card-notice-title">
            {t('agent.plan.planningTitle', 'Planning in progress')}
          </div>
          <div>
            {t(
              'agent.plan.planningDescription',
              'The agent is drafting a confirmable plan. Actions will appear when the plan is ready.',
            )}
          </div>
        </div>
      )}

      {isPlanningFailed && (
        <div role="alert" className="plan-card-notice plan-card-notice--error">
          <div className="plan-card-notice-strong">{t('agent.plan.failureTitle', 'Planning failed')}</div>
          {planningErrors.length > 0 ? (
            <ul className="plan-card-error-list">
              {planningErrors.map((item, index) => (
                <li key={index}>{item}</li>
              ))}
            </ul>
          ) : (
            <div>{t('agent.plan.failureUnknown', 'The planner did not return a valid confirmable plan.')}</div>
          )}
        </div>
      )}

      {error && (
        <div role="alert" className="plan-card-notice plan-card-notice--error">
          {error}
        </div>
      )}

      {/* Actions stay in the session card: no browser prompts, no context switch. */}
      {requiresClarification ? (
        <div className="plan-card-actions">
          <div className="plan-inline-actions">
            <PlanDecisionComposer
              testId="plan-clarification-composer"
              title={t('agent.plan.answerQuestions', 'Answer questions')}
              label={t('agent.plan.answerPrompt', 'Answer the open questions')}
              value={clarificationAnswers}
              disabled={busy !== null}
              submitLabel={busy === 'clarify' ? t('common.loading', 'Loading...') : t('agent.plan.sendAnswers', 'Send answers')}
              onChange={setClarificationAnswers}
              onSubmit={onAnswerQuestions}
            />
            <PlanDecisionComposer
              testId="plan-reject-composer"
              title={t('agent.plan.ignoreExit', 'Ignore / exit plan')}
              label={t('agent.plan.exitPrompt', 'Reason for leaving Plan Mode (optional)')}
              value={rejectReason}
              disabled={busy !== null}
              danger
              submitLabel={busy === 'reject' ? t('common.loading', 'Loading...') : t('agent.plan.submitExit', 'Exit Plan Mode')}
              onChange={setRejectReason}
              onSubmit={onReject}
            />
          </div>
          <button
            data-testid="plan-implement-disabled"
            type="button"
            className="btn btn-primary plan-card-actions-submit"
            disabled
            title={t('agent.plan.answerBeforeImplementing', 'Answer the open questions before implementing.')}
          >
            {t('agent.plan.implementPlan', 'Implement this plan')}
          </button>
        </div>
      ) : isAwaiting ? (
        <div className="plan-card-actions">
          <div className="plan-inline-actions">
            <PlanDecisionComposer
              testId="plan-revision-composer"
              title={t('agent.plan.adjustPlan', 'Adjust plan')}
              label={t('agent.plan.adjustPrompt', 'Tell the agent what to adjust')}
              value={revisionRequest}
              disabled={busy !== null}
              submitLabel={busy === 'revise' ? t('common.loading', 'Loading...') : t('agent.plan.sendAdjustment', 'Send adjustment')}
              onChange={setRevisionRequest}
              onSubmit={onRequestChanges}
            />
            <PlanDecisionComposer
              testId="plan-reject-composer"
              title={t('agent.plan.ignoreExit', 'Ignore / exit plan')}
              label={t('agent.plan.exitPrompt', 'Reason for leaving Plan Mode (optional)')}
              value={rejectReason}
              disabled={busy !== null}
              danger
              submitLabel={busy === 'reject' ? t('common.loading', 'Loading...') : t('agent.plan.submitExit', 'Exit Plan Mode')}
              onChange={setRejectReason}
              onSubmit={onReject}
            />
          </div>
          <button
            type="button"
            className="btn btn-primary plan-card-actions-submit"
            disabled={busy !== null}
            onClick={onConfirm}
          >
            {busy === 'confirm' ? t('common.loading', 'Loading...') : t('agent.plan.implementPlan', 'Implement this plan')}
          </button>
        </div>
      ) : isPlanningFailed ? (
        <div className="plan-card-actions">
          <div className="plan-inline-actions">
            <PlanDecisionComposer
              testId="plan-revision-composer"
              title={t('agent.plan.adjustAndRetry', 'Adjust and retry')}
              label={t('agent.plan.adjustPrompt', 'Tell the agent what to adjust')}
              value={revisionRequest}
              disabled={busy !== null}
              submitLabel={busy === 'revise' ? t('common.loading', 'Loading...') : t('agent.plan.sendAdjustment', 'Send adjustment')}
              onChange={setRevisionRequest}
              onSubmit={onRequestChanges}
            />
            <PlanDecisionComposer
              testId="plan-reject-composer"
              title={t('agent.plan.ignoreExit', 'Ignore / exit plan')}
              label={t('agent.plan.exitPrompt', 'Reason for leaving Plan Mode (optional)')}
              value={rejectReason}
              disabled={busy !== null}
              danger
              submitLabel={busy === 'reject' ? t('common.loading', 'Loading...') : t('agent.plan.submitExit', 'Exit Plan Mode')}
              onChange={setRejectReason}
              onSubmit={onReject}
            />
          </div>
          <button
            type="button"
            className="btn btn-primary plan-card-actions-submit"
            disabled={busy !== null}
            onClick={onRegenerate}
          >
            {busy === 'regenerate' ? t('common.loading', 'Loading...') : t('agent.plan.retryGeneration', 'Retry plan generation')}
          </button>
        </div>
      ) : isPlanning || isConfirmed ? null : (
        <div className="plan-card-terminal">
          {t(`agent.plan.terminal.${plan.status}`, t('agent.plan.noActions', 'No actions available for this plan.'))}
        </div>
      )}
    </div>
  );
}

interface PlanDecisionComposerProps {
  testId: string;
  title: string;
  label: string;
  value: string;
  submitLabel: string;
  disabled: boolean;
  danger?: boolean;
  onChange: (value: string) => void;
  onSubmit: (event: React.FormEvent) => void;
}

function PlanDecisionComposer({
  testId,
  title,
  label,
  value,
  submitLabel,
  disabled,
  danger = false,
  onChange,
  onSubmit,
}: PlanDecisionComposerProps) {
  return (
    <details data-testid={testId} className="plan-inline-composer">
      <summary className={danger ? 'danger' : undefined}>{title}</summary>
      <form onSubmit={onSubmit}>
        <label>
          <span>{label}</span>
          <textarea
            value={value}
            onChange={(event) => onChange(event.currentTarget.value)}
            disabled={disabled}
            rows={3}
            placeholder={label}
          />
        </label>
        <button type="submit" className={danger ? 'btn btn-danger' : 'btn btn-secondary'} disabled={disabled}>
          {submitLabel}
        </button>
      </form>
    </details>
  );
}

/** Real execution state for a confirmed plan (CC-align §4.5/§4.6). */
function HandoffBanner({
  plan,
  dense,
  retrying,
  onRetry,
}: {
  plan: PlanRequest;
  dense: boolean;
  retrying: boolean;
  onRetry: () => void;
}) {
  const { t } = useTranslation();
  const status = plan.handoff_status;
  const payload = (plan.handoff_payload || {}) as Record<string, unknown>;
  const reason = typeof payload.error === 'string' ? payload.error : typeof payload.reason === 'string' ? payload.reason : null;

  const isError = status === 'skipped' || status === 'failed';
  const tone = isError
    ? { color: 'var(--error)', bg: 'var(--error-subtle)' }
    : status === 'completed'
      ? { color: 'var(--success)', bg: 'var(--bg-secondary)' }
      : { color: 'var(--text-secondary)', bg: 'var(--bg-secondary)' };

  let headline: string;
  if (status === 'completed') headline = t('agent.plan.handoff.completed', 'Started — executing in this conversation');
  else if (status === 'queued') headline = t('agent.plan.handoff.queued', 'Confirmed — waiting for the current run to finish');
  else if (status === 'skipped') headline = t('agent.plan.handoff.skipped', 'Confirmed, but execution did not start');
  else if (status === 'failed') headline = t('agent.plan.handoff.failed', 'Confirmed, but execution failed to start');
  else headline = t('agent.plan.handoff.preparing', 'Confirmed — preparing to start');

  return (
    <div
      role={isError ? 'alert' : 'status'}
      className={`plan-card-notice${dense ? ' plan-card-notice--dense' : ''}`}
      style={{ color: tone.color, background: tone.bg }}
    >
      <div className="plan-card-notice-strong">{headline}</div>
      {isError && reason && <div className="plan-card-notice-reason">{reason}</div>}
      {isError && (
        <button
          type="button"
          className="btn btn-secondary plan-card-handoff-retry"
          disabled={retrying}
          onClick={onRetry}
        >
          {retrying
            ? t('common.loading', 'Loading...')
            : t('agent.plan.handoff.retry', 'Retry execution')}
        </button>
      )}
    </div>
  );
}
