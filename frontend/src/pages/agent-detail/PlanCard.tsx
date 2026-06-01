/**
 * Plan Mode card (§12.1) — the user-facing confirmation surface for a plan.
 *
 * Renders the structured `plan_json` (objective / steps / success criteria /
 * wake policy / external side effects / estimated cost / risk level) and the
 * three workflow actions: Confirm / Request changes / Reject.
 *
 * Reusable across both surfaces:
 *   - chat inline (mounted by `StructuredToolResultBody` on a `needs_plan` tool
 *     result), and
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

interface PlanCardProps {
  agentId: string;
  plan: PlanRequest;
  /** Fired after a confirm/revise/reject so the parent can refetch the queue. */
  onChanged?: () => void | Promise<unknown>;
  /** Compact spacing for inline chat rendering. */
  dense?: boolean;
}

const RISK_COLORS: Record<PlanRiskLevel, string> = {
  low: 'var(--success, #059669)',
  medium: 'var(--warning, #b45309)',
  high: 'var(--error, #dc2626)',
};

function riskColor(level?: string | null): string {
  if (level && (level === 'low' || level === 'medium' || level === 'high')) {
    return RISK_COLORS[level];
  }
  return 'var(--text-tertiary)';
}

function humanizeWakePolicy(wake: PlanRequest['plan_json']['wake_policy']): string | null {
  if (!wake) return null;
  if (wake.expr) {
    return `${wake.type || 'cron'} · ${wake.expr}${wake.timezone ? ` (${wake.timezone})` : ''}`;
  }
  if (typeof wake.minutes === 'number') {
    return wake.minutes >= 60 ? `interval · every ${wake.minutes / 60}h` : `interval · every ${wake.minutes} min`;
  }
  if (wake.at) {
    return `once · ${wake.at}`;
  }
  return wake.type || null;
}

function plannerWorkLedgerPath(metadata: PlanRequest['metadata']): string | null {
  const value = metadata?.planner_work_ledger;
  if (!value || typeof value !== 'object' || Array.isArray(value)) return null;
  const path = (value as Record<string, unknown>).path;
  return typeof path === 'string' && path.trim() ? path : null;
}

type PlanConfirmationApi = Pick<typeof planApi, 'confirm' | 'handoff'>;

export async function confirmAndHandoffPlan(
  agentId: string,
  plan: PlanRequest,
  api: PlanConfirmationApi = planApi,
): Promise<void> {
  if (plan.plan_hash == null) {
    throw new Error('missing_plan_hash');
  }
  await api.confirm(agentId, plan.id, {
    plan_version: plan.plan_version,
    plan_hash: plan.plan_hash,
  });
  await api.handoff(agentId, plan.id);
}

