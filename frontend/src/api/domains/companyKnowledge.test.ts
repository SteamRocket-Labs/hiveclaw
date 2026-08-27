import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { companyKnowledgeApi } from './companyKnowledge';

const localStore: Record<string, string> = {};
const localStorageStub = {
  getItem: (key: string) => (key in localStore ? localStore[key] : null),
  setItem: (key: string, value: string) => {
    localStore[key] = value;
  },
  removeItem: (key: string) => {
    delete localStore[key];
  },
  clear: () => {
    for (const key of Object.keys(localStore)) delete localStore[key];
  },
};

function jsonResponse(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { 'Content-Type': 'application/json' },
  });
}

let fetchMock: ReturnType<typeof vi.fn<typeof fetch>>;

beforeEach(() => {
  vi.stubGlobal('localStorage', localStorageStub);
  vi.stubGlobal('crypto', { randomUUID: () => 'request-1' });
  localStorageStub.setItem('token', 'test-token');
  localStorageStub.setItem('current_tenant_id', '8a02e41f-385f-49cf-9142-befbbfcd8f55');
  fetchMock = vi.fn<typeof fetch>();
  vi.stubGlobal('fetch', fetchMock);
});

afterEach(() => {
  vi.unstubAllGlobals();
  localStorageStub.clear();
});

function requestOf(callIndex = 0): { url: string; init: RequestInit } {
  const call = fetchMock.mock.calls[callIndex];
  return { url: String(call?.[0] ?? ''), init: (call?.[1] ?? {}) as RequestInit };
}

