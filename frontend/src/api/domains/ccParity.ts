import { get, patch, post } from '../core';

export interface CommandIndexEntry {
  name: string;
  canonical_name?: string;
  aliases: string[];
  description: string;
  category: string;
  source: string;
  execution_mode: string;
  permission_mode: string;
  bridge_safe: boolean;
  remote_safe: boolean;
}

export interface CommandDefinition extends CommandIndexEntry {
  handler_ref: string;
  input_schema: Record<string, unknown>;
  visible_to_model: boolean;
  visible_to_user: boolean;
}

export interface ExecuteCommandInput {
  arguments?: Record<string, unknown>;
  session_id?: string | null;
}

export interface ListCommandsOptions {
  surface?: 'agent_prompt' | 'user';
  includeOptionalPacks?: boolean;
}

export interface ExecuteCommandResult {
  ok: boolean;
  command: string;
  result:
    | unknown
    | {
      ok?: boolean;
      command?: string;
      action?: string;
      session_id?: string;
      ui_action?: Record<string, unknown>;
      control_event?: Record<string, unknown> | null;
      debug_payload?: Record<string, unknown>;
      [key: string]: unknown;
    };
}

export interface StartGoalInput {
  objective: string;
  token_budget?: number | null;
  max_continuation_turns?: number | null;
  time_budget_seconds?: number | null;
}

export interface SessionGoal {
  id: string;
  agent_id: string;
  session_id: string;
  objective: string;
  status: string;
  token_budget: number | null;
  tokens_used: number;
  max_continuation_turns: number | null;
  continuation_count: number;
}

export interface GoalContinuationResult {
  ok: boolean;
  goal_id: string;
  decision?: Record<string, unknown>;
  run?: Record<string, unknown>;
}

export interface StartAdvancedPlanInput {
  objective: string;
  context?: Record<string, unknown>;
}

export interface AdvancedPlanRun {
  run_id?: string;
  status?: string;
  [key: string]: unknown;
}

export interface CreateAgentTeamMemberInput {
  name: string;
  role?: string;
  model_id?: string | null;
  tool_policy?: Record<string, unknown>;
  budget?: Record<string, unknown>;
}

export interface CreateAgentTeamInput {
  parent_session_id: string;
  name: string;
  members: CreateAgentTeamMemberInput[];
}

export interface AgentTeamMember {
  id: string;
  member_name: string;
  member_role: string | null;
  chat_session_id: string;
  runtime_task_id: string | null;
  runtime_task_type: string;
  status: string;
  summary?: string;
  t0_refs?: unknown[];
  artifacts?: unknown[];
}

export interface AgentTeam {
  id: string;
  name: string;
  status: string;
  transcript_truth: string;
  lead_agent_id: string;
  parent_session_id: string;
  members: AgentTeamMember[];
}

export interface AgentTeamEnterResult {
  team_id: string;
  member_id: string;
  chat_session_id: string;
  runtime_task_id: string | null;
  runtime_task_type: string;
  status: string;
}

export interface AgentTeamCloseResult extends AgentTeam {
  consolidation_plan: Record<string, unknown>;
}

export interface SessionWorkbench {
  schema: string;
  agent_id: string;
  session: Record<string, unknown>;
  active_turn?: {
    session_id: string;
    runtime_task_id?: string | null;
    turn_id?: string | null;
    status: string;
    expected_turn_id?: string | null;
    pending_steer_count: number;
  } | null;
  timeline?: {
    schema: string;
    truth_source: string;
    event_count: number;
    window_limit?: number;
    truncated?: boolean;
    events: Record<string, unknown>[];
  };
  tool_calls?: Record<string, unknown>[];
  approvals?: Record<string, unknown>[];
  hooks?: Record<string, unknown>[];
  compactions?: Record<string, unknown>[];
  context_window?: {
    schema: string;
    decision_count: number;
    latest_status?: Record<string, unknown> | null;
    latest_skipped?: Record<string, unknown> | null;
    latest_tool_result_budget?: Record<string, unknown> | null;
    decisions?: Record<string, unknown>[];
  };
  branches?: Record<string, unknown>[];
  permission_profile?: Record<string, unknown>;
  context_policy?: Record<string, unknown>;
  turn: {
    truth_source: string;
    event_count: number;
    checkpoint_count?: number;
    latest_event?: Record<string, unknown> | null;
    checkpoints?: Record<string, unknown>[];
  };
  controls: Record<string, unknown>;
  active_run?: Record<string, unknown> | null;
  runtime_tasks: Record<string, unknown>[];
  completion_wake_policy?: Record<string, unknown>;
  completion_wake_summary?: Record<string, unknown>;
  completion_wakes?: Record<string, unknown>[];
  goals: Record<string, unknown>[];
  teams: AgentTeam[];
  session_index?: Record<string, unknown> | null;
  links?: Record<string, string>;
}

export interface SessionJsonExport {
  schema: string;
  exported_at?: string;
  agent_id: string;
  session: Record<string, unknown>;
  workbench: SessionWorkbench;
  transcript: {
    truth_source: string;
    event_count: number;
    events: Record<string, unknown>[];
  };
}

export interface HookControlPlane {
  schema: string;
  agent_id: string;
  events: Array<{
    event: string;
    category: string;
    handler_count: number;
    blocking_supported: boolean;
    standard: boolean;
  }>;
  registered_events: string[];
  registrations: Array<Record<string, unknown>>;
}

