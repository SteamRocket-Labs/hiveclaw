import { del, get, getBlob, patch, post, upload } from '../core';

// Knowledge read model (backend spec §11 / P7) — structured views over the
// agent's memory engine. The Knowledge plane consumes these instead of
// parsing raw file layout; raw Markdown stays behind the Raw advanced view.

export interface DistillerStatus {
  name: string;
  // 'stale' = the pipeline's input keeps arriving but its state file is not
  // keeping up (closure plan A1: exists ≠ fresh).
  state: 'active' | 'stale' | 'never_ran' | string;
  last_run_at: string;
  runtime_status?: string;
  runtime_task_id?: string;
  runtime_phase?: string;
  runtime_mode?: string;
  runtime_result?: string;
  runtime_created_at?: string | null;
  coverage_total?: number;
  coverage_reviewed?: number;
  coverage_complete?: boolean;
  coverage_state?: 'complete' | 'incomplete' | 'legacy_unknown' | string;
}

export type MemoryBusinessState = 'remembered' | 'consolidating' | 'needs_attention' | 'empty';

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
  memoryStatus?: {
    state: MemoryBusinessState;
    availableForRecall: boolean;
    recentMemoryAvailable: boolean;
    longTermMemoryAvailable: boolean;
    pendingConsolidation: boolean;
    pendingItems: number;
    issueCount: number;
  };
  // Live internal debt facts for operator/debug consumers; employee UI uses memoryStatus.
  pipeline: {
    pendingPackages?: number;
    pendingStitchPackages?: number;
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
  score_trace?: Record<string, unknown>;
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
  job_id?: string | null;
  source_sha256: string;
  artifact_hash: string;
  canonical_md_path: string;
  segment_count: number;
  status: string;
  warnings?: string[];
}

export interface PersonalKnowledgeImportFileOptions {
  title?: string;
  agent_searchable?: boolean;
  sensitivity?: string;
}

export interface PersonalKnowledgeUrlImportRequest {
  url: string;
  title?: string;
  agent_searchable?: boolean;
  sensitivity?: string;
}

export interface PersonalKnowledgeJobSummary {
  job_id: string;
  document_id: string;
  stage: string;
  status: string;
  artifact_hash: string;
  error_message: string | null;
  attempt_count: number;
  metadata: Record<string, unknown>;
  created_at: string | null;
  updated_at: string | null;
}

export interface PersonalKnowledgeDocumentPatchRequest {
  agent_searchable?: boolean;
  sensitivity?: string;
  status?: string;
}

export interface PersonalKnowledgeGraphEntity {
  entity_id: string;
  canonical_name: string;
  entity_type: string;
  aliases: string[];
  description: string | null;
  confidence: number;
  source_refs: Record<string, unknown>[];
}

export interface PersonalKnowledgeGraphLink {
  link_id: string;
  from_kind: string;
  from_id: string;
  to_kind: string;
  to_id: string;
  relation: string;
  confidence: number;
  source_refs: Record<string, unknown>[];
}

export interface PersonalKnowledgeGraphAssertion {
  assertion_id: string;
  subject_text: string;
  predicate: string;
  object_text: string;
  confidence: number;
  status: string;
  source_refs: Record<string, unknown>[];
}

export interface PersonalKnowledgeGraphSummary {
  entities: PersonalKnowledgeGraphEntity[];
  links: PersonalKnowledgeGraphLink[];
  assertions: PersonalKnowledgeGraphAssertion[];
}

export interface PersonalKnowledgeGrantSummary {
  grant_id: string;
  resource_type: string;
  resource_id: string;
  document_id: string | null;
  grantee_type: 'user' | 'agent' | string;
  grantee_id: string;
  permission: 'read' | 'search' | 'manage' | string;
  requester_user_id: string | null;
  session_id: string | null;
  purpose: 'interactive_session' | 'autonomous_agent' | 'a2a_delegation' | 'subagent_delegation' | string | null;
  delegation_id: string | null;
  sensitivity_ceiling: 'PL1_public' | 'PL2_pii' | 'PL3_sensitive' | 'PL4_credential' | string;
  binding_key: string;
  expires_at: string | null;
  revoked_at: string | null;
  revoked_by_user_id: string | null;
  active: boolean;
  metadata: Record<string, unknown>;
  created_at: string | null;
}

export interface PersonalKnowledgeGrantRequest {
  resource_type: 'scope' | 'document';
  resource_id?: string;
  document_id?: string;
  grantee_type: 'user' | 'agent';
  grantee_id: string;
  permission?: 'read' | 'search' | 'manage';
  requester_user_id?: string | null;
  session_id?: string | null;
  purpose?: 'interactive_session' | 'autonomous_agent' | 'a2a_delegation' | 'subagent_delegation' | null;
  delegation_id?: string | null;
  sensitivity_ceiling?: 'PL1_public' | 'PL2_pii' | 'PL3_sensitive' | 'PL4_credential';
  expires_at?: string | null;
  metadata?: Record<string, unknown>;
}

