import { get, post } from '../core';

// Knowledge read model (backend spec §11 / P7) — structured views over the
// agent's memory engine. The Knowledge plane consumes these instead of
// parsing raw file layout; raw Markdown stays behind the Raw advanced view.

export interface DistillerStatus {
  name: string;
  // 'stale' = the pipeline's input keeps arriving but its state file is not
  // keeping up (closure plan A1: exists ≠ fresh).
  state: 'active' | 'stale' | 'never_ran' | string;
  last_run_at: string;
}

export interface KnowledgeOverview {
  identity: {
    sections: number;
    frozenSections: number;
    pendingSoulCandidates: number;
    lastUpdated?: string;
  };
  // Two-plane world (memory spec v1.2): profile plane converges
  // (self/profiles), knowledge plane networks (knowledge/milestones).
  planes: {
    self: { entries: number; failureModes: { active: number; mitigating: number; resolved: number } };
    profiles: { entries: number };
    knowledge: { pages: number };
    milestones: { pages: number };
    explicit: { active: number };
  };
  // Consolidation-debt summary — empty object until first assessed.
  pipeline: {
    pendingPackages?: number;
    heldJobs?: number;
    stalled?: boolean;
    lastAssessedAt?: string;
  };
  // Growth-report freshness — empty object until first generated.
  growth: {
    generatedAt?: string;
    reportPath?: string;
  };
  distillers: {
    t2_pipeline: DistillerStatus;
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

// J2 growth report metrics as persisted in growth_metrics_history.jsonl.
export interface GrowthFailureMode {
  id: string;
  title: string;
  status: string;
  recurred: number;
  avoided: number;
  avoidance_rate: number | null;
}

export interface GrowthMetrics {
  generated_at?: string;
  failure_modes?: GrowthFailureMode[];
  rework?: { labeled_packages?: number; rework_packages?: number; recent_rate?: number | null; previous_rate?: number | null };
  reuse?: { knowledge_pages?: number; total_citations?: number; top_cited?: { ref: string; count: number }[] };
  feedback_polarity?: { recent?: Record<string, number>; previous?: Record<string, number> };
  task_volume?: { recent_invocations?: number; window_days?: number };
  evolution?: { promotions?: number; rollbacks?: number };
}

export interface MemoryObservability {
  debt: Record<string, unknown>;
  debt_history: Record<string, unknown>[];
  label_axes: Record<string, Record<string, number>>;
  growth: { generated_at?: string; metrics?: GrowthMetrics; report_path?: string };
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

export interface PersonalKnowledgeDocumentSummary {
  document_id: string;
  title: string;
  source_kind: string;
  source_uri: string | null;
  source_sha256: string;
  source_ref: string;
  canonical_md_path: string;
  status: string;
  sensitivity: string;
  agent_searchable: boolean;
  segment_count: number;
  created_at: string | null;
  updated_at: string | null;
  metadata: Record<string, unknown>;
}

export interface PersonalKnowledgeSegment {
  segment_id: string;
  position: number;
  heading_path: string[];
  content: string;
  token_count: number;
}

export interface PersonalKnowledgeDocumentDetail extends PersonalKnowledgeDocumentSummary {
  segments: PersonalKnowledgeSegment[];
}

export interface PersonalKnowledgeSearchResult {
  document_id: string;
  segment_id: string;
  title: string;
  snippet: string;
  source_ref: string;
  score: number;
  heading_path: string[];
  sensitivity: string;
  metadata: Record<string, unknown>;
}

export interface PersonalKnowledgeIngestRequest {
  title: string;
  markdown: string;
  source_kind?: string;
  source_uri?: string | null;
  agent_searchable?: boolean;
  sensitivity?: string;
}

export interface PersonalKnowledgeIngestResponse {
  document_id: string;
  source_sha256: string;
  artifact_hash: string;
  canonical_md_path: string;
  segment_count: number;
  status: string;
}

export const knowledgeApi = {
  overview: (agentId: string) => get<KnowledgeOverview>(`/agents/${agentId}/knowledge/overview`),
  pages: (agentId: string) => get<{ pages: KnowledgePageSummary[] }>(`/agents/${agentId}/knowledge/pages`),
  page: (agentId: string, pageId: string) =>
    get<KnowledgePageDetail>(`/agents/${agentId}/knowledge/pages/${pageId}`),
  entries: (agentId: string) => get<{ entries: KnowledgeEntry[] }>(`/agents/${agentId}/knowledge/entries`),
  events: (agentId: string) => get<{ events: KnowledgeEvent[] }>(`/agents/${agentId}/knowledge/events`),
  candidates: (agentId: string) => get<KnowledgeCandidates>(`/agents/${agentId}/knowledge/candidates`),
  observability: (agentId: string) => get<MemoryObservability>(`/agents/${agentId}/knowledge/observability`),
  personalDocuments: (agentId: string) =>
    get<{ documents: PersonalKnowledgeDocumentSummary[] }>(`/agents/${agentId}/knowledge/personal/documents`),
  personalIngest: (agentId: string, body: PersonalKnowledgeIngestRequest) =>
    post<PersonalKnowledgeIngestResponse>(`/agents/${agentId}/knowledge/personal/documents`, body),
  personalSearch: (agentId: string, query: string, limit = 5) => {
    const params = new URLSearchParams({ q: query, limit: String(limit) });
    return get<{ results: PersonalKnowledgeSearchResult[] }>(
      `/agents/${agentId}/knowledge/personal/search?${params.toString()}`,
    );
  },
  personalDocument: (agentId: string, documentId: string) =>
    get<PersonalKnowledgeDocumentDetail>(`/agents/${agentId}/knowledge/personal/documents/${documentId}`),
};
