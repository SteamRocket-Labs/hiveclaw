/**
 * Workflow frontend adapter (§9 P12 of docs/workflow-source-capability.md).
 *
 * One mental model for the user — 自动化流程 — backed by two definition
 * sources on ONE engine (§3.1):
 *
 *   POST /agents/{agentId}/workflows/preview            compile+admission+confirmation notes, never runs
 *   POST /agents/{agentId}/workflows/runs               start only with the exact preview binding
 *   GET  /agents/{agentId}/workflows/runs               run history (asset view §4)
 *   GET  /agents/{agentId}/workflows/runs/{runId}       run + step journal
 *   POST /agents/{agentId}/workflows/runs/{runId}/cancel kill (resumable later)
 *   POST /agents/{agentId}/workflows/runs/{runId}/promote 固化 → registered DRAFT (provenance kept)
 *   GET  /agents/{agentId}/workflows/promote-suggestions repeated-run evidence
 *
 *   POST /workflow-definitions                          create draft (versioned, immutable)
 *   GET  /workflow-definitions                          list visible to the tenant/agent
 *   POST /workflow-definitions/{id}/activate|deprecate|revoke
 *   POST /workflow-definitions/{id}/approve-promotion   human approver only (§10 decision 4)
 *   POST /workflow-definitions/{id}/fork                registered + patch → ephemeral data
 */

import { get, post } from '../core';

// ---------------------------------------------------------------------------
// types
// ---------------------------------------------------------------------------

export type WorkflowRunStatus =
  | 'pending'
  | 'running'
  | 'completed'
  | 'replayed'
  | 'failed'
  | 'suspended'
  | 'killed';

export type WorkflowStepStatus =
  | 'pending'
  | 'running'
  | 'done'
  | 'failed'
  | 'skipped'
  | 'suspended'
  | 'unknown_requires_reconciliation';

export type WorkflowDefinitionStatus = 'draft' | 'active' | 'deprecated' | 'revoked';

export interface WorkflowPreview {
  preview_id: string;
  session_id: string;
  preview_status: 'ready' | 'starting' | 'started' | 'failed' | 'expired';
  artifact_version: number;
  artifact_hash: string;
  run_id?: string | null;
  attempt_count?: number;
  failure?: { code: string | null; message: string | null } | null;
  proposal_id?: string | null;
  candidate_id?: string | null;
  definition_hash: string;
  args_hash: string;
  confirmation_required: boolean;
  confirmation_reasons: string[];
  planned_leaf_calls: number;
  budget_tokens: number;
}

export interface WorkflowRunStep {
  step_id: string;
  step_type: string;
  status: WorkflowStepStatus;
  error: string | null;
}

export interface DynamicWorkflowRunMeta {
  proposal_id?: string | null;
  candidate_id?: string | null;
  preview_id?: string | null;
  definition_hash?: string | null;
  args_hash?: string | null;
  pattern_mix?: string[];
  success_criteria?: string[];
  failure_policy?: Record<string, unknown>;
  budget?: Record<string, unknown>;
  repair_attempts?: number;
  outcome_evidence?: WorkflowOutcomeEvidence;
  repair_plan?: WorkflowRepairPlan | null;
}

export interface WorkflowOutcomeEvidence {
  status?: string | null;
  steps_total?: number;
  steps_done?: number;
  steps_failed?: number;
  steps_suspended?: number;
  leaf_total?: number;
  leaf_done?: number;
  leaf_failed?: number;
  promotion_eligible?: boolean;
  success_criteria_count?: number;
  [key: string]: unknown;
}

export interface WorkflowRepairPlan {
  repairable: boolean;
  strategy?: string | null;
  repair_attempts?: number;
  repair_rounds?: number;
  failed_leaf_count?: number;
  failed_step_count?: number;
  failed_leaves?: Array<Record<string, unknown>>;
  failed_steps?: Array<Record<string, unknown>>;
  next_action?: string | null;
}

export interface WorkflowLeafCall {
  step_id: string;
  leaf_id: string;
  status: WorkflowStepStatus;
  error: string | null;
  token_usage?: Record<string, unknown> | null;
}

export interface WorkflowRun {
  run_id: string;
  status: WorkflowRunStatus;
  definition_hash: string | null;
  definition_source: string | null;
  dynamic_workflow?: DynamicWorkflowRunMeta | null;
  outcome_evidence?: WorkflowOutcomeEvidence | null;
  repair_plan?: WorkflowRepairPlan | null;
  steps: WorkflowRunStep[];
  leaf_calls: WorkflowLeafCall[];
}

