import { useMutation, useQuery } from '@tanstack/react-query';
import { useTranslation } from 'react-i18next';

import AskUserQuestionCard from './AskUserQuestionCard';
import PlanModeRequestCard from './PlanModeRequestCard';
import { HrBlueprintPreviewCard } from './HrBlueprintPreviewCard';
import PlanCard from './PlanCard';
import { planApi } from '../../api/domains/plans';
import { getWorkflowPreview, previewWorkflowCandidate, startWorkflow } from '../../api/domains/workflows';
import type { ToolCallMeta, WorkflowPreviewToolMeta } from './toolResultEnvelope';

type Translate = ReturnType<typeof useTranslation>['t'];

interface StructuredToolResultBodyProps {
  toolName?: string;
  toolMeta?: ToolCallMeta | null;
  toolResult?: string;
  toolRawResult?: string;
  agentId?: string;
  agentName?: string | null;
  submitted?: boolean;
  onSendMessage?: (text: string) => void | Promise<unknown>;
  onEnterPlanMode?: (reason: string) => void | Promise<unknown>;
}

export function InlinePlanCard({ agentId, planId }: { agentId: string; planId: string }) {
  const { t } = useTranslation();
  const { data: plan, isLoading, error, refetch } = useQuery({
    queryKey: ['agent-plan-inline', agentId, planId],
    queryFn: () => planApi.get(agentId, planId),
    enabled: !!agentId && !!planId,
    refetchInterval: 10000,
  });

  if (isLoading) {
    return (
      <div style={{ fontSize: '12px', color: 'var(--text-tertiary)' }}>
        {t('agent.plan.inlineLoading', 'Loading plan card...')}
      </div>
    );
  }

  if (error || !plan) {
    return (
      <div style={{ fontSize: '12px', color: 'var(--text-tertiary)' }}>
        {t('agent.plan.inlineLoadFailed', 'Plan card could not load. Open Aware > Plan Queue if it is still pending.')}
      </div>
    );
  }

  return <PlanCard agentId={agentId} plan={plan} onChanged={() => refetch()} dense />;
}

export function RawToolResultBlock({ text }: { text: string }) {
  return (
    <div
      style={{
        color: 'var(--text-secondary)',
        fontSize: '11px',
        fontFamily: 'var(--font-mono)',
        whiteSpace: 'pre-wrap',
        wordBreak: 'break-word',
        maxHeight: '240px',
        overflow: 'auto',
        background: 'rgba(0,0,0,0.15)',
        borderRadius: '4px',
        padding: '4px 6px',
      }}
    >
      {text}
    </div>
  );
}

export function StructuredToolSection({ label, items }: { label: string; items: string[] }) {
  if (items.length === 0) return null;
  return (
    <div>
      <div style={{ fontSize: '10px', fontWeight: 700, color: 'var(--text-tertiary)', marginBottom: '4px', textTransform: 'uppercase', letterSpacing: '0.04em' }}>
        {label}
      </div>
      <ul style={{ margin: 0, paddingLeft: '16px', color: 'var(--text-secondary)', lineHeight: 1.6 }}>
        {items.map((item) => (
          <li key={`${label}-${item}`}>{item}</li>
        ))}
      </ul>
    </div>
  );
}

export function WorkflowPreviewSummary({
  meta,
  status,
}: {
  meta: WorkflowPreviewToolMeta;
  status: WorkflowPreviewToolMeta['previewStatus'];
}) {
  const { t } = useTranslation();
  return (
    <div style={{ display: 'grid', gap: '8px' }}>
      <div style={{ display: 'flex', justifyContent: 'space-between', gap: '8px', alignItems: 'center' }}>
        <div style={{ fontSize: '12px', fontWeight: 700, color: 'var(--text-primary)' }}>
          {t('agent.chat.workflowPreview.title', 'Workflow ready to run')}
        </div>
        <span className={`badge ${status === 'failed' || status === 'expired' ? 'badge-error' : status === 'started' ? 'badge-success' : 'badge-neutral'}`}>
          {status === 'started'
            ? t('agent.chat.workflowPreview.started', 'Started')
            : status === 'starting'
              ? t('agent.chat.workflowPreview.starting', 'Starting')
              : status === 'failed'
                ? t('agent.chat.workflowPreview.failed', 'Retry available')
                : status === 'expired'
                  ? t('agent.chat.workflowPreview.expired', 'Expired')
                  : t('agent.chat.workflowPreview.ready', 'Ready')}
        </span>
      </div>
      <div style={{ fontSize: '11px', color: 'var(--text-secondary)' }}>
        {meta.plannedLeafCalls != null
          ? t('agent.chat.workflowPreview.workUnits', '{{count}} planned work units', { count: meta.plannedLeafCalls })
          : t('agent.chat.workflowPreview.review', 'Review the workflow before running it.')}
        {meta.budgetTokens != null
          ? ` · ${t('agent.chat.workflowPreview.tokenBudget', 'Token budget {{count}}', { count: meta.budgetTokens })}`
          : ''}
      </div>
      <StructuredToolSection
        label={t('agent.chat.workflowPreview.confirmationReasons', 'Why confirmation is needed')}
        items={meta.confirmationReasons}
      />
    </div>
  );
}