export interface PersonalKnowledgeProposalSummary {
  proposal_id: string;
  owner_user_id: string;
  proposed_by_agent_id: string;
  delegated_by_agent_id: string | null;
  delegation_id: string | null;
  title: string;
  content: string;
  content_hash: string;
  baseline_document_id: string | null;
  baseline_revision_id: string | null;
  baseline_content_hash: string | null;
  diff_unified: string;
  target_collection: string;
  source_refs: string[];
  sensitivity: string;
  purpose: string;
  dedupe_key: string;
  idempotency_key: string;
  policy_outcome: 'approve' | 'ask' | 'reject' | string;
  policy_reason_codes: string[];
  status: 'pending' | 'approved' | 'committed' | 'rejected' | string;
  review_reason: string | null;
  document_id: string | null;
  revision_id: string | null;
  rollback_ref: string | null;
  created_at: string | null;
  updated_at: string | null;
}

export interface PersonalKnowledgeProposalDecisionRequest {
  decision: 'approve' | 'reject';
  reason?: string;
}

export interface PersonalKnowledgeRevision {
  id: string;
  version: number;
  change_source: string;
  change_message?: string | null;
  changed_at?: string | null;
  created_at?: string | null;
  rollback_of_revision_id?: string | null;
  content?: Record<string, unknown>;
}

export interface PersonalKnowledgeRollbackResult {
  document_id: string;
  version: number;
  rollback_of_version: number;
  revision_id: string;
  rollback_ref: string;
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
  myPersonalDocuments: () =>
    get<{ documents: PersonalKnowledgeDocumentSummary[] }>('/knowledge/personal/documents'),
  myPersonalIngest: (body: PersonalKnowledgeIngestRequest) =>
    post<PersonalKnowledgeIngestResponse>('/knowledge/personal/documents', body),
  myPersonalSearch: (query: string, limit = 5) => {
    const params = new URLSearchParams({ q: query, limit: String(limit) });
    return get<{ results: PersonalKnowledgeSearchResult[] }>(
      `/knowledge/personal/search?${params.toString()}`,
    );
  },
  myPersonalDocument: (documentId: string) =>
    get<PersonalKnowledgeDocumentDetail>(`/knowledge/personal/documents/${documentId}`),
  myPersonalDocumentSourcePreview: (documentId: string) =>
    getBlob(`/knowledge/personal/documents/${documentId}/source-preview`),
  myPersonalImportFile: (file: File, options: PersonalKnowledgeImportFileOptions = {}) => {
    const fields: Record<string, string> = {};
    if (options.title) fields.title = options.title;
    if (options.agent_searchable !== undefined) fields.agent_searchable = String(options.agent_searchable);
    if (options.sensitivity) fields.sensitivity = options.sensitivity;
    return upload<PersonalKnowledgeIngestResponse>('/knowledge/personal/imports', file, fields);
  },
  myPersonalImportUrl: (body: PersonalKnowledgeUrlImportRequest) =>
    post<PersonalKnowledgeIngestResponse>('/knowledge/personal/import-url', body),
  myPersonalImportJobs: () =>
    get<{ jobs: PersonalKnowledgeJobSummary[] }>('/knowledge/personal/import-jobs'),
  myPersonalRetryImportJob: (jobId: string) =>
    post<PersonalKnowledgeIngestResponse>(`/knowledge/personal/import-jobs/${jobId}/retry`),
  myPersonalPatchDocument: (documentId: string, body: PersonalKnowledgeDocumentPatchRequest) =>
    patch<PersonalKnowledgeDocumentDetail>(`/knowledge/personal/documents/${documentId}`, body),
  myPersonalRebuildDocument: (documentId: string) =>
    post<PersonalKnowledgeIngestResponse>(`/knowledge/personal/documents/${documentId}/rebuild-index`),
  myPersonalGraph: () =>
    get<PersonalKnowledgeGraphSummary>('/knowledge/personal/graph'),
  myPersonalGrants: () =>
    get<{ grants: PersonalKnowledgeGrantSummary[] }>('/knowledge/personal/grants'),
  myPersonalCreateGrant: (body: PersonalKnowledgeGrantRequest) =>
    post<PersonalKnowledgeGrantSummary>('/knowledge/personal/grants', body),
  myPersonalDeleteGrant: (grantId: string) =>
    del<{ revoked: boolean; deleted: false }>(`/knowledge/personal/grants/${grantId}`),
  myPersonalProposals: (status?: string) => {
    const suffix = status ? `?${new URLSearchParams({ status }).toString()}` : '';
    return get<{ proposals: PersonalKnowledgeProposalSummary[] }>(`/knowledge/personal/proposals${suffix}`);
  },
  myPersonalDecideProposal: (proposalId: string, body: PersonalKnowledgeProposalDecisionRequest) =>
    post<PersonalKnowledgeProposalSummary>(`/knowledge/personal/proposals/${proposalId}/decision`, body),
  myPersonalDocumentRevisions: (documentId: string) =>
    get<{ revisions: PersonalKnowledgeRevision[] }>(`/knowledge/personal/documents/${documentId}/revisions`),
  myPersonalRollbackDocument: (documentId: string, targetVersion: number) =>
    post<PersonalKnowledgeRollbackResult>(`/knowledge/personal/documents/${documentId}/rollback`, {
      target_version: targetVersion,
    }),
};