describe('companyKnowledgeApi', () => {
  it('projects authorized Company Library results without forwarding forensic fields', async () => {
    fetchMock.mockResolvedValueOnce(
      jsonResponse({
        status: 'ok',
        documents: [
          {
            publication_id: 'publication-1',
            document_id: 'document-1',
            title: 'Employee Handbook',
            namespace: 'company/policies',
            sensitivity: 'PL2_pii',
            version: 4,
            valid_from: '2026-07-24T00:00:00Z',
            valid_until: null,
            source_ref: 'company-publication://publication-1/documents/document-1',
          },
        ],
        authority: { required_actions: ['discover', 'search'] },
        warnings: [],
      }),
    );

    const result = await companyKnowledgeApi.listLibrary();

    expect(requestOf().url).toBe('/api/knowledge/company/documents?limit=200');
    expect(result.documents).toEqual([
      {
        publicationKey: 'publication-1',
        documentKey: 'document-1',
        title: 'Employee Handbook',
        area: 'policies',
        sensitivity: 'personal_data',
        version: 4,
        validFrom: '2026-07-24T00:00:00Z',
        validUntil: null,
      },
    ]);
    expect(JSON.stringify(result)).not.toContain('company-publication://');
    expect(JSON.stringify(result)).not.toContain('required_actions');
  });

  it('maps segment-level search hits to unique internal segment identity without forwarding forensic fields (RC-02C)', async () => {
    fetchMock.mockResolvedValueOnce(
      jsonResponse({
        status: 'ok',
        results: [
          {
            result_kind: 'company_knowledge_segment',
            publication_id: 'publication-1',
            document_id: 'document-1',
            segment_id: 'segment-1',
            version: 1,
            title: 'Runbook',
            namespace: 'company/general',
            snippet: 'Alpha snippet with the marker.',
            source_ref: 'company-publication://publication-1/documents/document-1#segment=segment-1',
            sensitivity: 'PL1_public',
            score: 0.91,
            score_trace: { exact: 3 },
            valid_from: '2026-08-26T00:00:00Z',
            valid_until: null,
          },
          {
            result_kind: 'company_knowledge_segment',
            publication_id: 'publication-1',
            document_id: 'document-1',
            segment_id: 'segment-2',
            version: 1,
            title: 'Runbook',
            namespace: 'company/general',
            snippet: 'Beta snippet with the marker.',
            source_ref: 'company-publication://publication-1/documents/document-1#segment=segment-2',
            sensitivity: 'PL1_public',
            score: 0.87,
            score_trace: { exact: 2 },
            valid_from: '2026-08-26T00:00:00Z',
            valid_until: null,
          },
        ],
        authority: { required_actions: ['discover', 'search'] },
        warnings: [],
      }),
    );

    const result = await companyKnowledgeApi.searchLibrary('  marker  ');

    const request = requestOf();
    expect(request.url).toBe('/api/knowledge/company/search');
    expect(JSON.parse(String(request.init.body))).toMatchObject({ query: 'marker', limit: 50 });

    // segment_id becomes the internal segmentKey; snippet and backend order
    // are preserved so repeated hits of one document keep unique identity.
    expect(result.results.map((hit) => hit.segmentKey)).toEqual(['segment-1', 'segment-2']);
    expect(result.results.map((hit) => hit.snippet)).toEqual([
      'Alpha snippet with the marker.',
      'Beta snippet with the marker.',
    ]);
    expect(result.results.map((hit) => hit.title)).toEqual(['Runbook', 'Runbook']);

    // The UI model never receives source_ref, score, score_trace, or other
    // forensic fields.
    const serialized = JSON.stringify(result);
    for (const forbidden of ['source_ref', 'score_trace', 'score', 'company-publication://', 'required_actions']) {
      expect(serialized).not.toContain(forbidden);
    }
    expect(result.results[0]).not.toHaveProperty('score');
    expect(result.results[0]).not.toHaveProperty('source_ref');
  });

  it('submits a Personal document through explicit scope-change attestation', async () => {
    fetchMock.mockResolvedValueOnce(
      jsonResponse({ intake_id: 'intake-1', status: 'queued', recovery: 'automatic' }, 202),
    );

    await companyKnowledgeApi.submitPersonal({
      documentKey: 'document-1',
      area: 'team_notes',
      purpose: 'Share the reviewed onboarding note with the company.',
      title: 'Onboarding note',
    });

    const request = requestOf();
    const body = JSON.parse(String(request.init.body));
    expect(request.url).toBe('/api/knowledge/company/promotion-intakes/personal');
    expect(request.init.method).toBe('POST');
    expect(body).toMatchObject({
      document_id: 'document-1',
      proposed_namespace: 'company/team-notes',
      purpose: 'Share the reviewed onboarding note with the company.',
      title: 'Onboarding note',
      attest_scope_change: true,
      idempotency_key: 'company-personal:request-1',
      trace_id: 'company-personal:request-1',
    });
    expect(body).not.toHaveProperty('source_ref');
    expect(body).not.toHaveProperty('content_hash');
  });

  it('keeps legacy paths and hashes out of candidate labels while replaying the exact snapshot', async () => {
    fetchMock
      .mockResolvedValueOnce(
        jsonResponse({
          candidates: [
            {
              relative_path: 'retired/team/private/onboarding.md',
              size_bytes: 128,
              sha256: 'a'.repeat(64),
            },
          ],
          excluded_symlink_count: 2,
        }),
      )
      .mockResolvedValueOnce(
        jsonResponse({ intake_id: 'intake-1', status: 'queued', recovery: 'automatic' }, 202),
      );

    const result = await companyKnowledgeApi.listLegacyCandidates();
    expect(result.candidates[0]).toMatchObject({
      label: 'onboarding.md',
      sizeBytes: 128,
    });
    expect(result.candidates[0].label).not.toContain('/');

    await companyKnowledgeApi.submitLegacy({
      candidate: result.candidates[0],
      area: 'playbooks',
      sensitivity: 'restricted',
      purpose: 'Recover the reviewed onboarding playbook.',
      title: 'Onboarding playbook',
    });

    const body = JSON.parse(String(requestOf(1).init.body));
    expect(body.relative_path).toBe('retired/team/private/onboarding.md');
    expect(body.expected_sha256).toBe('a'.repeat(64));
    expect(body.proposed_namespace).toBe('company/playbooks');
    expect(body.proposed_sensitivity).toBe('PL3_sensitive');
    expect(body.attest_scope_change).toBe(true);
  });

  it('maps business capabilities to governed permission actions and never asks the page for raw rules', async () => {
    fetchMock.mockResolvedValueOnce(
      jsonResponse({
        permission_id: 'permission-1',
        principal: { kind: 'role', label: 'All employees' },
        resource: { kind: 'company', label: 'All Company Knowledge' },
        capabilities: ['find_and_read'],
        effect: 'allow',
        sensitivity_ceiling: 'PL2_pii',
        purposes: ['interactive_session'],
        expires_at: null,
        active: true,
      }),
    );

    await companyKnowledgeApi.grantAccess({
      audience: { kind: 'role', key: 'role:member', label: 'All employees' },
      capabilities: ['find_and_read', 'propose_updates'],
      sensitivity: 'personal_data',
      effect: 'allow',
    });

    const body = JSON.parse(String(requestOf().init.body));
    expect(body).toMatchObject({
      principal_type: 'role',
      principal_id: null,
      principal_key: 'role:member',
      resource_type: 'company_knowledge_scope',
      resource_id: '8a02e41f-385f-49cf-9142-befbbfcd8f55',
      actions: ['cite', 'discover', 'propose', 'read', 'search'],
      effect: 'allow',
      sensitivity_ceiling: 'PL2_pii',
      purposes: ['interactive_session'],
    });
    expect(body).not.toHaveProperty('conditions');
    expect(body).not.toHaveProperty('tool_rules');
  });

  it('does not issue a scope-wide permission mutation without an authenticated tenant', async () => {
    localStorageStub.removeItem('current_tenant_id');

    await expect(
      companyKnowledgeApi.grantAccess({
        audience: { kind: 'role', key: 'role:member', label: 'All employees' },
        capabilities: ['find_and_read'],
        sensitivity: 'company',
        effect: 'allow',
      }),
    ).rejects.toThrow('company_knowledge_tenant_required');

    expect(fetchMock).not.toHaveBeenCalled();
  });

  it('loads durable publication lifecycle rows so retirement remains recoverable after reload', async () => {
    fetchMock.mockResolvedValueOnce(
      jsonResponse({
        publications: [
          {
            publication_id: 'publication-1',
            document_id: 'document-1',
            title: 'Employee Handbook',
            status: 'retired',
            version: 3,
            namespace: 'company/policies',
            sensitivity: 'PL2_pii',
            valid_from: '2026-07-20T00:00:00Z',
            valid_until: '2026-07-24T00:00:00Z',
            available_action: 'restore',
          },
        ],
      }),
    );

    const result = await companyKnowledgeApi.listPublicationLifecycle();

    expect(requestOf().url).toBe('/api/knowledge/company/publications?limit=200');
    expect(result[0]).toMatchObject({
      publicationKey: 'publication-1',
      documentKey: 'document-1',
      title: 'Employee Handbook',
      status: 'retired',
      availableAction: 'restore',
    });
  });
});

