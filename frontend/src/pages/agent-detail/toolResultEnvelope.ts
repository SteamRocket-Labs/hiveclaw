export interface CreateEmployeeToolResult {
  agentId: string;
  agentName: string | null;
  message: string;
  warnings: string[];
  manualSteps: string[];
  raw: string;
}

export interface HrPreviewToolResult {
  kind: 'hr_preview';
  blueprintId: string | null;
  blueprintVersion: number;
  blueprintHash: string | null;
  status: string;
  name: string | null;
  mission: string | null;
  firstMission: string | null;
  primaryUsers: string[];
  coreOutputs: string[];
  boundaries: string | null;
  permissionScope: string | null;
  sourceAttributions: Record<string, unknown>[];
  riskClass: string | null;
  missingGates: string[];
  knowledgeDebt: unknown[];
  confirmationRequirements: unknown[];
  readyNow: string[];
  willInstall: string[];
  deferredCapabilities: string[];
  warnings: string[];
  manualSteps: string[];
}

export interface CreateEmployeeSuccessToolMeta {
  kind: 'create_employee_success';
  agentId: string;
  agentName: string | null;
  message: string;
  warnings: string[];
  manualSteps: string[];
}

export interface HrCreationHandoffToolMeta {
  kind: 'hr_creation_handoff';
}

export interface PlanProposalToolMeta {
  kind: 'plan_proposal';
  planId: string;
  planVersion: number;
  planHash: string | null;
  status: string;
  summary: string | null;
  nextAction: string | null;
  planJson: Record<string, unknown>;
}

export interface DynamicWorkflowCandidateMeta {
  candidateId: string;
  name: string | null;
  patternMix: string[];
  riskLevel: string | null;
  plannedLeafCalls: number | null;
  budgetTokens: number | null;
  confirmationRequired: boolean;
  definitionHash: string | null;
  argsHash: string | null;
}

export interface DynamicWorkflowProposalToolMeta {
  kind: 'dynamic_workflow_proposal';
  proposalId: string;
  goal: string | null;
  whyWorkflow: string | null;
  successCriteria: string[];
  recommendedCandidateId: string | null;
  nextAction: string | null;
  candidates: DynamicWorkflowCandidateMeta[];
}

export interface WorkflowPreviewToolMeta {
  kind: 'workflow_preview';
  previewId: string;
  sessionId: string | null;
  previewStatus: 'ready' | 'starting' | 'started' | 'failed' | 'expired';
  proposalId: string | null;
  candidateId: string | null;
  confirmationRequired: boolean;
  confirmationReasons: string[];
  plannedLeafCalls: number | null;
  budgetTokens: number | null;
}

export interface ClarificationOption {
  label: string;
  description: string;
}

export interface ClarificationQuestion {
  question: string;
  header: string;
  options: ClarificationOption[];
  multiSelect: boolean;
}

export interface UserClarificationToolMeta {
  kind: 'user_clarification';
  questions: ClarificationQuestion[];
  blocking: boolean;
  nextAction: string | null;
  answered?: boolean;
  answeredByEventId?: string | null;
  answerText?: string | null;
  answeredAt?: string | null;
}

export interface PlanModeRequestToolMeta {
  kind: 'plan_mode_request';
  reason: string;
  nextAction: string | null;
}

export interface RuntimeStepToolMeta {
  kind: 'runtime_step';
  toolCallId: string | null;
  stepId: string | null;
  durationMs: number | null;
  visibility: 'visible' | 'collapsed' | 'debug' | 'redacted';
  status: string | null;
}

export type ToolCallMeta =
  | HrPreviewToolResult
  | HrCreationHandoffToolMeta
  | CreateEmployeeSuccessToolMeta
  | PlanProposalToolMeta
  | DynamicWorkflowProposalToolMeta
  | WorkflowPreviewToolMeta
  | UserClarificationToolMeta
  | PlanModeRequestToolMeta
  | RuntimeStepToolMeta;

export interface NormalizedToolCallResult {
  displayResult: string;
  createdAgentId: string | null;
  raw: string;
  toolMeta: ToolCallMeta | null;
}