export interface UpdateHookRuntimeConfigInput {
  enabled?: boolean;
  timeout_seconds?: number | null;
  failure_policy?: 'continue' | 'block' | null;
}

export interface UpdateHookRuntimeConfigResult {
  ok: boolean;
  config: Record<string, unknown>;
}

export interface AgentTeamWorkbench {
  schema: string;
  team: AgentTeam;
  members: Array<AgentTeamMember & {
    summary?: string;
    artifacts?: unknown[];
    work_ledger_deltas?: unknown[];
    t0_refs?: string[];
    enter_href?: string;
  }>;
  events: Array<Record<string, unknown>>;
  summary: {
    member_count: number;
    active_member_count: number;
    event_count: number;
    transcript_truth: string;
  };
  links?: Record<string, string>;
}

export const ccParityApi = {
  listCommands(agentId: string, options: ListCommandsOptions = {}): Promise<CommandIndexEntry[]> {
    const params = new URLSearchParams();
    if (options.surface) params.set('surface', options.surface);
    if (options.includeOptionalPacks) params.set('include_optional_packs', 'true');
    const query = params.toString();
    return get<CommandIndexEntry[]>(`/agents/${agentId}/commands${query ? `?${query}` : ''}`);
  },

  getCommand(agentId: string, commandName: string, options: { includeOptionalPacks?: boolean } = {}): Promise<CommandDefinition> {
    const query = options.includeOptionalPacks ? '?include_optional_packs=true' : '';
    return get<CommandDefinition>(`/agents/${agentId}/commands/${commandName}${query}`);
  },

  executeCommand(agentId: string, commandName: string, input: ExecuteCommandInput = {}): Promise<ExecuteCommandResult> {
    return post<ExecuteCommandResult>(`/agents/${agentId}/commands/${commandName}/execute`, {
      arguments: input.arguments ?? {},
      session_id: input.session_id ?? null,
    });
  },

  startGoal(agentId: string, sessionId: string, input: StartGoalInput): Promise<SessionGoal> {
    return post<SessionGoal>(`/agents/${agentId}/sessions/${sessionId}/goals`, {
      objective: input.objective,
      token_budget: input.token_budget ?? null,
      max_continuation_turns: input.max_continuation_turns ?? null,
      time_budget_seconds: input.time_budget_seconds ?? null,
    });
  },

  continueGoal(agentId: string, sessionId: string, goalId: string): Promise<GoalContinuationResult> {
    return post<GoalContinuationResult>(`/agents/${agentId}/sessions/${sessionId}/goals/${goalId}/continue`);
  },

  startAdvancedPlan(agentId: string, sessionId: string, input: StartAdvancedPlanInput): Promise<AdvancedPlanRun> {
    return post<AdvancedPlanRun>(`/agents/${agentId}/sessions/${sessionId}/advanced-plan`, {
      objective: input.objective,
      context: input.context ?? {},
    });
  },

  getSessionWorkbench(agentId: string, sessionId: string): Promise<SessionWorkbench> {
    return get<SessionWorkbench>(`/agents/${agentId}/sessions/${sessionId}/workbench`);
  },

  exportSessionJson(agentId: string, sessionId: string): Promise<SessionJsonExport> {
    return get<SessionJsonExport>(`/agents/${agentId}/sessions/${sessionId}/export`);
  },

  listHooks(agentId: string): Promise<HookControlPlane> {
    return get<HookControlPlane>(`/agents/${agentId}/hooks`);
  },

  updateHookRuntimeConfig(agentId: string, hookKey: string, input: UpdateHookRuntimeConfigInput): Promise<UpdateHookRuntimeConfigResult> {
    return patch<UpdateHookRuntimeConfigResult>(`/agents/${agentId}/hooks/${encodeURIComponent(hookKey)}`, input);
  },

  createTeam(agentId: string, input: CreateAgentTeamInput): Promise<AgentTeam> {
    return post<AgentTeam>(`/agents/${agentId}/agent-teams`, input);
  },

  listTeams(agentId: string, parentSessionId?: string | null): Promise<AgentTeam[]> {
    const query = parentSessionId ? `?parent_session_id=${encodeURIComponent(parentSessionId)}` : '';
    return get<AgentTeam[]>(`/agents/${agentId}/agent-teams${query}`);
  },

  getTeam(agentId: string, teamId: string): Promise<AgentTeam> {
    return get<AgentTeam>(`/agents/${agentId}/agent-teams/${teamId}`);
  },

  getTeamWorkbench(agentId: string, teamId: string): Promise<AgentTeamWorkbench> {
    return get<AgentTeamWorkbench>(`/agents/${agentId}/agent-teams/${teamId}/workbench`);
  },

  enterTeamMember(agentId: string, teamId: string, memberId: string): Promise<AgentTeamEnterResult> {
    return get<AgentTeamEnterResult>(`/agents/${agentId}/agent-teams/${teamId}/members/${memberId}/enter`);
  },

  closeTeam(agentId: string, teamId: string): Promise<AgentTeamCloseResult> {
    return post<AgentTeamCloseResult>(`/agents/${agentId}/agent-teams/${teamId}/close`);
  },
};
