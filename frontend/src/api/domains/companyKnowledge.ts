import { get, post } from '../core';

export type CompanyKnowledgeArea =
  | 'general'
  | 'policies'
  | 'team_notes'
  | 'playbooks'
  | 'operations';

export type CompanyKnowledgeSensitivity =
  | 'company'
  | 'personal_data'
  | 'restricted'
  | 'credential';

export type CompanyKnowledgeCapability =
  | 'find_and_read'
  | 'propose_updates'
  | 'review_and_publish'
  | 'manage_lifecycle'
  | 'use_company_model';

export interface CompanyLibraryDocument {
  publicationKey: string;
  documentKey: string;
  title: string;
  area: string;
  sensitivity: CompanyKnowledgeSensitivity;
  version: number;
  validFrom: string;
  validUntil: string | null;
}

export interface CompanyLibrarySearchHit extends CompanyLibraryDocument {
  snippet: string;
}

export interface CompanyLibraryDocumentDetail {
  publicationKey: string;
  documentKey: string;
  title: string;
  area: string;
  sensitivity: CompanyKnowledgeSensitivity;
  version: number;
  content: string;
  truncated: boolean;
}

export interface CompanyKnowledgeIntake {
  intakeKey: string;
  reviewKey: string | null;
  kind: 'personal' | 'legacy' | 'other';
  title: string;
  sourceLabel: string;
  area: string;
  sensitivity: CompanyKnowledgeSensitivity;
  status: string;
  recovery: 'automatic' | 'manual' | 'none';
  attemptCount: number;
  reviewStatus: string | null;
  createdAt: string;
  updatedAt: string;
}

export interface LegacyKnowledgeCandidate {
  sourcePath: string;
  sourceHash: string;
  label: string;
  sizeBytes: number;
}

export interface CompanyKnowledgeReview {
  reviewKey: string;
  title: string;
  status: string;
  kind: 'personal' | 'legacy' | 'knowledge' | 'ontology' | 'other';
  area: string;
  sensitivity: CompanyKnowledgeSensitivity;
  risk: string;
  reason: string;
  createdBy: 'digital_employee' | 'company_member';
  stateVersion: number;
  needsMaterialization: boolean;
  materialized: boolean;
  updatedAt: string;
}

export interface CompanyKnowledgeReviewWorkspace extends CompanyKnowledgeReview {
  expectedCandidateHash: string;
  evidenceRefs: string[];
  candidateTitle: string;
  candidateMarkdown: string;
}

export interface CompanyKnowledgeAccessRule {
  permissionKey: string;
  audience: string;
  resource: string;
  capabilities: CompanyKnowledgeCapability[];
  effect: 'allow' | 'deny';
  sensitivity: CompanyKnowledgeSensitivity;
  active: boolean;
  expiresAt: string | null;
}

export interface CompanyKnowledgeAudience {
  kind: 'role' | 'user' | 'agent';
  key: string;
  label: string;
}

export interface CompanyKnowledgePublicationLifecycle {
  publicationKey: string;
  documentKey: string;
  title: string;
  status: 'active' | 'retired';
  version: number;
  area: string;
  sensitivity: CompanyKnowledgeSensitivity;
  validFrom: string;
  validUntil: string | null;
  availableAction: 'retire' | 'restore';
}

export interface CompanyOntologyStatus {
  engineStatus: 'available' | 'degraded' | 'unavailable';
  installedPacks: Array<{ name: string; version: string; status: string }>;
  releases: Array<{ area: string; version: number; status: string }>;
}

interface RawDocument {
  publication_id: string;
  document_id: string;
  title: string;
  namespace: string;
  sensitivity: string;
  version: number;
  valid_from: string;
  valid_until: string | null;
}

const AREA_NAMESPACES: Record<CompanyKnowledgeArea, string> = {
  general: 'company/general',
  policies: 'company/policies',
  team_notes: 'company/team-notes',
  playbooks: 'company/playbooks',
  operations: 'company/operations',
};

const CAPABILITY_ACTIONS: Record<CompanyKnowledgeCapability, string[]> = {
  find_and_read: ['discover', 'search', 'read', 'cite'],
  propose_updates: ['propose'],
  review_and_publish: ['review', 'approve', 'publish'],
  manage_lifecycle: ['retire', 'restore', 'manage_permissions'],
  use_company_model: ['query', 'simulate', 'execute_action'],
};