export default function PlanCard({ agentId, plan, onChanged, dense = false }: PlanCardProps) {
  const { t } = useTranslation();
  const [busy, setBusy] = React.useState<null | 'confirm' | 'revise' | 'reject'>(null);
  const [error, setError] = React.useState<string | null>(null);

  const planJson = plan.plan_json || {};
  const isAwaiting = plan.status === 'awaiting_confirmation';
  const steps = Array.isArray(planJson.steps) ? planJson.steps : [];
  const successCriteria = Array.isArray(planJson.success_criteria) ? planJson.success_criteria : [];
  const capabilities = Array.isArray(planJson.required_capabilities) ? planJson.required_capabilities : [];
  const sideEffects = Array.isArray(planJson.external_side_effects) ? planJson.external_side_effects : [];
  const stopConditions = Array.isArray(planJson.stop_conditions) ? planJson.stop_conditions : [];
  const wakeText = humanizeWakePolicy(planJson.wake_policy);
  const risk = planJson.risk_assessment;
  const cost = planJson.estimated_cost;
  const workLedgerPath = plannerWorkLedgerPath(plan.metadata);

  const runAction = async (
    kind: 'confirm' | 'revise' | 'reject',
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

  const onRequestChanges = () => {
    const reason = window.prompt(t('agent.plan.requestChangesPrompt', 'What should change about this plan?'));
    if (reason == null) return;
    return runAction('revise', () =>
      planApi.revise(agentId, plan.id, { fill: reason.trim() ? { revision_request: reason.trim() } : {} }),
    );
  };

  const onReject = () => {
    const reason = window.prompt(t('agent.plan.rejectPrompt', 'Reason for rejecting this plan (optional)'));
    if (reason === null) return;
    return runAction('reject', () =>
      planApi.reject(agentId, plan.id, { reason: reason.trim() || undefined }),
    );
  };

  const labelStyle: React.CSSProperties = {
    fontSize: '11px',
    fontWeight: 700,
    color: 'var(--text-tertiary)',
    textTransform: 'uppercase',
    letterSpacing: '0.4px',
    marginBottom: '4px',
  };

  return (
    <div
      data-testid="plan-card"
      style={{
        border: '1px solid var(--border-subtle)',
        borderRadius: '10px',
        padding: dense ? '12px 14px' : '16px 18px',
        background: 'var(--bg-primary)',
        display: 'grid',
        gap: dense ? '10px' : '12px',
      }}
    >
      {/* Header: title + status + risk */}
      <div style={{ display: 'flex', justifyContent: 'space-between', gap: '12px', alignItems: 'flex-start' }}>
        <div style={{ minWidth: 0 }}>
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
              {t('agent.plan.badge', 'Plan')}
            </span>
            <span style={{ fontSize: '11px', color: 'var(--text-tertiary)' }}>
              {t('agent.plan.version', 'v{{version}}', { version: plan.plan_version })}
            </span>
            <span style={{ fontSize: '11px', color: 'var(--text-tertiary)' }}>
              {t(`agent.plan.status.${plan.status}`, String(plan.status).replace(/_/g, ' '))}
            </span>
          </div>
          <div style={{ fontSize: dense ? '14px' : '15px', fontWeight: 600, color: 'var(--text-primary)', marginTop: '6px' }}>
            {planJson.title || plan.original_request}
          </div>
        </div>
        {risk?.level && (
          <span
            style={{
              fontSize: '11px',
              fontWeight: 600,
              color: riskColor(risk.level),
              border: `1px solid ${riskColor(risk.level)}`,
              borderRadius: '6px',
              padding: '2px 8px',
              whiteSpace: 'nowrap',
            }}
            title={Array.isArray(risk.reasons) ? risk.reasons.join('; ') : undefined}
          >
            {t(`agent.plan.risk.${risk.level}`, String(risk.level))}
          </span>
        )}
      </div>

      {/* Objective + motivation */}
      {(planJson.objective || planJson.motivation) && (
        <div>
          <div style={labelStyle}>{t('agent.plan.objective', 'Objective')}</div>
          {planJson.objective && (
            <div style={{ fontSize: '13px', color: 'var(--text-primary)', lineHeight: 1.5 }}>{planJson.objective}</div>
          )}
          {planJson.motivation && (
            <div style={{ fontSize: '12px', color: 'var(--text-tertiary)', marginTop: '3px', lineHeight: 1.5 }}>
              {planJson.motivation}
            </div>
          )}
        </div>
      )}

      {/* Steps */}
      {steps.length > 0 && (
        <div>
          <div style={labelStyle}>{t('agent.plan.steps', 'Steps')}</div>
          <ol style={{ margin: 0, paddingLeft: '18px', display: 'grid', gap: '4px' }}>
            {steps.map((step, index) => (
              <li key={step.order ?? index} style={{ fontSize: '13px', color: 'var(--text-secondary)', lineHeight: 1.5 }}>
                {step.description}
                {step.expected_output && (
                  <span style={{ color: 'var(--text-tertiary)' }}> — {step.expected_output}</span>
                )}
              </li>
            ))}
          </ol>
        </div>
      )}

      {/* Success criteria */}
      {successCriteria.length > 0 && (
        <div>
          <div style={labelStyle}>{t('agent.plan.successCriteria', 'Success criteria')}</div>
          <ul style={{ margin: 0, paddingLeft: '18px', display: 'grid', gap: '4px' }}>
            {successCriteria.map((item, index) => (
              <li key={index} style={{ fontSize: '12px', color: 'var(--text-secondary)', lineHeight: 1.5 }}>{item}</li>
            ))}
          </ul>
        </div>
      )}

      {/* Wake policy + capabilities + cost in a meta row */}
      <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fit, minmax(160px, 1fr))', gap: '10px' }}>
        {wakeText && (
          <div>
            <div style={labelStyle}>{t('agent.plan.wakePolicy', 'Wake policy')}</div>
            <div style={{ fontSize: '12px', color: 'var(--text-secondary)', fontFamily: 'var(--font-mono, monospace)' }}>{wakeText}</div>
          </div>
        )}
        {capabilities.length > 0 && (
          <div>
            <div style={labelStyle}>{t('agent.plan.capabilities', 'Capabilities')}</div>
            <div style={{ display: 'flex', flexWrap: 'wrap', gap: '4px' }}>
              {capabilities.map((cap) => (
                <span
                  key={cap}
                  style={{
                    fontSize: '11px',
                    padding: '1px 7px',
                    borderRadius: '6px',
                    background: 'var(--bg-secondary)',
                    border: '1px solid var(--border-subtle)',
                    color: 'var(--text-secondary)',
                    fontFamily: 'var(--font-mono, monospace)',
                  }}
                >
                  {cap}
                </span>
              ))}
            </div>
          </div>
        )}
        {cost && (cost.tokens_per_run || cost.expected_duration) && (
          <div>
            <div style={labelStyle}>{t('agent.plan.estimatedCost', 'Estimated cost')}</div>
            <div style={{ fontSize: '12px', color: 'var(--text-secondary)' }}>
              {[cost.tokens_per_run, cost.expected_duration].filter(Boolean).join(' · ')}
            </div>
          </div>
        )}
      </div>

      {/* External side effects */}
      {sideEffects.length > 0 && (
        <div>
          <div style={labelStyle}>{t('agent.plan.sideEffects', 'External side effects')}</div>
          <ul style={{ margin: 0, paddingLeft: '18px', display: 'grid', gap: '4px' }}>
            {sideEffects.map((effect, index) => {
              const parts = [effect.kind, effect.channel, effect.audience].filter(Boolean).join(' · ');
              return (
                <li key={index} style={{ fontSize: '12px', color: 'var(--text-secondary)', lineHeight: 1.5 }}>
                  {parts || t('agent.plan.sideEffectGeneric', 'External action')}
                  {effect.requires_confirmation && (
                    <span style={{ color: 'var(--warning, #b45309)' }}> — {t('agent.plan.requiresConfirmation', 'requires confirmation')}</span>
                  )}
                </li>
              );
            })}
          </ul>
        </div>
      )}

      {/* Stop conditions */}
      {stopConditions.length > 0 && (
        <div>
          <div style={labelStyle}>{t('agent.plan.stopConditions', 'Stop conditions')}</div>
          <ul style={{ margin: 0, paddingLeft: '18px', display: 'grid', gap: '4px' }}>
            {stopConditions.map((item, index) => (
              <li key={index} style={{ fontSize: '12px', color: 'var(--text-tertiary)', lineHeight: 1.5 }}>{item}</li>
            ))}
          </ul>
        </div>
      )}

      {workLedgerPath && (
        <div>
          <div style={labelStyle}>{t('agent.plan.workLedger', 'Work ledger')}</div>
          <div
            style={{
              fontSize: '11px',
              color: 'var(--text-tertiary)',
              fontFamily: 'var(--font-mono, monospace)',
              overflowWrap: 'anywhere',
            }}
          >
            {workLedgerPath}
          </div>
        </div>
      )}

      {error && (
        <div
          role="alert"
          style={{
            fontSize: '12px',
            color: 'var(--error, #dc2626)',
            background: 'rgba(220, 100, 100, 0.1)',
            borderRadius: '6px',
            padding: '6px 10px',
          }}
        >
          {error}
        </div>
      )}

      {/* Actions — only while awaiting confirmation (§8) */}
      {isAwaiting ? (
        <div style={{ display: 'flex', gap: '8px', justifyContent: 'flex-end', flexWrap: 'wrap' }}>
          <button
            type="button"
            className="btn btn-ghost"
            style={{ fontSize: '12px', padding: '6px 12px', color: 'var(--error, #dc2626)' }}
            disabled={busy !== null}
            onClick={onReject}
          >
            {busy === 'reject' ? t('common.loading', 'Loading...') : t('agent.plan.reject', 'Reject')}
          </button>
          <button
            type="button"
            className="btn btn-ghost"
            style={{ fontSize: '12px', padding: '6px 12px' }}
            disabled={busy !== null}
            onClick={onRequestChanges}
          >
            {busy === 'revise' ? t('common.loading', 'Loading...') : t('agent.plan.requestChanges', 'Request changes')}
          </button>
          <button
            type="button"
            className="btn btn-primary"
            style={{ fontSize: '12px', padding: '6px 14px' }}
            disabled={busy !== null}
            onClick={onConfirm}
          >
            {busy === 'confirm' ? t('common.loading', 'Loading...') : t('agent.plan.confirmAndStart', 'Confirm and start')}
          </button>
        </div>
      ) : (
        <div style={{ fontSize: '11px', color: 'var(--text-tertiary)' }}>
          {plan.status === 'confirmed' && plan.handoff_status
            ? t('agent.plan.handoffState', 'Handoff: {{state}}', { state: String(plan.handoff_status).replace(/_/g, ' ') })
            : t(`agent.plan.terminal.${plan.status}`, t('agent.plan.noActions', 'No actions available for this plan.'))}
        </div>
      )}
    </div>
  );
}