function coerceToolResultToString(rawResult: unknown): string {
  if (typeof rawResult === 'string') {
    return rawResult;
  }
  if (rawResult == null) {
    return '';
  }
  try {
    return JSON.stringify(rawResult);
  } catch {
    return String(rawResult);
  }
}

function parseStructuredToolPayload(rawResult: unknown): Record<string, unknown> | null {
  if (!rawResult) {
    return null;
  }
  if (typeof rawResult === 'object' && !Array.isArray(rawResult)) {
    return rawResult as Record<string, unknown>;
  }
  if (typeof rawResult !== 'string' || !rawResult.trim()) {
    return null;
  }
  try {
    const parsed = JSON.parse(rawResult.trim());
    if (parsed && typeof parsed === 'object' && !Array.isArray(parsed)) {
      return parsed as Record<string, unknown>;
    }
  } catch {
    return null;
  }
  return null;
}

function normalizeStringList(value: unknown): string[] {
  if (!Array.isArray(value)) {
    return [];
  }
  return value.filter((item): item is string => typeof item === 'string' && item.trim().length > 0);
}

function normalizeRecordList(value: unknown): Record<string, unknown>[] {
  if (!Array.isArray(value)) return [];
  return value.filter(
    (item): item is Record<string, unknown> => Boolean(item) && typeof item === 'object' && !Array.isArray(item),
  );
}

function normalizeDisplayList(value: unknown): unknown[] {
  if (Array.isArray(value)) return value;
  if (!value || typeof value !== 'object') return [];
  return Object.entries(value as Record<string, unknown>)
    .filter(([, entry]) => entry === true || (entry !== false && entry != null && entry !== ''))
    .map(([key, entry]) => (entry === true ? key : `${key}: ${String(entry)}`));
}

export function parseCreateEmployeeToolResult(rawResult: unknown): CreateEmployeeToolResult | null {
  const raw = coerceToolResultToString(rawResult).trim();
  const parsed = parseStructuredToolPayload(rawResult);
  if (
    parsed?.status === 'success'
    && typeof parsed.agent_id === 'string'
    && typeof parsed.message === 'string'
  ) {
    return {
      agentId: parsed.agent_id,
      agentName: typeof parsed.agent_name === 'string' ? parsed.agent_name : null,
      message: parsed.message,
      warnings: normalizeStringList(parsed.warnings),
      manualSteps: normalizeStringList(parsed.manual_steps),
      raw,
    };
  }

  const idMatch = raw.match(/ID:\s*([0-9a-f-]{36})/i);
  if (!idMatch) {
    return null;
  }

  return {
    agentId: idMatch[1],
    agentName: null,
    message: raw,
    warnings: [],
    manualSteps: [],
    raw,
  };
}

export function parsePreviewAgentBlueprintResult(rawResult: unknown): HrPreviewToolResult | null {
  const parsed = parseStructuredToolPayload(rawResult);
  if (parsed?.status !== 'preview') {
    return null;
  }

  const blueprint =
    parsed.blueprint && typeof parsed.blueprint === 'object' && !Array.isArray(parsed.blueprint)
      ? (parsed.blueprint as Record<string, unknown>)
      : {};
  const summary =
    parsed.summary && typeof parsed.summary === 'object' && !Array.isArray(parsed.summary)
      ? (parsed.summary as Record<string, unknown>)
      : {};

  return {
    kind: 'hr_preview',
    blueprintId: typeof parsed.blueprint_id === 'string' ? parsed.blueprint_id : null,
    blueprintVersion: typeof parsed.blueprint_version === 'number' ? parsed.blueprint_version : 1,
    blueprintHash: typeof parsed.blueprint_hash === 'string' ? parsed.blueprint_hash : null,
    status: typeof parsed.draft_status === 'string' ? parsed.draft_status : 'legacy_preview',
    name: typeof blueprint.name === 'string' ? blueprint.name : null,
    mission: typeof summary.mission === 'string' ? summary.mission : null,
    firstMission: typeof summary.first_mission === 'string' ? summary.first_mission : null,
    primaryUsers: normalizeStringList(blueprint.primary_users),
    coreOutputs: normalizeStringList(blueprint.core_outputs),
    boundaries: typeof blueprint.boundaries === 'string' ? blueprint.boundaries : null,
    permissionScope: typeof blueprint.permission_scope === 'string' ? blueprint.permission_scope : null,
    sourceAttributions: normalizeRecordList(blueprint.source_attributions),
    riskClass: typeof parsed.risk_class === 'string' ? parsed.risk_class : null,
    missingGates: normalizeStringList(parsed.missing_gates),
    knowledgeDebt: normalizeDisplayList(parsed.knowledge_debt),
    confirmationRequirements: normalizeDisplayList(parsed.confirmation_requirements),
    readyNow: normalizeStringList(parsed.ready_now ?? blueprint.ready_now),
    willInstall: normalizeStringList(parsed.will_install),
    deferredCapabilities: normalizeStringList(blueprint.deferred_capabilities ?? parsed.deferred_capabilities),
    warnings: normalizeStringList(parsed.warnings),
    manualSteps: normalizeStringList(parsed.manual_steps),
  };
}

