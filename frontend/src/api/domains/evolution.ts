import { get } from '../core';

export type EvolutionSkillSummary = {
  active: number;
  stale: number;
  archived: number;
  total: number;
};

export type EvolutionSkill = {
  slug: string;
  state: string;
  use_count: number;
  last_used_at: string | null;
  pinned: boolean;
};

export type EvolutionTimelineItem = {
  at: string;
  kind: string;
  title: string;
  detail: string;
};

export type AgentEvolutionView = {
  skill_summary: EvolutionSkillSummary;
  skills: EvolutionSkill[];
  timeline: EvolutionTimelineItem[];
};

export const evolutionApi = {
  get: (agentId: string) => get<AgentEvolutionView>(`/agents/${agentId}/evolution`),
};