function requestRef(prefix: string): string {
  const suffix =
    typeof globalThis.crypto?.randomUUID === 'function'
      ? globalThis.crypto.randomUUID()
      : `${Date.now()}-${Math.random().toString(16).slice(2)}`;
  return `${prefix}:${suffix}`;
}

function areaFromNamespace(value: unknown): string {
  const raw = String(value ?? '').trim().replace(/^company\//, '');
  if (!raw) return 'general';
  const known = Object.entries(AREA_NAMESPACES).find(([, namespace]) => namespace === `company/${raw}`);
  return known?.[0] ?? raw.split('/').at(-1)?.replaceAll('-', '_') ?? 'general';
}

function sensitivityFromRaw(value: unknown): CompanyKnowledgeSensitivity {
  if (value === 'PL2_pii') return 'personal_data';
  if (value === 'PL3_sensitive') return 'restricted';
  if (value === 'PL4_credential') return 'credential';
  return 'company';
}

function sensitivityToRaw(value: CompanyKnowledgeSensitivity): string {
  if (value === 'personal_data') return 'PL2_pii';
  if (value === 'restricted') return 'PL3_sensitive';
  if (value === 'credential') return 'PL4_credential';
  return 'PL1_public';
}

function safeSourceLabel(value: unknown): string {
  const raw = String(value ?? '').trim().replaceAll('\\', '/');
  return raw.split('/').filter(Boolean).at(-1) || 'Imported knowledge';
}

function libraryDocument(raw: RawDocument): CompanyLibraryDocument {
  return {
    publicationKey: String(raw.publication_id),
    documentKey: String(raw.document_id),
    title: String(raw.title || 'Untitled knowledge'),
    area: areaFromNamespace(raw.namespace),
    sensitivity: sensitivityFromRaw(raw.sensitivity),
    version: Number(raw.version || 1),
    validFrom: String(raw.valid_from || ''),
    validUntil: raw.valid_until ? String(raw.valid_until) : null,
  };
}

function reviewKind(value: unknown): CompanyKnowledgeReview['kind'] {
  if (value === 'personal_promotion') return 'personal';
  if (value === 'legacy_import') return 'legacy';
  if (value === 'knowledge' || value === 'ontology') return value;
  return 'other';
}

function intakeKind(value: unknown): CompanyKnowledgeIntake['kind'] {
  if (value === 'personal_promotion') return 'personal';
  if (value === 'legacy_import') return 'legacy';
  return 'other';
}

function currentTenantKey(): string {
  const tenantKey = localStorage.getItem('current_tenant_id')?.trim();
  if (!tenantKey) throw new Error('company_knowledge_tenant_required');
  return tenantKey;
}

function rawCandidateMarkdown(patch: Record<string, unknown>): string {
  for (const key of ['markdown', 'replacement_markdown', 'candidate_markdown']) {
    const value = patch[key];
    if (typeof value === 'string') return value;
  }
  return '';
}

export const companyKnowledgeApi = {
  async listLibrary(): Promise<{ documents: CompanyLibraryDocument[] }> {
    const raw = await get<{ documents?: RawDocument[] }>('/knowledge/company/documents?limit=200');
    return { documents: (raw.documents ?? []).map(libraryDocument) };
  },

  async searchLibrary(query: string): Promise<{ results: CompanyLibrarySearchHit[] }> {
    const raw = await post<{ results?: Array<RawDocument & { snippet?: string }> }>('/knowledge/company/search', {
      query: query.trim(),
      filters: {},
      limit: 50,
    });
    return {
      results: (raw.results ?? []).map((item) => ({
        ...libraryDocument({
          ...item,
          valid_from: item.valid_from || '',
          valid_until: item.valid_until ?? null,
        }),
        snippet: String(item.snippet || ''),
      })),
    };
  },

  async readLibrary(documentKey: string, publicationKey: string): Promise<CompanyLibraryDocumentDetail | null> {
    const query = new URLSearchParams({ publication_id: publicationKey, max_chars: '100000' });
    const raw = await get<Record<string, unknown>>(
      `/knowledge/company/documents/${encodeURIComponent(documentKey)}?${query.toString()}`,
    );
    if (raw.status !== 'ok') return null;
    const segments = Array.isArray(raw.segments) ? raw.segments : [];
    return {
      publicationKey: String(raw.publication_id || publicationKey),
      documentKey: String(raw.document_id || documentKey),
      title: String(raw.title || 'Untitled knowledge'),
      area: areaFromNamespace(raw.namespace),
      sensitivity: sensitivityFromRaw(raw.sensitivity),
      version: Number(raw.version || 1),
      content: segments
        .map((segment) => String((segment as Record<string, unknown>).content || '').trim())
        .filter(Boolean)
        .join('\n\n'),
      truncated: Boolean(raw.truncated),
    };
  },

  async listIntakes(): Promise<CompanyKnowledgeIntake[]> {
    const raw = await get<{ intakes?: Array<Record<string, unknown>> }>(
      '/knowledge/company/promotion-intakes?limit=200',
    );
    return (raw.intakes ?? []).map((item) => ({
      intakeKey: String(item.intake_id || ''),
      reviewKey: item.proposal_id ? String(item.proposal_id) : null,
      kind: intakeKind(item.kind),
      title: String(item.title || 'Untitled knowledge'),
      sourceLabel: safeSourceLabel(item.source_label),
      area: areaFromNamespace(item.namespace),
      sensitivity: sensitivityFromRaw(item.sensitivity),
      status: String(item.status || 'held'),
      recovery:
        item.recovery === 'automatic' || item.recovery === 'none'
          ? item.recovery
          : 'manual',
      attemptCount: Number(item.attempt_count || 0),
      reviewStatus: item.proposal_status ? String(item.proposal_status) : null,
      createdAt: String(item.created_at || ''),
      updatedAt: String(item.updated_at || ''),
    }));
  },

  async submitPersonal(input: {
    documentKey: string;
    area: CompanyKnowledgeArea;
    purpose: string;
    title?: string;
  }): Promise<void> {
    const ref = requestRef('company-personal');
    await post('/knowledge/company/promotion-intakes/personal', {
      document_id: input.documentKey,
      proposed_namespace: AREA_NAMESPACES[input.area],
      purpose: input.purpose.trim(),
      risk_level: 'normal',
      title: input.title?.trim() || null,
      attest_scope_change: true,
      idempotency_key: ref,
      trace_id: ref,
    });
  },

  async listLegacyCandidates(): Promise<{
    candidates: LegacyKnowledgeCandidate[];
    excludedSymlinkCount: number;
  }> {
    const raw = await get<{
      candidates?: Array<{ relative_path: string; size_bytes: number; sha256: string }>;
      excluded_symlink_count?: number;
    }>('/knowledge/company/promotion-intakes/legacy-candidates');
    return {
      candidates: (raw.candidates ?? []).map((candidate) => ({
        sourcePath: candidate.relative_path,
        sourceHash: candidate.sha256,
        label: safeSourceLabel(candidate.relative_path),
        sizeBytes: Number(candidate.size_bytes || 0),
      })),
      excludedSymlinkCount: Number(raw.excluded_symlink_count || 0),
    };
  },

  async submitLegacy(input: {
    candidate: LegacyKnowledgeCandidate;
    area: CompanyKnowledgeArea;
    sensitivity: CompanyKnowledgeSensitivity;
    purpose: string;
    title?: string;
  }): Promise<void> {
    const ref = requestRef('company-legacy');
    await post('/knowledge/company/promotion-intakes/legacy', {
      relative_path: input.candidate.sourcePath,
      expected_sha256: input.candidate.sourceHash,
      proposed_namespace: AREA_NAMESPACES[input.area],
      proposed_sensitivity: sensitivityToRaw(input.sensitivity),
      purpose: input.purpose.trim(),
      risk_level: input.sensitivity === 'restricted' ? 'high' : 'normal',
      title: input.title?.trim() || null,
      attest_scope_change: true,
      idempotency_key: ref,
      trace_id: ref,
    });
  },

  async retryIntake(intakeKey: string): Promise<void> {
    await post(`/knowledge/company/promotion-intakes/${encodeURIComponent(intakeKey)}/retry`, {
      trace_id: requestRef('company-intake-retry'),
    });
  },

  async listReviews(): Promise<CompanyKnowledgeReview[]> {
    const raw = await get<{ proposals?: Array<Record<string, unknown>> }>(
      '/knowledge/company/proposals?limit=200',
    );
    return (raw.proposals ?? []).map((item) => ({
      reviewKey: String(item.proposal_id || ''),
      title: String(item.title || 'Untitled proposal'),
      status: String(item.status || 'submitted'),
      kind: reviewKind(item.kind),
      area: areaFromNamespace(item.namespace),
      sensitivity: sensitivityFromRaw(item.sensitivity),
      risk: String(item.risk_level || 'normal'),
      reason: String(item.reason || ''),
      createdBy: item.created_by === 'digital_employee' ? 'digital_employee' : 'company_member',
      stateVersion: Number(item.state_version || 1),
      needsMaterialization: Boolean(item.materialization_required),
      materialized: Boolean(item.materialized),
      updatedAt: String(item.updated_at || ''),
    }));
  },

  async getReviewWorkspace(
    review: CompanyKnowledgeReview,
    intakes: CompanyKnowledgeIntake[],
  ): Promise<CompanyKnowledgeReviewWorkspace> {
    const raw = await get<Record<string, unknown>>(
      `/knowledge/company/proposals/${encodeURIComponent(review.reviewKey)}`,
    );
    const linkedIntake = intakes.find((item) => item.reviewKey === review.reviewKey);
    let candidate: Record<string, unknown> = {};
    if (linkedIntake) {
      candidate = await get<Record<string, unknown>>(
        `/knowledge/company/promotion-intakes/${encodeURIComponent(linkedIntake.intakeKey)}/candidate`,
      );
    }
    const patch =
      raw.proposed_patch_json && typeof raw.proposed_patch_json === 'object'
        ? (raw.proposed_patch_json as Record<string, unknown>)
        : {};
    return {
      ...review,
      stateVersion: Number(raw.state_version || review.stateVersion),
      expectedCandidateHash: String(raw.proposed_content_hash || ''),
      evidenceRefs: Array.isArray(raw.source_refs_json)
        ? raw.source_refs_json.map((value) => String(value))
        : [],
      candidateTitle: String(candidate.title || review.title),
      candidateMarkdown: String(candidate.markdown || rawCandidateMarkdown(patch)),
      materialized: Boolean(raw.materialized_document_id || review.materialized),
    };
  },

  async materializeReview(
    workspace: CompanyKnowledgeReviewWorkspace,
    input: { title: string; markdown: string },
  ): Promise<void> {
    const ref = requestRef('company-review-materialize');
    await post(`/knowledge/company/proposals/${encodeURIComponent(workspace.reviewKey)}/materialize`, {
      expected_state_version: workspace.stateVersion,
      expected_proposed_content_hash: workspace.expectedCandidateHash,
      title: input.title.trim(),
      markdown: input.markdown,
      attest_candidate_applied: true,
      idempotency_key: ref,
      trace_id: ref,
    });
  },

  async decideReview(
    workspace: CompanyKnowledgeReviewWorkspace,
    decision: 'approve' | 'reject' | 'request_changes',
    reason: string,
  ): Promise<void> {
    await post(`/knowledge/company/proposals/${encodeURIComponent(workspace.reviewKey)}/review`, {
      expected_state_version: workspace.stateVersion,
      decision,
      reason: reason.trim(),
      evidence_refs: workspace.evidenceRefs,
      trace_id: requestRef('company-review-decision'),
    });
  },

  async publishReview(workspace: CompanyKnowledgeReviewWorkspace): Promise<void> {
    await post(`/knowledge/company/proposals/${encodeURIComponent(workspace.reviewKey)}/publish`, {
      expected_state_version: workspace.stateVersion,
      valid_from: new Date().toISOString(),
      valid_until: null,
      trace_id: requestRef('company-review-publish'),
    });
  },

  async listAccessRules(): Promise<CompanyKnowledgeAccessRule[]> {
    const raw = await get<{ permissions?: Array<Record<string, unknown>> }>('/knowledge/company/permissions');
    return (raw.permissions ?? []).map((item) => {
      const principal =
        item.principal && typeof item.principal === 'object'
          ? (item.principal as Record<string, unknown>)
          : {};
      const resource =
        item.resource && typeof item.resource === 'object'
          ? (item.resource as Record<string, unknown>)
          : {};
      return {
        permissionKey: String(item.permission_id || ''),
        audience: String(principal.label || 'Unavailable audience'),
        resource: String(resource.label || 'Company Knowledge'),
        capabilities: Array.isArray(item.capabilities)
          ? item.capabilities.filter((value): value is CompanyKnowledgeCapability =>
              Object.hasOwn(CAPABILITY_ACTIONS, String(value)),
            )
          : [],
        effect: item.effect === 'deny' ? 'deny' : 'allow',
        sensitivity: sensitivityFromRaw(item.sensitivity_ceiling),
        active: Boolean(item.active),
        expiresAt: item.expires_at ? String(item.expires_at) : null,
      };
    });
  },

  async grantAccess(input: {
    audience: CompanyKnowledgeAudience;
    capabilities: CompanyKnowledgeCapability[];
    sensitivity: CompanyKnowledgeSensitivity;
    effect: 'allow' | 'deny';
  }): Promise<void> {
    const ref = requestRef('company-access');
    const actions = Array.from(
      new Set(input.capabilities.flatMap((capability) => CAPABILITY_ACTIONS[capability])),
    ).sort();
    await post('/knowledge/company/permissions', {
      principal_type: input.audience.kind,
      principal_id: input.audience.kind === 'role' ? null : input.audience.key,
      principal_key: input.audience.kind === 'role' ? input.audience.key : null,
      resource_type: 'company_knowledge_scope',
      resource_id: currentTenantKey(),
      resource_key: null,
      actions,
      effect: input.effect,
      sensitivity_ceiling: sensitivityToRaw(input.sensitivity),
      purposes: ['interactive_session'],
      expires_at: null,
      idempotency_key: ref,
      trace_id: ref,
    });
  },

  async revokeAccess(permissionKey: string, reason: string): Promise<void> {
    await post(`/knowledge/company/permissions/${encodeURIComponent(permissionKey)}/revoke`, {
      reason: reason.trim(),
      trace_id: requestRef('company-access-revoke'),
    });
  },

  async listPublicationLifecycle(): Promise<CompanyKnowledgePublicationLifecycle[]> {
    const raw = await get<{ publications?: Array<Record<string, unknown>> }>(
      '/knowledge/company/publications?limit=200',
    );
    return (raw.publications ?? []).map((item) => ({
      publicationKey: String(item.publication_id || ''),
      documentKey: String(item.document_id || ''),
      title: String(item.title || 'Untitled knowledge'),
      status: item.status === 'retired' ? 'retired' : 'active',
      version: Number(item.version || 1),
      area: areaFromNamespace(item.namespace),
      sensitivity: sensitivityFromRaw(item.sensitivity),
      validFrom: String(item.valid_from || ''),
      validUntil: item.valid_until ? String(item.valid_until) : null,
      availableAction: item.available_action === 'restore' ? 'restore' : 'retire',
    }));
  },

  async retirePublication(publicationKey: string, reason: string): Promise<void> {
    await post(`/knowledge/company/publications/${encodeURIComponent(publicationKey)}/retire`, {
      reason: reason.trim(),
      trace_id: requestRef('company-publication-retire'),
    });
  },

  async restorePublication(publicationKey: string, reason: string): Promise<void> {
    await post(`/knowledge/company/publications/${encodeURIComponent(publicationKey)}/restore`, {
      reason: reason.trim(),
      valid_from: new Date().toISOString(),
      trace_id: requestRef('company-publication-restore'),
    });
  },

  async getOntologyStatus(): Promise<CompanyOntologyStatus> {
    const [installations, releases, capabilities] = await Promise.all([
      get<{ installations?: Array<Record<string, unknown>> }>(
        '/knowledge/company/ontology/package-installations',
      ),
      get<{ releases?: Array<Record<string, unknown>> }>('/knowledge/company/ontology/releases'),
      get<Record<string, unknown>>('/knowledge/company/ontology/capabilities'),
    ]);
    const engine =
      capabilities.engine && typeof capabilities.engine === 'object'
        ? (capabilities.engine as Record<string, unknown>)
        : {};
    const engineValue = String(engine.status || engine.state || 'unavailable');
    return {
      engineStatus:
        engineValue === 'available' || engineValue === 'ready'
          ? 'available'
          : engineValue === 'degraded'
            ? 'degraded'
            : 'unavailable',
      installedPacks: (installations.installations ?? []).map((item) => {
        const installation =
          item.installation && typeof item.installation === 'object'
            ? (item.installation as Record<string, unknown>)
            : {};
        return {
          name: String(item.display_name || item.package_key || 'Company model'),
          version: String(item.version || ''),
          status: String(installation.status || 'installed'),
        };
      }),
      releases: (releases.releases ?? []).map((item) => ({
        area: areaFromNamespace(item.namespace),
        version: Number(item.version || 1),
        status: String(item.status || 'active'),
      })),
    };
  },
};
