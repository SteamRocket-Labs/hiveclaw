import { get, post } from '../core';

export interface CommandIndexEntry {
  name: string;
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

export interface ExecuteCommandResult {
  ok: boolean;
  command: string;
  result: unknown;
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

export const ccParityApi = {
  listCommands(agentId: string): Promise<CommandIndexEntry[]> {
    return get<CommandIndexEntry[]>(`/agents/${agentId}/commands`);
  },

  getCommand(agentId: string, commandName: string): Promise<CommandDefinition> {
    return get<CommandDefinition>(`/agents/${agentId}/commands/${commandName}`);
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

  enterTeamMember(agentId: string, teamId: string, memberId: string): Promise<AgentTeamEnterResult> {
    return get<AgentTeamEnterResult>(`/agents/${agentId}/agent-teams/${teamId}/members/${memberId}/enter`);
  },

  closeTeam(agentId: string, teamId: string): Promise<AgentTeamCloseResult> {
    return post<AgentTeamCloseResult>(`/agents/${agentId}/agent-teams/${teamId}/close`);
  },
};
