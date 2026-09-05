// @vitest-environment jsdom
//
// Mounted interaction tests for the Personal Knowledge search surface (RC-01B):
// the real page is rendered in jsdom with a real @tanstack/react-query
// QueryClient, a real MemoryRouter, and the real i18n catalog; only the
// knowledgeApi domain boundary is mocked. These tests prove the explicit
// search submission flow: a discoverable submit control, trimmed activation,
// same-query refetch, truthful pending/empty/error terminal states, and that
// a zero-hit success is never rendered as unavailable (and vice versa).

import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { ApiError } from '../api/core';
import { knowledgeApi, type PersonalKnowledgeSearchResult } from '../api/domains/knowledge';
import '../i18n';

vi.mock('../api/domains/knowledge', () => ({
  knowledgeApi: {
    myPersonalDocuments: vi.fn(),
    myPersonalDocument: vi.fn(),
    myPersonalDocumentSourcePreview: vi.fn(),
    myPersonalSearch: vi.fn(),
    myPersonalIngest: vi.fn(),
    myPersonalImportFile: vi.fn(),
    myPersonalImportUrl: vi.fn(),
    myPersonalImportJobs: vi.fn(),
    myPersonalRetryImportJob: vi.fn(),
    myPersonalCancelImportJob: vi.fn(),
    myPersonalPatchDocument: vi.fn(),
    myPersonalRestoreDocument: vi.fn(),
    myPersonalRebuildDocument: vi.fn(),
    myPersonalGraph: vi.fn(),
    myPersonalGrants: vi.fn(),
    myPersonalCreateGrant: vi.fn(),
    myPersonalDeleteGrant: vi.fn(),
    myPersonalProposals: vi.fn(),
    myPersonalDecideProposal: vi.fn(),
    myPersonalDocumentRevisions: vi.fn(),
    myPersonalRollbackDocument: vi.fn(),
  },
}));

import PersonalKnowledge from './PersonalKnowledge';

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

const api = vi.mocked(knowledgeApi);

const MARKER = 'HIVE-PERSONAL-RUN1-QUARTZ-417';

function searchHit(): PersonalKnowledgeSearchResult {
  return {
    document_id: 'doc-1',
    segment_id: 'seg-1',
    title: 'Quartz research notes',
    snippet: `Marker ${MARKER} anchors the quartz segment.`,
    source_ref: 'kb://person/user-1/documents/doc-1#segment=seg-1',
    score: 0.98,
    heading_path: ['Field notes', 'Quartz'],
    sensitivity: 'internal',
    metadata: {},
  };
}

function deferred<T>() {
  let resolve!: (value: T) => void;
  let reject!: (reason?: unknown) => void;
  const promise = new Promise<T>((res, rej) => {
    resolve = res;
    reject = rej;
  });
  return { promise, resolve, reject };
}

