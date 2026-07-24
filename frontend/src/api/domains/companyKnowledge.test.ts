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
