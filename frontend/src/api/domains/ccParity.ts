import { get, patch, post } from '../core';
import {
  withSessionOperatorQuery,
  type SessionOperatorOptions,
} from './chat';

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
  request_id?: string | null;
  objective: string;
  token_budget?: number | null;
  max_continuation_turns?: number | null;
  time_budget_seconds?: number | null;
  content?: string | null;
  display_content?: string | null;
  file_name?: string;
  attachments?: Array<Record<string, unknown>>;
  parts?: Array<Record<string, unknown>>;
  start_immediately?: boolean;
}

export interface SessionGoal {
  id: string;
  agent_id: string;
  session_id: string;
  objective: string;
  status: string;
  token_budget: number | null;
  tokens_used: number;
  remaining_tokens: number | null;
  time_budget_seconds: number | null;
  time_used_seconds: number;
  remaining_time_seconds: number | null;
  max_continuation_turns: number | null;
  continuation_count: number;
  remaining_continuation_turns: number | null;
  blocked_count: number;
  blocked_reason: string | null;
  completion_summary: string | null;
  controls: {
    can_pause: boolean;
    can_resume: boolean;
    can_stop: boolean;
  };
  created_at: string | null;
  updated_at: string | null;
  completed_at: string | null;
  run?: Record<string, unknown> | null;
  continuation?: Record<string, unknown> | null;
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

export interface CreateAgentTeamInput {
  parent_session_id: string;
  name: string;
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
  last_turn_status?: string | null;
  last_runtime_status?: string | null;
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
  member_count?: number;
  running_member_count?: number;
  close_status?: string | null;
  close_failure?: string | null;
  members: AgentTeamMember[];
  team_create_semantics?: string;
  teammate_creation_tool?: string;
  teammate_creation_args?: {
    team_name?: string;
    name?: string;
    prompt?: string;
  };
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
  close_delivery?: {
    status: string;
    notification_id?: string | null;
  };
}

export interface StartAgentTeamMemberRunInput {
  content: string;
  display_content?: string;
}

export interface MessageAgentTeamMemberInput {
  message: string;
  display_content?: string;
  interrupt?: boolean;
}

export interface SessionWorkbench {
  schema: string;
  audience?: 'user' | 'operator';
  operator_details_available?: boolean;
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
  context_usage?: SessionContextUsage;
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
  goals: SessionGoal[];
  teams: AgentTeam[];
  session_index?: Record<string, unknown> | null;
  links?: Record<string, string>;
}

export interface SessionContextUsage {
  schema: string;
  session_id: string;
  agent_id: string;
  model_window_tokens?: number | null;
  used_tokens?: number | null;
  free_space_tokens?: number | null;
  categories?: unknown[];
  context_candidates?: unknown[];
  selected_contexts?: unknown[];
  suppressed_contexts?: unknown[];
  dynamic_context_sections?: unknown[];
  tool_result_ledger?: unknown[];
  cache_decision_ledger?: unknown[];
  agent_cycle_decision_ledger?: unknown[];
  activation_candidates?: unknown[];
  context_artifacts?: unknown[];
  active_tool_names?: unknown[];
  deferred_tool_names?: unknown[];
  loaded_skills?: unknown[];
  prompt_manifest_schema?: string | null;
  runtime_assembly_state_schema?: string | null;
  counts?: Record<string, number>;
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
  registrations: Array<{
    event: string;
    handler_name: string;
    key?: string | null;
    failure_mode?: 'required' | 'advisory';
    runtime_config?: {
      key?: string | null;
      enabled?: boolean;
      timeout_seconds?: number | null;
      failure_policy?: 'inherit' | 'required' | 'advisory' | 'block' | 'continue';
      effective_failure_mode?: 'required' | 'advisory';
      migration_preview?: Record<string, unknown> | null;
    };
  }>;
  recent_receipts?: Array<{
    id: string;
    hook_key: string;
    event: string;
    status: string;
    failure_mode: 'required' | 'advisory';
    retryable: boolean;
    error?: string | null;
    created_at?: string | null;
  }>;
  failure_mode_contract?: Record<string, string>;
}

export interface UpdateHookRuntimeConfigInput {
  enabled?: boolean;
  timeout_seconds?: number | null;
  failure_policy?: 'inherit' | 'required' | 'advisory' | 'continue' | 'block' | null;
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
      request_id: input.request_id ?? null,
      objective: input.objective,
      token_budget: input.token_budget ?? null,
      max_continuation_turns: input.max_continuation_turns ?? null,
      time_budget_seconds: input.time_budget_seconds ?? null,
      content: input.content ?? null,
      display_content: input.display_content ?? null,
      file_name: input.file_name ?? '',
      attachments: input.attachments ?? [],
      parts: input.parts ?? [],
      start_immediately: input.start_immediately ?? false,
    });
  },

  transitionGoal(
    agentId: string,
    sessionId: string,
    goalId: string,
    action: 'pause' | 'resume' | 'stop',
  ): Promise<SessionGoal> {
    return post<SessionGoal>(`/agents/${agentId}/sessions/${sessionId}/goals/${goalId}/transition`, { action });
  },

  startAdvancedPlan(agentId: string, sessionId: string, input: StartAdvancedPlanInput): Promise<AdvancedPlanRun> {
    return post<AdvancedPlanRun>(`/agents/${agentId}/sessions/${sessionId}/advanced-plan`, {
      objective: input.objective,
      context: input.context ?? {},
    });
  },

  getSessionWorkbench(
    agentId: string,
    sessionId: string,
    options?: SessionOperatorOptions,
  ): Promise<SessionWorkbench> {
    return get<SessionWorkbench>(
      withSessionOperatorQuery(`/agents/${agentId}/sessions/${sessionId}/workbench`, options),
    );
  },

  getSessionContextUsage(
    agentId: string,
    sessionId: string,
    options?: SessionOperatorOptions,
  ): Promise<SessionContextUsage> {
    return get<SessionContextUsage>(
      withSessionOperatorQuery(`/agents/${agentId}/sessions/${sessionId}/context-usage`, options),
    );
  },

  exportSessionJson(
    agentId: string,
    sessionId: string,
    options?: SessionOperatorOptions,
  ): Promise<SessionJsonExport> {
    return get<SessionJsonExport>(
      withSessionOperatorQuery(`/agents/${agentId}/sessions/${sessionId}/export`, options),
    );
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

  startTeamMemberRun(
    agentId: string,
    teamId: string,
    memberId: string,
    input: StartAgentTeamMemberRunInput,
  ): Promise<AgentTeamMember> {
    return post<AgentTeamMember>(`/agents/${agentId}/agent-teams/${teamId}/members/${memberId}/runs`, {
      content: input.content,
      display_content: input.display_content ?? '',
    });
  },

  messageTeamMember(
    agentId: string,
    teamId: string,
    memberId: string,
    input: MessageAgentTeamMemberInput,
  ): Promise<AgentTeamMember> {
    return post<AgentTeamMember>(`/agents/${agentId}/agent-teams/${teamId}/members/${memberId}/messages`, {
      message: input.message,
      display_content: input.display_content ?? '',
      interrupt: input.interrupt ?? false,
    });
  },

  closeTeam(agentId: string, teamId: string): Promise<AgentTeamCloseResult> {
    return post<AgentTeamCloseResult>(`/agents/${agentId}/agent-teams/${teamId}/close`);
  },
};