export function InteractiveWorkflowPreviewCard({ meta, agentId }: { meta: WorkflowPreviewToolMeta; agentId: string }) {
  const { t } = useTranslation();
  const statusQuery = useQuery({
    queryKey: ['workflow-preview', agentId, meta.previewId],
    queryFn: () => getWorkflowPreview(agentId, meta.previewId),
    refetchInterval: (query) => (query.state.data?.preview_status === 'starting' ? 1500 : false),
  });
  const startMutation = useMutation({
    mutationFn: () => startWorkflow(agentId, { previewId: meta.previewId }),
    onSuccess: () => statusQuery.refetch(),
  });
  const status = statusQuery.data?.preview_status ?? meta.previewStatus;
  const canStart = status === 'ready' || status === 'failed';

  return (
    <div style={{ display: 'grid', gap: '8px' }}>
      <WorkflowPreviewSummary meta={meta} status={status} />
      {canStart && (
        <button
          type="button"
          className="btn btn-primary"
          onClick={() => startMutation.mutate()}
          disabled={startMutation.isPending}
        >
          {startMutation.isPending
            ? t('agent.chat.workflowPreview.starting', 'Starting')
            : meta.confirmationRequired
              ? t('agent.chat.workflowPreview.confirmAndRun', 'Confirm and run')
              : t('agent.chat.workflowPreview.run', 'Run workflow')}
        </button>
      )}
      {startMutation.isError && (
        <div className="agent-workflows-error" role="alert">
          {startMutation.error instanceof Error
            ? startMutation.error.message
            : t('agent.chat.workflowPreview.startFailed', 'Workflow could not start. You can retry safely.')}
        </div>
      )}
    </div>
  );
}

export function WorkflowPreviewConfirmationCard({ meta, agentId }: { meta: WorkflowPreviewToolMeta; agentId?: string }) {
  if (!agentId) {
    return <WorkflowPreviewSummary meta={meta} status={meta.previewStatus} />;
  }
  return <InteractiveWorkflowPreviewCard meta={meta} agentId={agentId} />;
}

