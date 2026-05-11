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
  name: string | null;
  mission: string | null;
  firstMission: string | null;
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

export interface DeepResearchToolMeta {
  kind: 'deep_research';
  taskId: string | null;
  status: string | null;
  summary: string | null;
  reportPath: string | null;
  artifactDir: string | null;
  sourceCount: number | null;
  claimCount: number | null;
  qualityGates: Record<string, string>;
  gaps: string[];
}

export type ToolCallMeta = HrPreviewToolResult | CreateEmployeeSuccessToolMeta | DeepResearchToolMeta;

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
    name: typeof blueprint.name === 'string' ? blueprint.name : null,
    mission: typeof summary.mission === 'string' ? summary.mission : null,
    firstMission: typeof summary.first_mission === 'string' ? summary.first_mission : null,
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

function parseDeepResearchToolResult(toolName: string | undefined, rawResult: unknown): DeepResearchToolMeta | null {
  if (!toolName?.startsWith('deep_research_')) {
    return null;
  }
  const parsed = parseStructuredToolPayload(rawResult);
  if (!parsed) {
    return null;
  }
  const qualityGates: Record<string, string> = {};
  if (parsed.quality_gates && typeof parsed.quality_gates === 'object' && !Array.isArray(parsed.quality_gates)) {
    Object.entries(parsed.quality_gates as Record<string, unknown>).forEach(([key, value]) => {
      if (typeof value === 'string') {
        qualityGates[key] = value;
      }
    });
  }
  const sourceCount = typeof parsed.source_count === 'number' ? parsed.source_count : null;
  const claimCount = typeof parsed.claim_count === 'number' ? parsed.claim_count : null;

  return {
    kind: 'deep_research',
    taskId: typeof parsed.task_id === 'string' ? parsed.task_id : null,
    status: typeof parsed.status === 'string' ? parsed.status : null,
    summary: typeof parsed.summary === 'string' ? parsed.summary : null,
    reportPath: typeof parsed.report_path === 'string' ? parsed.report_path : null,
    artifactDir: typeof parsed.artifact_dir === 'string' ? parsed.artifact_dir : null,
    sourceCount,
    claimCount,
    qualityGates,
    gaps: normalizeStringList(parsed.gaps),
  };
}

function buildDeepResearchDisplayResult(meta: DeepResearchToolMeta): string {
  const status = meta.status || 'started';
  if (status === 'running') {
    return meta.taskId ? `Deep research running: ${meta.taskId}` : 'Deep research running';
  }
  return meta.summary || `Deep research ${status}`;
}

export function normalizeToolCallResult(toolName: string | undefined, rawResult: unknown): NormalizedToolCallResult {
  const raw = coerceToolResultToString(rawResult);

  const deepResearch = parseDeepResearchToolResult(toolName, rawResult);
  if (deepResearch) {
    return {
      displayResult: buildDeepResearchDisplayResult(deepResearch),
      createdAgentId: null,
      raw,
      toolMeta: deepResearch,
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