function renderPage() {
  const queryClient = new QueryClient({
    defaultOptions: {
      queries: { retry: false },
      mutations: { retry: false },
    },
  });
  return render(
    <QueryClientProvider client={queryClient}>
      <MemoryRouter>
        <PersonalKnowledge />
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

function searchInput(): HTMLInputElement {
  return screen.getByPlaceholderText('Search your documents and notes...') as HTMLInputElement;
}

function searchButton(): HTMLButtonElement {
  return screen.getByRole('button', { name: 'Search' }) as HTMLButtonElement;
}

beforeEach(() => {
  vi.clearAllMocks();
  localStorage.clear();
  // Bounded unrelated queries: empty documents/jobs keep the mounted page on
  // the search path without driving any other lane or detail query.
  api.myPersonalDocuments.mockResolvedValue({ documents: [] });
  api.myPersonalImportJobs.mockResolvedValue({ jobs: [] });
  api.myPersonalSearch.mockResolvedValue({ results: [searchHit()] });
});

afterEach(() => {
  cleanup();
});

describe('PersonalKnowledge search submission (mounted)', () => {
  it('keeps the explicit Search action disabled for a blank or whitespace query and renders no search state before submission', async () => {
    renderPage();
    await screen.findByText('No import jobs yet.');

    // The form is a labelled search landmark with an explicit submit control.
    expect(screen.getByRole('search', { name: 'Search Personal Knowledge' })).toBeTruthy();
    expect(searchButton().disabled).toBe(true);

    fireEvent.change(searchInput(), { target: { value: '   ' } });
    expect(searchButton().disabled).toBe(true);
    fireEvent.submit(screen.getByRole('search', { name: 'Search Personal Knowledge' }));

    expect(api.myPersonalSearch).not.toHaveBeenCalled();
    // No search state is rendered before a non-empty query is submitted.
    expect(screen.queryByRole('heading', { name: 'Search results' })).toBeNull();
  });

  it('submits the trimmed query with limit 8 and renders title, heading path, snippet, and the exact kb:// source_ref', async () => {
    renderPage();
    await screen.findByText('No import jobs yet.');

    fireEvent.change(searchInput(), { target: { value: `  ${MARKER}  ` } });
    expect(searchButton().disabled).toBe(false);
    fireEvent.click(searchButton());

    await screen.findByRole('heading', { name: 'Search results' });
    expect(api.myPersonalSearch).toHaveBeenCalledTimes(1);
    expect(api.myPersonalSearch).toHaveBeenCalledWith(MARKER, 8);
    expect(screen.getByText('Quartz research notes')).toBeTruthy();
    expect(screen.getByText('Field notes / Quartz')).toBeTruthy();
    expect(screen.getByText(`Marker ${MARKER} anchors the quartz segment.`)).toBeTruthy();
    expect(screen.getByText('kb://person/user-1/documents/doc-1#segment=seg-1')).toBeTruthy();
  });

  it('keeps Enter submission via the form onSubmit working', async () => {
    renderPage();
    await screen.findByText('No import jobs yet.');

    fireEvent.change(searchInput(), { target: { value: MARKER } });
    fireEvent.submit(screen.getByRole('search', { name: 'Search Personal Knowledge' }));

    await screen.findByRole('heading', { name: 'Search results' });
    expect(api.myPersonalSearch).toHaveBeenCalledWith(MARKER, 8);
  });

  it('treats clicking Search again with the same query as an explicit refetch', async () => {
    renderPage();
    await screen.findByText('No import jobs yet.');

    fireEvent.change(searchInput(), { target: { value: MARKER } });
    fireEvent.click(searchButton());
    await screen.findByText('Quartz research notes');
    expect(api.myPersonalSearch).toHaveBeenCalledTimes(1);

    fireEvent.click(searchButton());
    await screen.findByText('Quartz research notes');
    expect(api.myPersonalSearch).toHaveBeenCalledTimes(2);
    expect(api.myPersonalSearch).toHaveBeenLastCalledWith(MARKER, 8);
  });

  it('shows a truthful Searching label and disables the action while the search is in flight', async () => {
    const pending = deferred<{ results: PersonalKnowledgeSearchResult[] }>();
    api.myPersonalSearch.mockReturnValue(pending.promise);
    renderPage();
    await screen.findByText('No import jobs yet.');

    fireEvent.change(searchInput(), { target: { value: MARKER } });
    fireEvent.click(searchButton());

    const busyButton = (await screen.findByRole('button', { name: 'Searching...' })) as HTMLButtonElement;
    expect(busyButton.disabled).toBe(true);
    expect(api.myPersonalSearch).toHaveBeenCalledTimes(1);

    pending.resolve({ results: [searchHit()] });
    await screen.findByText('Quartz research notes');
    expect(searchButton().disabled).toBe(false);
  });

  it('renders an explicit localized empty-result state for a completed search with zero hits', async () => {
    api.myPersonalSearch.mockResolvedValue({ results: [] });
    renderPage();
    await screen.findByText('No import jobs yet.');

    fireEvent.change(searchInput(), { target: { value: 'quartz-absent-000' } });
    fireEvent.click(searchButton());

    await screen.findByRole('heading', { name: 'Search results' });
    expect(screen.getByText(/No results for "quartz-absent-000"/)).toBeTruthy();
    // The empty conclusion is distinct from the unavailable/error surface.
    expect(document.querySelector('[data-personal-knowledge-state]')).toBeNull();
  });

  it('renders the typed unavailable surface on API rejection and never the empty conclusion', async () => {
    api.myPersonalSearch.mockRejectedValue(new ApiError(500, 'Internal Server Error'));
    renderPage();
    await screen.findByText('No import jobs yet.');

    fireEvent.change(searchInput(), { target: { value: MARKER } });
    fireEvent.click(searchButton());

    await screen.findByText('Personal Knowledge is temporarily unavailable');
    expect(document.querySelector('[data-personal-knowledge-state="unavailable"]')).not.toBeNull();
    expect(screen.queryByText(/No results for/)).toBeNull();
    expect(screen.queryByRole('heading', { name: 'Search results' })).toBeNull();
  });
});

// ---------------------------------------------------------------------------
// Media import failure: the novice explanation must name the real
// prerequisites (administrator-enabled processing capability, or a supported
// text/document format) while retryability stays a pure backend fact.
// ---------------------------------------------------------------------------

type MediaJobOverrides = Record<string, unknown>;

function mediaJob(overrides: MediaJobOverrides = {}) {
  return {
    job_id: 'job-media-1',
    document_id: 'doc-media-1',
    stage: 'transcribing',
    status: 'failed',
    artifact_hash: 'b'.repeat(64),
    error_message: 'unsupported_or_unconfigured:media_transcription_provider',
    attempt_count: 1,
    metadata: { source_filename: 'voice-memo.mp3', media_kind: 'audio', source_mime_type: 'audio/mpeg' },
    created_at: '2026-09-01T00:00:00Z',
    updated_at: '2026-09-01T00:01:00Z',
    terminal: true,
    retryable: true,
    cancellable: false,
    error_code: 'unsupported_or_unconfigured',
    max_attempts: 5,
    lifecycle_status: 'failed',
    result_status: 'failed',
    cancelled_at: null,
    ...overrides,
  };
}

describe('PersonalKnowledge media import failure recovery (mounted)', () => {
  it('explains the prerequisites and sends exactly one retry request for a retryable media failure', async () => {
    api.myPersonalImportJobs.mockResolvedValue({ jobs: [mediaJob()] });
    api.myPersonalDocument.mockResolvedValue({
      document_id: 'doc-media-1',
      title: 'voice-memo.mp3',
      source_kind: 'upload',
      source_uri: null,
      source_sha256: 'b'.repeat(64),
      source_ref: 'kb://person/user-1/documents/doc-media-1',
      canonical_md_path: 'persons/user-1/kb/doc-media-1.md',
      status: 'failed',
      sensitivity: 'internal',
      agent_searchable: true,
      segment_count: 0,
      created_at: '2026-09-01T00:00:00Z',
      updated_at: null,
      metadata: { media_kind: 'audio', source_filename: 'voice-memo.mp3' },
      segments: [],
    });
    api.myPersonalDocumentRevisions.mockResolvedValue({ revisions: [] });
    const pending = deferred<Record<string, unknown>>();
    api.myPersonalRetryImportJob.mockReturnValue(pending.promise as never);
    renderPage();
    await screen.findByText('voice-memo.mp3');

    // The explanation, not the raw machine code, reaches the DOM.
    expect(screen.getByText(/administrator may need to enable media processing/i)).toBeTruthy();
    expect(screen.getByText(/plain-text version instead/i)).toBeTruthy();
    expect(document.body.innerHTML).not.toContain('unsupported_or_unconfigured');
    expect(document.body.innerHTML).not.toContain('media_transcription_provider');

    const retryButton = screen.getByRole('button', { name: 'Retry' }) as HTMLButtonElement;
    fireEvent.click(retryButton);
    await waitFor(() => expect(api.myPersonalRetryImportJob).toHaveBeenCalledTimes(1));
    expect(api.myPersonalRetryImportJob).toHaveBeenCalledWith('job-media-1');

    // The in-flight job action is disabled while the request is pending;
    // a second click cannot fire.
    const busyButton = screen.getByRole('button', { name: 'Retry' }) as HTMLButtonElement;
    expect(busyButton.disabled).toBe(true);
    fireEvent.click(busyButton);
    expect(api.myPersonalRetryImportJob).toHaveBeenCalledTimes(1);

    pending.resolve({ ...mediaJob(), lifecycle_status: 'queued', status: 'queued', retryable: false, error_code: null, error_message: null });
    await waitFor(() => {
      expect((screen.getByRole('button', { name: 'Retry' }) as HTMLButtonElement).disabled).toBe(false);
    });
    expect(api.myPersonalRetryImportJob).toHaveBeenCalledTimes(1);
  });

  it('offers no retry action for a non-retryable media failure and never promises one', async () => {
    api.myPersonalImportJobs.mockResolvedValue({ jobs: [mediaJob({ retryable: false, attempt_count: 5 })] });
    renderPage();
    await screen.findByText('voice-memo.mp3');

    expect(screen.getByText(/administrator may need to enable media processing/i)).toBeTruthy();
    expect(screen.queryByRole('button', { name: 'Retry' })).toBeNull();
    expect(api.myPersonalRetryImportJob).not.toHaveBeenCalled();
  });
});

// ---------------------------------------------------------------------------
// Robustness: a malformed 200 detail body (missing the required segments
// array) is a backend contract violation, but it must degrade the detail
// panel instead of crashing the entire app behind the root error boundary.
// ---------------------------------------------------------------------------

const libraryDocument = {
  document_id: 'doc-1',
  title: 'Quartz research notes',
  source_kind: 'paste',
  source_uri: 'browser://knowledge/personal',
  source_sha256: 'a'.repeat(64),
  source_ref: 'kb://person/user-1/documents/doc-1',
  canonical_md_path: 'persons/user-1/kb/doc-1.md',
  status: 'ready',
  sensitivity: 'internal',
  agent_searchable: true,
  segment_count: 1,
  created_at: '2026-07-12T00:00:00Z',
  updated_at: null,
  metadata: {},
};

const libraryDocumentDetail = {
  ...libraryDocument,
  segments: [{
    segment_id: 'seg-1',
    position: 0,
    heading_path: ['Field notes'],
    content: `Marker ${MARKER} anchors the quartz segment.`,
    token_count: 9,
  }],
};

describe('PersonalKnowledge document detail robustness (mounted)', () => {
  it('degrades a detail body missing its segments array instead of crashing the page', async () => {
    api.myPersonalDocuments.mockResolvedValue({ documents: [libraryDocument] });
    // Contract violation: a 200 detail body without the required segments array.
    api.myPersonalDocument.mockResolvedValue({ document_id: 'doc-1', title: 'Quartz research notes' } as never);
    api.myPersonalDocumentRevisions.mockResolvedValue({ revisions: [] });
    renderPage();

    await screen.findByText('Content preview');
    expect(screen.getAllByText('Quartz research notes').length).toBeGreaterThan(0);
    expect(screen.getByText('Status unavailable')).toBeTruthy();
  });

  it('renders library metadata with plain-language sensitivity instead of the raw enum', async () => {
    api.myPersonalDocuments.mockResolvedValue({ documents: [libraryDocument] });
    api.myPersonalDocument.mockResolvedValue(libraryDocumentDetail);
    api.myPersonalDocumentRevisions.mockResolvedValue({ revisions: [] });
    renderPage();
    await screen.findByText('No import jobs yet.');

    fireEvent.click(screen.getByRole('tab', { name: /^Library/ }));

    await waitFor(() => {
      const meta = document.querySelector('.personal-kb-doc-meta');
      expect(meta?.textContent).toContain('Internal');
      expect(meta?.textContent).not.toContain('· internal');
      // The indexed-piece count uses the plain unit, never the schema noun,
      // and a single piece takes the English singular ("1 part", never "1 parts").
      expect(meta?.textContent).toContain('1 part ·');
      expect(meta?.textContent).not.toContain('segment');
      const stats = document.querySelector('.personal-kb-stats');
      expect(stats?.textContent).toContain('1 part');
      expect(stats?.textContent).not.toContain('1 parts');
      expect(stats?.textContent).toContain('1 document');
      expect(stats?.textContent).not.toContain('1 documents');
    });
  });
});