export function DynamicWorkflowProposalCard({
  meta,
  agentId,
}: {
  meta: Extract<ToolCallMeta, { kind: 'dynamic_workflow_proposal' }>;
  agentId?: string;
}) {
  const { t } = useTranslation();
  const previewMutation = useMutation({
    mutationFn: (candidateId: string) => (
      agentId
        ? previewWorkflowCandidate(agentId, meta.proposalId, candidateId)
        : Promise.reject(new Error('Agent context is unavailable'))
    ),
  });
  const selectedPreview = previewMutation.data;
  const selectedPreviewMeta: WorkflowPreviewToolMeta | null = selectedPreview
    ? {
        kind: 'workflow_preview',
        previewId: selectedPreview.preview_id,
        sessionId: selectedPreview.session_id || null,
        previewStatus: selectedPreview.preview_status,
        proposalId: selectedPreview.proposal_id || meta.proposalId,
        candidateId: selectedPreview.candidate_id || null,
        confirmationRequired: Boolean(selectedPreview.confirmation_required),
        confirmationReasons: selectedPreview.confirmation_reasons || [],
        plannedLeafCalls: selectedPreview.planned_leaf_calls ?? null,
        budgetTokens: selectedPreview.budget_tokens ?? null,
      }
    : null;

  return (
    <div style={{ display: 'grid', gap: '8px' }}>
      <div style={{ display: 'grid', gap: '4px' }}>
        <div style={{ fontSize: '11px', fontWeight: 700, color: 'var(--accent-text)' }}>
          {t('agent.chat.toolResults.dynamicWorkflowProposalTitle', 'Dynamic Workflow Proposal')}
        </div>
        {meta.goal && <div style={{ fontSize: '12px', fontWeight: 600, color: 'var(--text-primary)' }}>{meta.goal}</div>}
        {meta.whyWorkflow && <div style={{ fontSize: '12px', color: 'var(--text-secondary)' }}>{meta.whyWorkflow}</div>}
      </div>
      <StructuredToolSection
        label={t('agent.chat.toolResults.successCriteria', 'Success Criteria')}
        items={meta.successCriteria}
      />
      <div style={{ display: 'grid', gap: '6px' }}>
        <div style={{ fontSize: '11px', fontWeight: 700, color: 'var(--text-tertiary)' }}>
          {t('agent.chat.toolResults.candidates', 'Candidates')}
        </div>
        {meta.candidates.map((candidate, index) => {
          const recommended = candidate.candidateId === meta.recommendedCandidateId;
          const facts = [
            candidate.patternMix.length ? candidate.patternMix.join(', ') : '',
            candidate.riskLevel ? `${t('agent.chat.toolResults.risk', 'Risk')}: ${candidate.riskLevel}` : '',
            candidate.plannedLeafCalls != null
              ? `${t('agent.chat.toolResults.leafCalls', 'Leaf calls')}: ${candidate.plannedLeafCalls}`
              : '',
            candidate.budgetTokens != null
              ? `${t('agent.chat.toolResults.budgetTokens', 'Budget tokens')}: ${candidate.budgetTokens}`
              : '',
            candidate.confirmationRequired
              ? t('agent.chat.toolResults.confirmationRequired', 'Confirmation required')
              : '',
          ].filter(Boolean);
          return (
            <div
              key={candidate.candidateId}
              style={{
                display: 'grid',
                gap: '3px',
                padding: '8px',
                border: '1px solid var(--border-subtle)',
                borderRadius: '6px',
              }}
            >
              <div style={{ display: 'flex', gap: '6px', alignItems: 'center', flexWrap: 'wrap' }}>
                <span style={{ fontSize: '12px', fontWeight: 600, color: 'var(--text-primary)' }}>
                  {candidate.name || t('agent.chat.toolResults.candidateFallback', 'Candidate {{index}}', { index: index + 1 })}
                </span>
                {recommended && (
                  <span style={{ fontSize: '11px', color: 'var(--accent-text)' }}>
                    {t('agent.chat.toolResults.recommended', 'Recommended')}
                  </span>
                )}
              </div>
              {facts.length > 0 && <div style={{ fontSize: '11px', color: 'var(--text-tertiary)' }}>{facts.join(' · ')}</div>}
              {agentId && (
                <button
                  type="button"
                  className="btn btn-secondary"
                  onClick={() => previewMutation.mutate(candidate.candidateId)}
                  disabled={previewMutation.isPending}
                >
                  {previewMutation.isPending
                    ? t('agent.chat.workflowPreview.starting', 'Creating preview')
                    : t('agent.chat.toolResults.selectAndPreview', 'Select and preview')}
                </button>
              )}
            </div>
          );
        })}
      </div>
      {previewMutation.isError && (
        <div className="agent-workflows-error" role="alert">
          {previewMutation.error instanceof Error
            ? previewMutation.error.message
            : t('agent.chat.workflowPreview.previewFailed', 'Workflow preview could not be created. You can retry safely.')}
        </div>
      )}
      {selectedPreviewMeta ? <WorkflowPreviewConfirmationCard meta={selectedPreviewMeta} agentId={agentId} /> : null}
      {!selectedPreviewMeta && (
        <div style={{ fontSize: '12px', color: 'var(--text-secondary)' }}>
          {t('agent.chat.toolResults.dynamicWorkflowNotStarted', 'Select a candidate to create a reviewable preview. Nothing has run yet.')}
        </div>
      )}
    </div>
  );
}

