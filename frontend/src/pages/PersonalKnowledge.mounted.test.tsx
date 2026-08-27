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
import { cleanup, fireEvent, render, screen } from '@testing-library/react';
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
  return screen.getByPlaceholderText('Search everything you have handled...') as HTMLInputElement;
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