export interface WorkflowStartResult {
  run_id: string;
  status: WorkflowRunStatus;
  reason: string | null;
  definition_hash: string;
  confirmation_required: boolean;
  confirmation_reasons: string[];
  preview_id: string;
  reconciliation_pending?: boolean;
}

export interface WorkflowDefinitionRecord {
  id: string;
  name: string;
  description: string;
  definition_version: number;
  definition_hash: string;
  status: WorkflowDefinitionStatus;
  visibility_scope: string;
  owner_type: string;
  owner_id: string | null;
  call_policy: Record<string, unknown> | null;
  promoted_from_run_id: string | null;
}

/** One row of the agent's run history — the archived ephemeral flow. */
export interface WorkflowRunSummary {
  run_id: string;
  status: WorkflowRunStatus;
  name: string;
  description: string;
  definition_source: string | null;
  definition_hash: string | null;
  created_at: string | null;
  completed_at: string | null;
  steps_total: number;
  steps_done: number;
  steps_failed: number;
  promoted_definition_id: string | null;
  dynamic_workflow?: DynamicWorkflowRunMeta | null;
  outcome_evidence?: WorkflowOutcomeEvidence | null;
  repair_plan?: WorkflowRepairPlan | null;
}

/** Repeated-run evidence: "this flow ran N times — suggest 固化". */
export interface WorkflowPromoteSuggestion {
  definition_hash: string;
  name: string;
  run_count: number;
  sample_run_ids: string[];
  quality_evidence?: WorkflowOutcomeEvidence | null;
}

/** trigger.config.workflow_ref — the creation-time pin (§6.2 / P8). */
export interface WorkflowTriggerRef {
  definition_name: string;
  definition_version: number;
  definition_hash: string;
  args?: Record<string, unknown>;
}

export type WorkflowTriggerPinStatus = 'pinned' | 'version_mismatch' | 'hash_mismatch' | 'missing';

// ---------------------------------------------------------------------------
// ephemeral runs (agent-scoped)
// ---------------------------------------------------------------------------

export function previewWorkflow(
  agentId: string,
  definition: Record<string, unknown>,
  args: Record<string, unknown> = {},
): Promise<WorkflowPreview> {
  return post<WorkflowPreview>(`/agents/${agentId}/workflows/preview`, { definition, args });
}

export function getWorkflowPreview(agentId: string, previewId: string): Promise<WorkflowPreview> {
  return get<WorkflowPreview>(`/agents/${agentId}/workflows/previews/${previewId}`);
}

export function previewWorkflowCandidate(
  agentId: string,
  proposalId: string,
  candidateId: string,
): Promise<WorkflowPreview> {
  return post<WorkflowPreview>(
    `/agents/${agentId}/workflows/proposals/${encodeURIComponent(proposalId)}/candidates/${encodeURIComponent(candidateId)}/preview`,
  );
}

export interface StartWorkflowOptions {
  previewId: string;
  confirmedPlanId?: string;
  planVersion?: number;
  planHash?: string;
  ledgerTodoId?: string;
}

export function startWorkflow(
  agentId: string,
  options: StartWorkflowOptions,
): Promise<WorkflowStartResult> {
  return post<WorkflowStartResult>(`/agents/${agentId}/workflows/runs`, {
    preview_id: options.previewId,
    confirmed_plan_id: options.confirmedPlanId ?? null,
    plan_version: options.planVersion ?? null,
    plan_hash: options.planHash ?? null,
    ledger_todo_id: options.ledgerTodoId ?? null,
  });
}

export function getWorkflowRun(agentId: string, runId: string): Promise<WorkflowRun> {
  return get<WorkflowRun>(`/agents/${agentId}/workflows/runs/${runId}`);
}

export function cancelWorkflowRun(agentId: string, runId: string): Promise<{ run_id: string; status: string }> {
  return post<{ run_id: string; status: string }>(`/agents/${agentId}/workflows/runs/${runId}/cancel`);
}

export function repairWorkflowRun(
  agentId: string,
  runId: string,
): Promise<{ run_id: string; status: WorkflowRunStatus; reason: string | null }> {
  return post<{ run_id: string; status: WorkflowRunStatus; reason: string | null }>(
    `/agents/${agentId}/workflows/runs/${runId}/repair`,
  );
}

export interface WorkflowGateDecisionResult {
  run_id: string;
  status: WorkflowRunStatus;
  step_id: string;
  decision: 'approve' | 'reject';
  replayed?: boolean;
}

