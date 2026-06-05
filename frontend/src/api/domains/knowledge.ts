import { get } from '../core';

// Knowledge read model (backend spec §11 / P7) — structured views over the
// agent's memory engine. The Knowledge plane consumes these instead of
// parsing raw file layout; raw Markdown stays behind the Raw advanced view.

export interface DistillerStatus {
  name: string;
  state: 'active' | 'never_ran' | string;
  last_run_at: string;
}

export interface KnowledgeOverview {
  identity: {
    sections: number;
    frozenSections: number;
    pendingSoulCandidates: number;
    lastUpdated?: string;
  };
  memory: {
    active: number;
    stale: number;
    superseded: number;
    archived: number;
    sensitiveSuppressed: number;
  };
  distillers: {
    extractor: DistillerStatus;
    heartbeat: DistillerStatus;
    dream: DistillerStatus;
    skillDistiller: DistillerStatus;
  };
  linkedCapabilities: {
    skillsReferenced: number;
    workflowsReferenced: number;
    mcpToolsReferenced: number;
    skillCandidates: number;
  };
}

export interface KnowledgePageSummary {
  id: string;
  kind: 'wiki' | 'scene' | string;
  slug: string;
  title: string;
  tags: string;
  status: string;
  updatedAt: string;
}

export interface KnowledgePageLink {
  page_id: string;
  title: string;
  rel_type: string;
  exists: boolean;
  status: string;
}

export interface KnowledgePageDetail {
  id: string;
  kind: string;
  slug: string;
  frontmatter: Record<string, string>;
  markdown: string;
  updatedAt: string;
  links: { outgoing: KnowledgePageLink[]; incoming: KnowledgePageLink[] };
}

export interface KnowledgeEntry {
  id: string;
  file: string;
  category: string;
  content: string;
  preview: string;
  timestamp: string;
  heat: number;
  recallCount: number;
  lastRecalledAt: string;
  sensitivity: string;
  status: string;
  containerCandidate: string;
  promotedTo: string;
  load: string;
}

export interface KnowledgeEvent {
  at: string;
  kind: string;
  outcome: string;
  summary: string;
  detail: Record<string, unknown>;
}

export interface KnowledgeCandidateItem {
  entry_id: string;
  content: string;
  timestamp: string;
  filename: string;
  source: string;
}

export interface KnowledgeCandidates {
  skillCandidates: KnowledgeCandidateItem[];
  workflowCandidates: KnowledgeCandidateItem[];
  soulCandidates: { candidateId: string; reason: string; at: string }[];
  heldCurations: { at: string; stage: string; reason: string; detail: Record<string, unknown> }[];
}

export const knowledgeApi = {
  overview: (agentId: string) => get<KnowledgeOverview>(`/agents/${agentId}/knowledge/overview`),
  pages: (agentId: string) => get<{ pages: KnowledgePageSummary[] }>(`/agents/${agentId}/knowledge/pages`),
  page: (agentId: string, pageId: string) =>
    get<KnowledgePageDetail>(`/agents/${agentId}/knowledge/pages/${pageId}`),
  entries: (agentId: string) => get<{ entries: KnowledgeEntry[] }>(`/agents/${agentId}/knowledge/entries`),
  events: (agentId: string) => get<{ events: KnowledgeEvent[] }>(`/agents/${agentId}/knowledge/events`),
  candidates: (agentId: string) => get<KnowledgeCandidates>(`/agents/${agentId}/knowledge/candidates`),
};
