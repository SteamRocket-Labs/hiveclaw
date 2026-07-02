import { get, post } from '../core';

export type EvolutionSkillEcosystemSummary = {
  active: number;
  stale: number;
  archived: number;
  evolvable: number;
  total: number;
  by_origin: Record<string, number>;
};

export type EvolutionSkillEcosystemItem = {
  skill_name: string;
  skill_origin: string;
  evolvable: boolean;
  state: string;
  use_count: number;
  last_used_at: string | null;
  target_path?: string | null;
  active_version_hash?: string | null;
  last_candidate_id?: string | null;
};

export type EvolutionManifest = {
  candidate_id?: string;
  job_id?: string;
  status?: string;
  reason?: string;
  skill_name?: string;
  skill_origin?: string;
  target_path?: string;
  manifest_path?: string;
  source_refs?: string[];
  [key: string]: unknown;
};

export type EvolutionTimelineItem = {
  id: string;
  lane: string;
  stage: string;
  status: string;
  title: string;
  path?: string | null;
  source_refs?: string[];
  [key: string]: unknown;
};

export type AgentEvolutionView = {
  schema: 'agent_evolution_view.v2';
  path_contract: Record<string, string>;
  lanes: Record<string, EvolutionTimelineItem[]>;
  timeline: EvolutionTimelineItem[];
  memory_learning: {
    pending_t3_jobs: EvolutionManifest[];
    t3_targets: Record<string, { path: string; line_count: number }>;
  };
  soul: {
    active_path: string;
    pending_candidates: EvolutionManifest[];
  };
  skill_ecosystem: {
    summary: EvolutionSkillEcosystemSummary;
    skills: EvolutionSkillEcosystemItem[];
  };
  skill_tuning: {
    candidates: EvolutionManifest[];
  };
  legacy_audit: {
    detected_legacy_files: string[];
  };
};

export type SoulApprovalResult = {
  status: 'committed' | 'refused' | 'rejected' | string;
  candidate_id?: string;
  reason?: string;
  rollback_ref?: string;
};

export const evolutionApi = {
  get: (agentId: string) => get<AgentEvolutionView>(`/agents/${agentId}/evolution`),
  approveSoulCandidate: (agentId: string, candidateId: string) =>
    post<SoulApprovalResult>(`/agents/${agentId}/evolution/soul-candidates/${candidateId}/approve`, {}),
  rejectSoulCandidate: (agentId: string, candidateId: string, reason: string) =>
    post<SoulApprovalResult>(`/agents/${agentId}/evolution/soul-candidates/${candidateId}/reject`, { reason }),
};