function buildPreviewDisplayResult(preview: HrPreviewToolResult): string {
  if (preview.name) {
    return `Blueprint preview ready for ${preview.name}.`;
  }
  return 'Agent blueprint preview ready.';
}

function parsePlanProposalResult(rawResult: unknown): PlanProposalToolMeta | null {
  const parsed = parseStructuredToolPayload(rawResult);
  if (!parsed) {
    return null;
  }
  const status = parsed.status;
  if ((status !== 'needs_plan' && status !== 'planning_failed') || typeof parsed.plan_id !== 'string') {
    return null;
  }
  const planJson =
    parsed.plan_json && typeof parsed.plan_json === 'object' && !Array.isArray(parsed.plan_json)
      ? (parsed.plan_json as Record<string, unknown>)
      : parsed.plan_preview && typeof parsed.plan_preview === 'object' && !Array.isArray(parsed.plan_preview)
        ? (parsed.plan_preview as Record<string, unknown>)
        : {};
  return {
    kind: 'plan_proposal',
    planId: parsed.plan_id,
    planVersion: typeof parsed.plan_version === 'number' ? parsed.plan_version : 1,
    planHash: typeof parsed.plan_hash === 'string' ? parsed.plan_hash : null,
    status,
    summary: typeof parsed.summary === 'string' ? parsed.summary : null,
    nextAction: typeof parsed.next_action === 'string' ? parsed.next_action : null,
    planJson,
  };
}

function buildPlanProposalDisplayResult(meta: PlanProposalToolMeta): string {
  if (meta.status === 'planning_failed') {
    return meta.summary || 'Plan failed validation and needs revision.';
  }
  return meta.summary || 'Plan created — confirm before this autonomous work can begin.';
}

function normalizeNumber(value: unknown): number | null {
  return typeof value === 'number' && Number.isFinite(value) ? value : null;
}

function parseDynamicWorkflowCandidate(value: unknown): DynamicWorkflowCandidateMeta | null {
  if (!value || typeof value !== 'object' || Array.isArray(value)) {
    return null;
  }
  const record = value as Record<string, unknown>;
  const candidateId = typeof record.candidate_id === 'string' ? record.candidate_id.trim() : '';
  if (!candidateId) {
    return null;
  }
  return {
    candidateId,
    name: typeof record.name === 'string' ? record.name : null,
    patternMix: normalizeStringList(record.pattern_mix),
    riskLevel: typeof record.risk_level === 'string' ? record.risk_level : null,
    plannedLeafCalls: normalizeNumber(record.planned_leaf_calls),
    budgetTokens: normalizeNumber(record.budget_tokens),
    confirmationRequired: record.confirmation_required === true,
    definitionHash: typeof record.definition_hash === 'string' ? record.definition_hash : null,
    argsHash: typeof record.args_hash === 'string' ? record.args_hash : null,
  };
}