export function decideWorkflowGate(
  agentId: string,
  runId: string,
  stepId: string,
  decision: 'approve' | 'reject',
): Promise<WorkflowGateDecisionResult> {
  return post<WorkflowGateDecisionResult>(`/agents/${agentId}/workflows/runs/${runId}/gate-decision`, {
    step_id: stepId,
    decision,
  });
}

export function listWorkflowRuns(agentId: string, limit = 50): Promise<WorkflowRunSummary[]> {
  return get<WorkflowRunSummary[]>(`/agents/${agentId}/workflows/runs?limit=${limit}`);
}

/** 固化: archive → registered DRAFT. Activation still walks approve-promotion. */
export function promoteWorkflowRun(agentId: string, runId: string): Promise<WorkflowDefinitionRecord> {
  return post<WorkflowDefinitionRecord>(`/agents/${agentId}/workflows/runs/${runId}/promote`);
}

export function listPromoteSuggestions(agentId: string): Promise<WorkflowPromoteSuggestion[]> {
  return get<WorkflowPromoteSuggestion[]>(`/agents/${agentId}/workflows/promote-suggestions`);
}

// ---------------------------------------------------------------------------
// registered definitions (tenant-scoped)
// ---------------------------------------------------------------------------

export interface CreateDefinitionDraftOptions {
  visibilityScope?: string;
  ownerType?: string;
  ownerId?: string;
  callPolicy?: Record<string, unknown>;
}

export function createWorkflowDefinitionDraft(
  definition: Record<string, unknown>,
  options: CreateDefinitionDraftOptions = {},
): Promise<WorkflowDefinitionRecord> {
  return post<WorkflowDefinitionRecord>('/workflow-definitions', {
    definition,
    visibility_scope: options.visibilityScope ?? 'agent',
    owner_type: options.ownerType ?? 'user',
    owner_id: options.ownerId ?? null,
    call_policy: options.callPolicy ?? null,
  });
}

export function listWorkflowDefinitions(agentId?: string): Promise<WorkflowDefinitionRecord[]> {
  const query = agentId ? `?agent_id=${encodeURIComponent(agentId)}` : '';
  return get<WorkflowDefinitionRecord[]>(`/workflow-definitions${query}`);
}

export function activateWorkflowDefinition(definitionId: string): Promise<WorkflowDefinitionRecord> {
  return post<WorkflowDefinitionRecord>(`/workflow-definitions/${definitionId}/activate`);
}

export function deprecateWorkflowDefinition(definitionId: string): Promise<WorkflowDefinitionRecord> {
  return post<WorkflowDefinitionRecord>(`/workflow-definitions/${definitionId}/deprecate`);
}

export function revokeWorkflowDefinition(definitionId: string): Promise<WorkflowDefinitionRecord> {
  return post<WorkflowDefinitionRecord>(`/workflow-definitions/${definitionId}/revoke`);
}

export function approveWorkflowPromotion(definitionId: string): Promise<WorkflowDefinitionRecord> {
  return post<WorkflowDefinitionRecord>(`/workflow-definitions/${definitionId}/approve-promotion`);
}

export function forkWorkflowDefinition(
  definitionId: string,
  agentId: string,
  patch: Record<string, unknown> = {},
  version?: number,
): Promise<{ definition: Record<string, unknown>; source_definition_id: string }> {
  return post<{ definition: Record<string, unknown>; source_definition_id: string }>(
    `/workflow-definitions/${definitionId}/fork`,
    { agent_id: agentId, patch, version: version ?? null },
  );
}

// ---------------------------------------------------------------------------
// trigger pin status (§6.2: authorization binds to creation-time version+hash)
// ---------------------------------------------------------------------------

/**
 * Classify a trigger's workflow_ref against the CURRENT registered records:
 * 'pinned' when the exact version+hash still exists; mismatches mean the
 * trigger needs re-confirmation before it can run the newer definition.
 */
export function classifyTriggerPin(
  ref: WorkflowTriggerRef,
  records: WorkflowDefinitionRecord[],
): WorkflowTriggerPinStatus {
  const sameName = records.filter((record) => record.name === ref.definition_name);
  if (sameName.length === 0) return 'missing';
  const sameVersion = sameName.find((record) => record.definition_version === ref.definition_version);
  if (!sameVersion) return 'version_mismatch';
  if (sameVersion.definition_hash !== ref.definition_hash) return 'hash_mismatch';
  return 'pinned';
}
