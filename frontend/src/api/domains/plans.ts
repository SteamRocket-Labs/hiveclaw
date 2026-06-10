/**
 * Plan Mode frontend adapter (§11 of docs/plan-mode-design.md).
 *
 * Mirrors the agent-scoped REST surface in `backend/app/api/plans.py`:
 *
 *   POST   /agents/{agentId}/plans                  create + generate first version
 *   GET    /agents/{agentId}/plans                  list (newest first)
 *   GET    /agents/{agentId}/plans/{planId}         fetch one
 *   POST   /agents/{agentId}/plans/{planId}/revise  supersede + regenerate
 *   POST   /agents/{agentId}/plans/{planId}/regenerate retry same failed draft
 *   POST   /agents/{agentId}/plans/{planId}/confirm confirm (version + hash bound)
 *   POST   /agents/{agentId}/plans/{planId}/confirm-and-handoff confirm + start
 *   POST   /agents/{agentId}/plans/{planId}/reject  reject
 *   POST   /agents/{agentId}/plans/{planId}/handoff hand off to execution
 *
 * The confirm body binds to `plan_version` + `plan_hash` so the user confirms a
 * specific immutable plan version, never a mutable chat blob (§8.2).
 */

import { get, post } from '../core';

/** Lifecycle status of a plan request (§7). */
export type PlanStatus =
  | 'draft'
  | 'planning'
  | 'planning_failed'
  | 'awaiting_confirmation'
  | 'confirmed'
  | 'rejected'
  | 'superseded'
  | 'expired';

/** What kind of autonomous work the plan governs (§6.1). */
export type PlanIntentType =
  | 'autonomous_wake'
  | 'long_task'
  | 'delegation'
  | 'external_action'
  | 'state_change';

/** Outcome of a confirmed plan's handoff to the execution layer (§13).
 *
 * ``queued`` (CC-align §4.2): a current-session continuation that is confirmed but
 * waiting for an already-active run to finish before it starts. */
export type PlanHandoffStatus = 'not_started' | 'completed' | 'failed' | 'skipped' | 'queued';

/** Risk tier surfaced on the card (§6.3 `risk_assessment.level`). */
export type PlanRiskLevel = 'low' | 'medium' | 'high';

// ---------------------------------------------------------------------------
// plan_json sub-structures (§6.3 — the execution contract the card renders)
// ---------------------------------------------------------------------------

export interface PlanStep {
  order: number;
  description: string;
  expected_output?: string;
}

export interface PlanWakePolicy {
  type?: string;
  timezone?: string;
  expr?: string;
  minutes?: number;
  at?: string;
  [key: string]: unknown;
}

export interface PlanExternalSideEffect {
  kind?: string;
  channel?: string;
  audience?: string;
  requires_confirmation?: boolean;
  [key: string]: unknown;
}

export interface PlanRiskAssessment {
  level?: PlanRiskLevel | string;
  reasons?: string[];
}

export interface PlanEstimatedCost {
  tokens_per_run?: string;
  expected_duration?: string;
  [key: string]: unknown;
}

export interface PlanHandoffSpec {
  target?: string;
  create_trigger?: boolean;
  [key: string]: unknown;
}

/** Structured plan body — the canonical execution contract (§6.3). */
export interface PlanJson {
  schema?: string;
  title?: string;
  intent_type?: PlanIntentType | string;
  objective?: string;
  motivation?: string;
  /** Agent-authored markdown plan body (CC-align §4.1) — the card's primary
   * surface; structured fields below are folded governance detail. */
  plan_markdown?: string;
  steps?: PlanStep[];
  success_criteria?: string[];
  wake_policy?: PlanWakePolicy | null;
  required_capabilities?: string[];
  external_side_effects?: PlanExternalSideEffect[];
  risk_assessment?: PlanRiskAssessment | null;
  estimated_cost?: PlanEstimatedCost | null;
  stop_conditions?: string[];
  assumptions?: string[];
  open_questions?: string[];
  handoff?: PlanHandoffSpec | null;
  [key: string]: unknown;
}

