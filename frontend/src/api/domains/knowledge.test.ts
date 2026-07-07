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
});