function parseDynamicWorkflowProposalResult(rawResult: unknown): DynamicWorkflowProposalToolMeta | null {
  const parsed = parseStructuredToolPayload(rawResult);
  if (parsed?.status !== 'dynamic_workflow_proposed' || parsed.ok !== true || typeof parsed.proposal_id !== 'string') {
    return null;
  }
  const candidates = Array.isArray(parsed.candidates)
    ? parsed.candidates.flatMap((candidate) => {
        const parsedCandidate = parseDynamicWorkflowCandidate(candidate);
        return parsedCandidate ? [parsedCandidate] : [];
      })
    : [];
  if (candidates.length === 0) {
    return null;
  }
  return {
    kind: 'dynamic_workflow_proposal',
    proposalId: parsed.proposal_id,
    goal: typeof parsed.goal === 'string' ? parsed.goal : null,
    whyWorkflow: typeof parsed.why_workflow === 'string' ? parsed.why_workflow : null,
    successCriteria: normalizeStringList(parsed.success_criteria),
    recommendedCandidateId:
      typeof parsed.recommended_candidate_id === 'string' ? parsed.recommended_candidate_id : null,
    nextAction: typeof parsed.next_action === 'string' ? parsed.next_action : null,
    candidates,
  };
}

function buildDynamicWorkflowProposalDisplayResult(meta: DynamicWorkflowProposalToolMeta): string {
  return meta.goal
    ? `Dynamic Workflow proposal ready: ${meta.goal}`
    : 'Dynamic Workflow proposal ready.';
}

function parseWorkflowPreviewResult(rawResult: unknown): WorkflowPreviewToolMeta | null {
  const parsed = parseStructuredToolPayload(rawResult);
  const previewId = typeof parsed?.preview_id === 'string' ? parsed.preview_id.trim() : '';
  if (parsed?.ok !== true || !previewId || typeof parsed.planned_leaf_calls !== 'number') {
    return null;
  }
  const status = typeof parsed.preview_status === 'string' ? parsed.preview_status : 'ready';
  const previewStatus = ['ready', 'starting', 'started', 'failed', 'expired'].includes(status)
    ? (status as WorkflowPreviewToolMeta['previewStatus'])
    : 'ready';
  return {
    kind: 'workflow_preview',
    previewId,
    sessionId: typeof parsed.session_id === 'string' ? parsed.session_id : null,
    previewStatus,
    proposalId: typeof parsed.proposal_id === 'string' ? parsed.proposal_id : null,
    candidateId: typeof parsed.candidate_id === 'string' ? parsed.candidate_id : null,
    confirmationRequired: parsed.confirmation_required === true,
    confirmationReasons: normalizeStringList(parsed.confirmation_reasons),
    plannedLeafCalls: normalizeNumber(parsed.planned_leaf_calls),
    budgetTokens: normalizeNumber(parsed.budget_tokens),
  };
}

function parseClarificationOptions(value: unknown): ClarificationOption[] {
  if (!Array.isArray(value)) {
    return [];
  }
  return value.flatMap((entry) => {
    if (!entry || typeof entry !== 'object' || Array.isArray(entry)) {
      return [];
    }
    const record = entry as Record<string, unknown>;
    const label = typeof record.label === 'string' ? record.label.trim() : '';
    if (!label) {
      return [];
    }
    const description = typeof record.description === 'string' ? record.description.trim() : '';
    return [{ label, description }];
  });
}

function parseClarificationQuestions(value: unknown): ClarificationQuestion[] {
  if (!Array.isArray(value)) {
    return [];
  }
  return value.flatMap((entry) => {
    if (!entry || typeof entry !== 'object' || Array.isArray(entry)) {
      return [];
    }
    const record = entry as Record<string, unknown>;
    const question = typeof record.question === 'string' ? record.question.trim() : '';
    if (!question) {
      return [];
    }
    return [
      {
        question,
        header: typeof record.header === 'string' ? record.header.trim() : '',
        options: parseClarificationOptions(record.options),
        multiSelect: record.multiSelect === true || record.multi_select === true,
      },
    ];
  });
}