/** A plan request row as returned by the REST layer (`PlanOut` in plans.py). */
export interface PlanRequest {
  id: string;
  agent_id: string;
  tenant_id: string | null;
  session_id: string | null;
  runtime_task_id: string | null;
  requested_by_user_id: string | null;
  source: string;
  intent_type: PlanIntentType | string;
  original_request: string;
  status: PlanStatus | string;
  plan_version: number;
  plan_hash: string | null;
  plan_markdown_path: string | null;
  plan_json: PlanJson;
  handoff_status: PlanHandoffStatus | string | null;
  handoff_payload: Record<string, unknown> | null;
  confirmed_by_user_id: string | null;
  confirmed_at: string | null;
  rejected_by_user_id: string | null;
  rejected_at: string | null;
  superseded_by_plan_id: string | null;
  expires_at: string | null;
  created_at: string | null;
  updated_at: string | null;
  metadata: Record<string, unknown>;
}

export interface PlanCreateInput {
  original_request: string;
  intent_type: PlanIntentType | string;
  source?: string;
  session_id?: string | null;
  fill?: Record<string, unknown>;
  metadata?: Record<string, unknown>;
}

export interface PlanReviseInput {
  fill?: Record<string, unknown>;
}

/** Confirm body — binds to the exact immutable version (§8.2). */
export interface PlanConfirmInput {
  plan_version: number;
  plan_hash: string;
  reason?: string;
}

export interface PlanDecisionInput {
  reason?: string;
}

export interface PlanRecommendation {
  id: string;
  agent_id: string;
  tenant_id: string | null;
  session_id: string;
  runtime_task_id: string | null;
  recommended_to_user_id: string;
  source: string;
  intent_type: PlanIntentType | string;
  action_kind: string;
  tool_name: string;
  title: string;
  original_request: string;
  status: 'recommended' | 'declined' | 'accepted' | 'expired' | string;
  declined_by_user_id: string | null;
  declined_at: string | null;
  accepted_by_user_id: string | null;
  accepted_at: string | null;
  created_at: string | null;
  updated_at: string | null;
  metadata: Record<string, unknown>;
}

export interface PlanRecommendationCreateInput {
  original_request: string;
  session_id: string;
  source?: string;
  intent_type?: PlanIntentType | string;
  action_kind?: string;
  tool_name?: string;
  title?: string;
  metadata?: Record<string, unknown>;
}

export interface PlanConfirmResult {
  ok: boolean;
  status: PlanStatus | string;
  plan_id: string;
  handoff_status: PlanHandoffStatus | string | null;
}

export interface PlanHandoffResult {
  ok: boolean;
  status: PlanStatus | string;
  plan_id: string;
  handoff_status: PlanHandoffStatus | string | null;
  handoff_payload: Record<string, unknown> | null;
}

export const planApi = {
  list: (agentId: string, limit = 50) =>
    get<PlanRequest[]>(`/agents/${agentId}/plans?limit=${limit}`),
  get: (agentId: string, planId: string) =>
    get<PlanRequest>(`/agents/${agentId}/plans/${planId}`),
  create: (agentId: string, data: PlanCreateInput) =>
    post<PlanRequest>(`/agents/${agentId}/plans`, data),
  revise: (agentId: string, planId: string, data: PlanReviseInput = {}) =>
    post<PlanRequest>(`/agents/${agentId}/plans/${planId}/revise`, data),
  regenerate: (agentId: string, planId: string, data: PlanReviseInput = {}) =>
    post<PlanRequest>(`/agents/${agentId}/plans/${planId}/regenerate`, data),
  confirm: (agentId: string, planId: string, data: PlanConfirmInput) =>
    post<PlanConfirmResult>(`/agents/${agentId}/plans/${planId}/confirm`, data),
  confirmAndHandoff: (agentId: string, planId: string, data: PlanConfirmInput) =>
    post<PlanHandoffResult>(`/agents/${agentId}/plans/${planId}/confirm-and-handoff`, data),
  reject: (agentId: string, planId: string, data: PlanDecisionInput = {}) =>
    post<PlanRequest>(`/agents/${agentId}/plans/${planId}/reject`, data),
  handoff: (agentId: string, planId: string) =>
    post<PlanHandoffResult>(`/agents/${agentId}/plans/${planId}/handoff`, {}),
  createRecommendation: (agentId: string, data: PlanRecommendationCreateInput) =>
    post<PlanRecommendation>(`/agents/${agentId}/plan-recommendations`, data),
  declineRecommendation: (agentId: string, recommendationId: string) =>
    post<PlanRecommendation>(`/agents/${agentId}/plan-recommendations/${recommendationId}/decline`, {}),
};
