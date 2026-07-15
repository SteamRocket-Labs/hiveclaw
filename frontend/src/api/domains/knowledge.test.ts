import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { knowledgeApi } from './knowledge';

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
  localStorageStub.setItem('token', 'test-token');
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

describe('knowledgeApi personal KB endpoints', () => {
  it('lists and reads personal knowledge documents under the agent knowledge route', async () => {
    fetchMock
      .mockResolvedValueOnce(jsonResponse({ documents: [{ document_id: 'doc-1', title: 'Taste notes' }] }))
      .mockResolvedValueOnce(jsonResponse({ document_id: 'doc-1', title: 'Taste notes', segments: [] }));

    await knowledgeApi.personalDocuments('agent-1');
    await knowledgeApi.personalDocument('agent-1', 'doc-1');

    expect(requestOf(0).url).toBe('/api/agents/agent-1/knowledge/personal/documents');
    expect(requestOf(0).init.method).toBe('GET');
    expect(requestOf(1).url).toBe('/api/agents/agent-1/knowledge/personal/documents/doc-1');
    expect(requestOf(1).init.method).toBe('GET');
  });

  it('ingests personal Markdown without sending an owner id from the browser', async () => {
    fetchMock.mockResolvedValueOnce(
      jsonResponse({
        document_id: 'doc-1',
        source_sha256: 'a'.repeat(64),
        artifact_hash: 'b'.repeat(64),
        canonical_md_path: 'persons/owner/kb/doc.md',
        segment_count: 1,
        status: 'ready',
      }),
    );

    await knowledgeApi.personalIngest('agent-1', {
      title: 'Taste notes',
      markdown: '# Taste\n\nPrefer source refs.',
      source_kind: 'paste',
      source_uri: 'clipboard://taste',
      agent_searchable: true,
      sensitivity: 'internal',
    });

    const request = requestOf();
    const body = JSON.parse(String(request.init.body));
    expect(request.url).toBe('/api/agents/agent-1/knowledge/personal/documents');
    expect(request.init.method).toBe('POST');
    expect(body.title).toBe('Taste notes');
    expect(body.markdown).toContain('Prefer source refs.');
    expect(body.owner_user_id).toBeUndefined();
  });

  it('searches personal KB with query encoding and limit', async () => {
    fetchMock.mockResolvedValueOnce(jsonResponse({ results: [] }));

    await knowledgeApi.personalSearch('agent-1', 'source refs + ACL', 7);

    expect(requestOf().url).toBe('/api/agents/agent-1/knowledge/personal/search?q=source+refs+%2B+ACL&limit=7');
    expect(requestOf().init.method).toBe('GET');
  });

  it('uses owner-scoped workspace personal KB routes without an agent id', async () => {
    fetchMock
      .mockResolvedValueOnce(jsonResponse({ documents: [{ document_id: 'doc-1', title: 'Owner notes' }] }))
      .mockResolvedValueOnce(jsonResponse({ results: [] }))
      .mockResolvedValueOnce(jsonResponse({ document_id: 'doc-1', title: 'Owner notes', segments: [] }))
      .mockResolvedValueOnce(
        jsonResponse({
          document_id: 'doc-2',
          source_sha256: 'a'.repeat(64),
          artifact_hash: 'b'.repeat(64),
          canonical_md_path: 'persons/owner/kb/doc.md',
          segment_count: 1,
          status: 'ready',
        }),
      );

    await knowledgeApi.myPersonalDocuments();
    await knowledgeApi.myPersonalSearch('owner scope', 9);
    await knowledgeApi.myPersonalDocument('doc-1');
    await knowledgeApi.myPersonalIngest({
      title: 'Owner note',
      markdown: '# Owner\n\nWorkspace level.',
      source_kind: 'paste',
      source_uri: 'browser://knowledge/personal',
      agent_searchable: true,
      sensitivity: 'internal',
    });

    expect(requestOf(0).url).toBe('/api/knowledge/personal/documents');
    expect(requestOf(1).url).toBe('/api/knowledge/personal/search?q=owner+scope&limit=9');
    expect(requestOf(2).url).toBe('/api/knowledge/personal/documents/doc-1');
    expect(requestOf(3).url).toBe('/api/knowledge/personal/documents');
    expect(JSON.parse(String(requestOf(3).init.body)).owner_user_id).toBeUndefined();
  });

  it('routes personal KB import jobs, document actions, graph, and grants to backend truth endpoints', async () => {
    fetchMock
      .mockResolvedValueOnce(jsonResponse({ document_id: 'doc-upload', status: 'ready' }))
      .mockResolvedValueOnce(jsonResponse({ document_id: 'doc-url', status: 'ready' }))
      .mockResolvedValueOnce(jsonResponse({ jobs: [] }))
      .mockResolvedValueOnce(jsonResponse({ document_id: 'doc-retry', status: 'ready' }))
      .mockResolvedValueOnce(jsonResponse({ document_id: 'doc-1', agent_searchable: false }))
      .mockResolvedValueOnce(jsonResponse({ document_id: 'doc-1', status: 'ready' }))
      .mockResolvedValueOnce(jsonResponse({ entities: [], links: [], assertions: [] }))
      .mockResolvedValueOnce(jsonResponse({ grants: [] }))
      .mockResolvedValueOnce(jsonResponse({ grant_id: 'grant-1' }))
      .mockResolvedValueOnce(jsonResponse({ revoked: true, deleted: false }));

    const file = new File(['# Upload'], 'upload.md', { type: 'text/markdown' });
    await knowledgeApi.myPersonalImportFile(file, { title: 'Upload', sensitivity: 'internal' });
    await knowledgeApi.myPersonalImportUrl({ url: 'https://example.com/page', title: 'Page' });
    await knowledgeApi.myPersonalImportJobs();
    await knowledgeApi.myPersonalRetryImportJob('job-1');
    await knowledgeApi.myPersonalPatchDocument('doc-1', { agent_searchable: false });
    await knowledgeApi.myPersonalRebuildDocument('doc-1');
    await knowledgeApi.myPersonalGraph();
    await knowledgeApi.myPersonalGrants();
    await knowledgeApi.myPersonalCreateGrant({
      resource_type: 'scope',
      grantee_type: 'agent',
      grantee_id: 'agent-1',
      permission: 'search',
      purpose: 'autonomous_agent',
      sensitivity_ceiling: 'PL3_sensitive',
      expires_at: '2099-01-01T00:00:00Z',
      metadata: { reason: 'research' },
    });
    await knowledgeApi.myPersonalDeleteGrant('grant-1');

    expect(requestOf(0).url).toBe('/api/knowledge/personal/imports');
    expect(requestOf(0).init.method).toBe('POST');
    expect(requestOf(0).init.body).toBeInstanceOf(FormData);
    expect(requestOf(1).url).toBe('/api/knowledge/personal/import-url');
    expect(JSON.parse(String(requestOf(1).init.body)).url).toBe('https://example.com/page');
    expect(requestOf(2).url).toBe('/api/knowledge/personal/import-jobs');
    expect(requestOf(3).url).toBe('/api/knowledge/personal/import-jobs/job-1/retry');
    expect(requestOf(4).url).toBe('/api/knowledge/personal/documents/doc-1');
    expect(requestOf(4).init.method).toBe('PATCH');
    expect(requestOf(5).url).toBe('/api/knowledge/personal/documents/doc-1/rebuild-index');
    expect(requestOf(6).url).toBe('/api/knowledge/personal/graph');
    expect(requestOf(7).url).toBe('/api/knowledge/personal/grants');
    expect(requestOf(8).url).toBe('/api/knowledge/personal/grants');
    expect(requestOf(8).init.method).toBe('POST');
    expect(JSON.parse(String(requestOf(8).init.body))).toMatchObject({
      purpose: 'autonomous_agent',
      sensitivity_ceiling: 'PL3_sensitive',
      expires_at: '2099-01-01T00:00:00Z',
    });
    expect(requestOf(9).url).toBe('/api/knowledge/personal/grants/grant-1');
    expect(requestOf(9).init.method).toBe('DELETE');
  });

  it('routes Agent proposals through owner review, revision history, and rollback endpoints', async () => {
    fetchMock
      .mockResolvedValueOnce(jsonResponse({ proposals: [{ proposal_id: 'proposal-1', status: 'pending' }] }))
      .mockResolvedValueOnce(jsonResponse({ proposal_id: 'proposal-1', status: 'committed' }))
      .mockResolvedValueOnce(jsonResponse({ revisions: [{ id: 'revision-1', version: 1 }] }))
      .mockResolvedValueOnce(jsonResponse({ document_id: 'doc-1', version: 2, rollback_of_version: 1 }));

    await knowledgeApi.myPersonalProposals('pending');
    await knowledgeApi.myPersonalDecideProposal('proposal-1', {
      decision: 'approve',
      reason: 'Owner verified the evidence.',
    });
    await knowledgeApi.myPersonalDocumentRevisions('doc-1');
    await knowledgeApi.myPersonalRollbackDocument('doc-1', 1);

    expect(requestOf(0).url).toBe('/api/knowledge/personal/proposals?status=pending');
    expect(requestOf(0).init.method).toBe('GET');
    expect(requestOf(1).url).toBe('/api/knowledge/personal/proposals/proposal-1/decision');
    expect(requestOf(1).init.method).toBe('POST');
    expect(JSON.parse(String(requestOf(1).init.body))).toEqual({
      decision: 'approve',
      reason: 'Owner verified the evidence.',
    });
    expect(requestOf(2).url).toBe('/api/knowledge/personal/documents/doc-1/revisions');
    expect(requestOf(3).url).toBe('/api/knowledge/personal/documents/doc-1/rollback');
    expect(JSON.parse(String(requestOf(3).init.body))).toEqual({ target_version: 1 });
  });
});