export function StructuredToolResultBody({
  toolName,
  toolMeta,
  toolResult,
  toolRawResult,
  agentId,
  agentName,
  submitted = false,
  onSendMessage,
  onEnterPlanMode,
}: StructuredToolResultBodyProps) {
  const { t } = useTranslation();
  const rawText = typeof toolRawResult === 'string' && toolRawResult.trim() ? toolRawResult : '';

  if (!toolMeta) {
    return toolResult ? <RawToolResultBlock text={toolResult} /> : null;
  }

  if (toolMeta.kind === 'user_clarification') {
    if (!onSendMessage) {
      // No send path available (e.g. read-only history view) — fall back to a
      // static rendering with the questions but no interactive submit.
      return (
        <AskUserQuestionCard
          questions={toolMeta.questions}
          blocking={toolMeta.blocking}
          nextAction={toolMeta.nextAction}
          onSubmit={() => undefined}
          submitted
          dense
        />
      );
    }
    return (
      <AskUserQuestionCard
        questions={toolMeta.questions}
        blocking={toolMeta.blocking}
        nextAction={toolMeta.nextAction}
        onSubmit={(answerText) => onSendMessage(answerText)}
        submitted={submitted}
        dense
      />
    );
  }

  if (toolMeta.kind === 'plan_mode_request') {
    // CC EnterPlanMode parity: the agent requested Plan Mode; the user is the gate.
    // Approve → onEnterPlanMode sends the reason with plan_mode_requested=true so
    // the existing entry path activates Plan Mode. Decline → a normal message so
    // the agent continues without Plan Mode. With no send path (read-only history
    // view) render a static, already-decided card.
    if (!onEnterPlanMode || !onSendMessage) {
      return (
        <PlanModeRequestCard
          agentName={agentName}
          reason={toolMeta.reason}
          onApprove={() => undefined}
          onDecline={() => undefined}
          submitted
          dense
        />
      );
    }
    return (
      <PlanModeRequestCard
        agentName={agentName}
        reason={toolMeta.reason}
        onApprove={() => onEnterPlanMode(toolMeta.reason)}
        onDecline={() => onSendMessage(t('agent.plan.request.declineMessage', 'Continue without entering Plan Mode.'))}
        dense
      />
    );
  }

  if (toolMeta.kind === 'plan_proposal') {
    // CC-align §4.5: render the REAL plan by id (InlinePlanCard fetches it and
    // refetches), NOT a synthetic card hardcoded to awaiting_confirmation — the
    // synthetic one left a stale confirm button after the user confirmed, so a
    // re-click hit the backend's 409 "cannot confirm a confirmed plan".
    return (
      <div style={{ display: 'grid', gap: '8px' }}>
        {agentId && toolMeta.planId ? (
          <InlinePlanCard agentId={agentId} planId={toolMeta.planId} />
        ) : (
          <div style={{ fontSize: '12px', color: 'var(--text-secondary)' }}>
            {toolMeta.summary || t('agent.plan.needsConfirmation', 'A plan needs your confirmation.')}
          </div>
        )}
      </div>
    );
  }

  if (toolMeta.kind === 'workflow_preview') {
    return <WorkflowPreviewConfirmationCard meta={toolMeta} agentId={agentId} />;
  }

  if (toolMeta.kind === 'dynamic_workflow_proposal') {
    return <DynamicWorkflowProposalCard meta={toolMeta} agentId={agentId} />;
  }

  if (toolMeta.kind === 'hr_preview') {
    return (
      <HrBlueprintPreviewCard
        agentId={agentId}
        preview={toolMeta}
        onSendMessage={onSendMessage}
      />
    );
  }

  if (toolMeta.kind === 'runtime_step') {
    return toolResult ? <RawToolResultBlock text={toolResult} /> : null;
  }

  const showRawOutput = rawText.length > 0 && rawText !== toolMeta.message;
  return (
    <div style={{ display: 'grid', gap: '8px' }}>
      <div style={{ display: 'grid', gap: '4px' }}>
        <div style={{ fontSize: '11px', fontWeight: 700, color: 'var(--accent-text)' }}>
          {t('agent.chat.toolResults.createdTitle', 'Digital Employee Created')}
        </div>
        <div style={{ fontSize: '12px', color: 'var(--text-secondary)' }}>{toolMeta.message}</div>
        {toolMeta.agentName && (
          <div style={{ fontSize: '12px', color: 'var(--text-tertiary)' }}>
            <strong>{t('agent.chat.toolResults.agentName', 'Agent')}:</strong> {toolMeta.agentName}
          </div>
        )}
      </div>
      <StructuredToolSection label={t('agent.chat.toolResults.warnings', 'Warnings')} items={toolMeta.warnings} />
      <StructuredToolSection label={t('agent.chat.toolResults.manualSteps', 'Manual Steps')} items={toolMeta.manualSteps} />
      {showRawOutput && (
        <details>
          <summary style={{ cursor: 'pointer', color: 'var(--text-tertiary)' }}>
            {t('agent.chat.toolResults.rawOutput', 'Raw output')}
          </summary>
          <div style={{ marginTop: '6px' }}>
            <RawToolResultBlock text={rawText} />
          </div>
        </details>
      )}
      {!showRawOutput && toolName === 'create_digital_employee' && toolResult && toolResult !== toolMeta.message && (
        <RawToolResultBlock text={toolResult} />
      )}
    </div>
  );
}