function parseUserClarificationResult(rawResult: unknown): UserClarificationToolMeta | null {
  const parsed = parseStructuredToolPayload(rawResult);
  if (parsed?.status !== 'awaiting_user_clarification') {
    return null;
  }
  const questions = parseClarificationQuestions(parsed.questions);
  if (questions.length === 0) {
    return null;
  }
  return {
    kind: 'user_clarification',
    questions,
    blocking: parsed.blocking !== false,
    nextAction: typeof parsed.next_action === 'string' ? parsed.next_action : null,
  };
}

function buildUserClarificationDisplayResult(meta: UserClarificationToolMeta): string {
  if (meta.questions.length === 1) {
    return meta.questions[0].question;
  }
  return `The agent needs your input on ${meta.questions.length} questions.`;
}

export function parsePlanModeRequestResult(rawResult: unknown): PlanModeRequestToolMeta | null {
  const parsed = parseStructuredToolPayload(rawResult);
  if (parsed?.status !== 'plan_mode_entry_requested') {
    return null;
  }
  const reason = typeof parsed.reason === 'string' ? parsed.reason.trim() : '';
  if (!reason) {
    return null;
  }
  return {
    kind: 'plan_mode_request',
    reason,
    nextAction: typeof parsed.next_action === 'string' ? parsed.next_action : null,
  };
}

function buildPlanModeRequestDisplayResult(meta: PlanModeRequestToolMeta): string {
  return meta.reason;
}

export function normalizeToolCallResult(toolName: string | undefined, rawResult: unknown): NormalizedToolCallResult {
  const raw = coerceToolResultToString(rawResult);

  if (toolName === 'start_hr_agent_handoff' && raw.trim()) {
    return { displayResult: '', createdAgentId: null, raw, toolMeta: { kind: 'hr_creation_handoff' } };
  }

  const planProposal = parsePlanProposalResult(rawResult);
  if (planProposal) {
    return {
      displayResult: buildPlanProposalDisplayResult(planProposal),
      createdAgentId: null,
      raw,
      toolMeta: planProposal,
    };
  }

  const dynamicWorkflowProposal = parseDynamicWorkflowProposalResult(rawResult);
  if (dynamicWorkflowProposal) {
    return {
      displayResult: buildDynamicWorkflowProposalDisplayResult(dynamicWorkflowProposal),
      createdAgentId: null,
      raw,
      toolMeta: dynamicWorkflowProposal,
    };
  }

  const workflowPreview = parseWorkflowPreviewResult(rawResult);
  if (workflowPreview) {
    return {
      displayResult: workflowPreview.confirmationRequired
        ? 'Workflow preview ready — review and confirm this exact run.'
        : 'Workflow preview ready.',
      createdAgentId: null,
      raw,
      toolMeta: workflowPreview,
    };
  }

  const userClarification = parseUserClarificationResult(rawResult);
  if (userClarification) {
    return {
      displayResult: buildUserClarificationDisplayResult(userClarification),
      createdAgentId: null,
      raw,
      toolMeta: userClarification,
    };
  }

  const planModeRequest = parsePlanModeRequestResult(rawResult);
  if (planModeRequest) {
    return {
      displayResult: buildPlanModeRequestDisplayResult(planModeRequest),
      createdAgentId: null,
      raw,
      toolMeta: planModeRequest,
    };
  }

  if (toolName === 'preview_agent_blueprint' || toolName === 'create_digital_employee') {
    const preview = parsePreviewAgentBlueprintResult(rawResult);
    if (preview) {
      return {
        displayResult: buildPreviewDisplayResult(preview),
        createdAgentId: null,
        raw,
        toolMeta: preview,
      };
    }

    const parsed = parseCreateEmployeeToolResult(rawResult);
    if (parsed) {
      return {
        displayResult: parsed.message,
        createdAgentId: parsed.agentId,
        raw: parsed.raw,
        toolMeta: {
          kind: 'create_employee_success',
          agentId: parsed.agentId,
          agentName: parsed.agentName,
          message: parsed.message,
          warnings: parsed.warnings,
          manualSteps: parsed.manualSteps,
        },
      };
    }
  }

  return {
    displayResult: raw,
    createdAgentId: null,
    raw,
    toolMeta: null,
  };
}