// ---------------------------------------------------------------------------
// RC-02: direct file import + import job lifecycle client contract
// ---------------------------------------------------------------------------

const jobSummaryBody = {
  job_id: 'job-1',
  status: 'queued',
  lifecycle_status: 'queued',
  attempt_count: 0,
  max_attempts: 5,
  terminal: false,
  retryable: false,
  cancellable: true,
  error_code: null,
  title: 'Runbook',
  source_filename: 'runbook.pdf',
  namespace: 'company/general',
  sensitivity: 'internal',
  source_id: null,
  evidence_id: null,
  document_id: null,
  proposal_id: null,
  idempotency_key: 'ckb-test',
  cancelled_at: null,
  created_at: null,
  updated_at: null,
  completed_at: null,
};

describe('companyKnowledgeApi direct import endpoints', () => {
  it('lists and creates source contracts for the managed file intake', async () => {
    // The real backend wraps rows in the source_contracts envelope.
    fetchMock
      .mockResolvedValueOnce(
        jsonResponse({
          source_contracts: [
            { id: 'contract-1', stable_source_id: 'company-file-upload', status: 'active', version: 1, allowed_namespaces_json: ['company/general'], default_sensitivity: 'PL1_public' },
          ],
        }),
      )
      .mockResolvedValueOnce(jsonResponse({ id: 'contract-1', stable_source_id: 'company-file-upload', status: 'active', version: 1 }));

    const contracts = await companyKnowledgeApi.listSourceContracts();
    const created = await companyKnowledgeApi.createSourceContract({
      stable_source_id: 'company-file-upload',
      accountable_steward_ref: 'role:org_admin',
      allowed_namespaces: ['company/general'],
      default_sensitivity: 'PL1_public',
    });

    expect(requestOf(0).url).toBe('/api/knowledge/company/source-contracts');
    expect(contracts[0]).toMatchObject({ contractKey: 'contract-1', stableSourceId: 'company-file-upload', status: 'active' });
    const createInit = requestOf(1);
    expect(createInit.url).toBe('/api/knowledge/company/source-contracts');
    expect(createInit.init.method).toBe('POST');
    const payload = JSON.parse(String(createInit.init.body));
    expect(payload.source_kind).toBe('managed_file');
    expect(payload.provider_kind).toBe('manual_upload');
    expect(payload.ingest_mode).toBe('manual');
    expect(payload.allowed_namespaces).toEqual(['company/general']);
    expect(created.contractKey).toBe('contract-1');
  });

  it('uploads one file as multipart form data and returns the queued job summary', async () => {
    fetchMock.mockResolvedValueOnce(jsonResponse(jobSummaryBody, 202));

    const file = new File(['# Runbook'], 'runbook.md', { type: 'text/markdown' });
    const result = await companyKnowledgeApi.uploadCompanyImportFile(file, {
      source_contract_id: 'contract-1',
      source_contract_version: 1,
      title: 'Runbook',
      proposed_namespace: 'company/general',
      proposed_sensitivity: 'internal',
      purpose: 'RC-02',
      idempotency_key: 'ckb-upload-1',
    });

    const init = requestOf(0);
    expect(init.url).toBe('/api/knowledge/company/imports/file');
    expect(init.init.method).toBe('POST');
    expect(init.init.body).toBeInstanceOf(FormData);
    const form = init.init.body as FormData;
    expect(form.get('source_contract_id')).toBe('contract-1');
    expect(form.get('proposed_namespace')).toBe('company/general');
    expect(form.get('idempotency_key')).toBe('ckb-upload-1');
    // The backend requires the ACL snapshot explicitly; the admin-only manual
    // upload keeps the pre-change tenant-wide default until the UI grows an
    // ACL editor.
    const aclField = form.get('source_acl_snapshot');
    expect(aclField).toBe(JSON.stringify({ all_tenant_members: true }));
    expect(JSON.parse(String(aclField))).toEqual({ all_tenant_members: true });
    expect(result.lifecycleStatus).toBe('queued');
    expect(result.cancellable).toBe(true);
    expect(result.maxAttempts).toBe(5);
  });

  it('lists import jobs and exposes the lifecycle read model', async () => {
    fetchMock.mockResolvedValueOnce(jsonResponse({ jobs: [jobSummaryBody] }));

    const jobs = await companyKnowledgeApi.listCompanyImportJobs();

    expect(requestOf(0).url).toBe('/api/knowledge/company/import-jobs?limit=50');
    expect(jobs[0]).toMatchObject({ jobKey: 'job-1', lifecycleStatus: 'queued', cancellable: true });
  });

  it('retries, cancels, previews, and creates proposals through the lifecycle endpoints', async () => {
    fetchMock
      .mockResolvedValueOnce(jsonResponse({ ...jobSummaryBody, status: 'failed', lifecycle_status: 'failed', terminal: true, retryable: true, cancellable: false, error_code: 'conversion_timeout' }))
      .mockResolvedValueOnce(jsonResponse({ ...jobSummaryBody, status: 'cancelled', lifecycle_status: 'cancelled', terminal: true, cancellable: false, cancelled_at: '2026-08-25T01:02:03+00:00' }))
      .mockResolvedValueOnce(
        jsonResponse({
          job_id: 'job-1',
          document_id: 'doc-1',
          evidence_id: 'ev-1',
          source_id: 'src-1',
          proposal_id: null,
          title: 'Runbook',
          namespace: 'company/general',
          sensitivity: 'internal',
          segments: [{ segment_id: 'seg-1', position: 0, heading_path: ['Runbook'], content: 'marker', token_count: 3 }],
        }),
      )
      .mockResolvedValueOnce(jsonResponse({ id: 'proposal-1', status: 'submitted', proposal_kind: 'knowledge' }));

    const retried = await companyKnowledgeApi.retryCompanyImportJob('job-1');
    const cancelled = await companyKnowledgeApi.cancelCompanyImportJob('job-1');
    const preview = await companyKnowledgeApi.getCompanyImportPreview('job-1');
    const proposal = await companyKnowledgeApi.createProposalFromImport('job-1');

    expect(requestOf(0).url).toBe('/api/knowledge/company/import-jobs/job-1/retry');
    expect(retried.retryable).toBe(true);
    expect(requestOf(1).url).toBe('/api/knowledge/company/import-jobs/job-1/cancel');
    expect(cancelled.cancelledAt).toBe('2026-08-25T01:02:03+00:00');
    expect(requestOf(2).url).toBe('/api/knowledge/company/import-jobs/job-1/preview');
    expect(preview.segments[0]).toMatchObject({ segmentKey: 'seg-1', content: 'marker' });
    expect(requestOf(3).url).toBe('/api/knowledge/company/import-jobs/job-1/create-proposal');
    expect(proposal.proposalKey).toBe('proposal-1');
    expect(proposal.status).toBe('submitted');
  });
});
