// @vitest-environment jsdom
//
// Mounted-query tests for the real CompanyKnowledgeControlPlane page: a real
// QueryClient drives the page while only the API domain boundaries are mocked.
// These tests prove that (1) the access audience selector exposes the exact
// backend-supported `role:platform_admin` key through an explicit operator
// action (no bypass, no automatic grant), and (2) a successful grant or revoke
// makes the mounted review queue and the selected review workspace refetch and
// change without a page reload.

import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import type {
  CompanyKnowledgeAccessRule,
  CompanyKnowledgeReview,
  CompanyKnowledgeReviewWorkspace,
} from '../api/domains/companyKnowledge';
import { companyKnowledgeApi } from '../api/domains/companyKnowledge';
import { agentApi } from '../api/domains/agents';
import { enterpriseApi } from '../api/domains/enterprise';
import '../i18n';

vi.mock('../api/domains/companyKnowledge', () => ({
  companyKnowledgeApi: {
    listIntakes: vi.fn(),
    listSourceContracts: vi.fn(),
    listCompanyImportJobs: vi.fn(),
    getCompanyImportPreview: vi.fn(),
    listLegacyCandidates: vi.fn(),
    listReviews: vi.fn(),
    getReviewWorkspace: vi.fn(),
    listAccessRules: vi.fn(),
    grantAccess: vi.fn(),
    revokeAccess: vi.fn(),
    listPublicationLifecycle: vi.fn(),
    getOntologyStatus: vi.fn(),
    submitLegacy: vi.fn(),
    retryIntake: vi.fn(),
    materializeReview: vi.fn(),
    decideReview: vi.fn(),
    publishReview: vi.fn(),
    uploadCompanyImportFile: vi.fn(),
    createSourceContract: vi.fn(),
    retryCompanyImportJob: vi.fn(),
    cancelCompanyImportJob: vi.fn(),
    createProposalFromImport: vi.fn(),
  },
}));

vi.mock('../api/domains/agents', () => ({
  agentApi: { list: vi.fn() },
}));

vi.mock('../api/domains/enterprise', () => ({
  enterpriseApi: { getOrgMembers: vi.fn() },
}));

import CompanyKnowledgeControlPlane from './CompanyKnowledgeControlPlane';

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

const api = vi.mocked(companyKnowledgeApi);

const reviewA: CompanyKnowledgeReview = {
  reviewKey: 'review-1',
  title: 'Employee Handbook',
  status: 'submitted',
  kind: 'personal',
  area: 'policies',
  sensitivity: 'personal_data',
  risk: 'normal',
  reason: 'Owner submitted a reviewed policy.',
  createdBy: 'company_member',
  stateVersion: 2,
  needsMaterialization: false,
  materialized: true,
  updatedAt: '2026-07-24T00:00:00Z',
};

const reviewB: CompanyKnowledgeReview = {
  ...reviewA,
  reviewKey: 'review-2',
  title: 'Security Runbook',
  stateVersion: 1,
};

const platformRule: CompanyKnowledgeAccessRule = {
  permissionKey: 'permission-1',
  audience: 'Platform administrators',
  resource: 'All Company Knowledge',
  capabilities: ['find_and_read'],
  effect: 'allow',
  sensitivity: 'personal_data',
  active: true,
  expiresAt: null,
};

let queuedReviews: CompanyKnowledgeReview[] = [];
let accessRules: CompanyKnowledgeAccessRule[] = [];
let workspaceMarker = 'workspace-snapshot-v1';

function workspaceFor(review: CompanyKnowledgeReview, marker: string): CompanyKnowledgeReviewWorkspace {
  return {
    ...review,
    expectedCandidateHash: 'candidate-hash',
    evidenceRefs: [],
    candidateTitle: review.title,
    candidateMarkdown: 'Reviewed content.',
    reason: marker,
  };
}

function renderPlane() {
  const queryClient = new QueryClient({
    defaultOptions: {
      queries: { retry: false },
      mutations: { retry: false },
    },
  });
  return render(
    <QueryClientProvider client={queryClient}>
      <MemoryRouter>
        <CompanyKnowledgeControlPlane />
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

beforeEach(() => {
  vi.clearAllMocks();
  queuedReviews = [reviewA];
  accessRules = [];
  workspaceMarker = 'workspace-snapshot-v1';
  api.listIntakes.mockResolvedValue([]);
  api.listSourceContracts.mockResolvedValue([]);
  api.listCompanyImportJobs.mockResolvedValue([]);
  api.listLegacyCandidates.mockResolvedValue({ candidates: [], excludedSymlinkCount: 0 });
  api.listReviews.mockImplementation(() => Promise.resolve(queuedReviews));
  api.getReviewWorkspace.mockImplementation((review: CompanyKnowledgeReview) =>
    Promise.resolve(workspaceFor(review, workspaceMarker)),
  );
  api.listAccessRules.mockImplementation(() => Promise.resolve(accessRules));
  api.grantAccess.mockResolvedValue(undefined);
  api.revokeAccess.mockResolvedValue(undefined);
  vi.mocked(agentApi.list).mockResolvedValue([]);
  vi.mocked(enterpriseApi.getOrgMembers).mockResolvedValue([]);
});

afterEach(() => {
  cleanup();
});

describe('Company Knowledge control plane — mounted authority views', () => {
  it('exposes the exact role:platform_admin audience and a successful grant refreshes the mounted review queue and selected workspace', async () => {
    renderPlane();

    // Review lane: the queue and the selected workspace are mounted views.
    fireEvent.click(screen.getByRole('button', { name: 'Review & publish' }));
    await screen.findByRole('heading', { name: 'Employee Handbook' });
    await screen.findByText('workspace-snapshot-v1');
    const reviewCallsBeforeGrant = api.listReviews.mock.calls.length;

    // Access lane: the audience selector must offer the platform administrator
    // role with the exact backend-supported key; nothing is granted before the
    // operator explicitly saves the rule.
    fireEvent.click(screen.getByRole('button', { name: 'Access' }));
    const platformOption = (await screen.findByRole('option', {
      name: 'Platform administrators',
    })) as HTMLOptionElement;
    const audienceSelect = platformOption.closest('select');
    expect(audienceSelect).not.toBeNull();
    expect(api.grantAccess).not.toHaveBeenCalled();

    fireEvent.change(audienceSelect as HTMLSelectElement, { target: { value: platformOption.value } });
    // Server-side effect of the upcoming grant: the review queue gains an item
    // and the selected workspace snapshot advances.
    queuedReviews = [reviewA, reviewB];
    workspaceMarker = 'workspace-snapshot-v2';
    fireEvent.click(screen.getByRole('button', { name: 'Save access rule' }));

    await waitFor(() => expect(api.grantAccess).toHaveBeenCalledTimes(1));
    expect(api.grantAccess.mock.calls[0]?.[0]).toEqual(
      expect.objectContaining({
        audience: expect.objectContaining({ kind: 'role', key: 'role:platform_admin' }),
      }),
    );

    // The mounted review queue and the still-selected workspace refetch in
    // place, without any reload.
    await waitFor(() => expect(api.listReviews.mock.calls.length).toBeGreaterThan(reviewCallsBeforeGrant));
    await waitFor(() => expect(api.getReviewWorkspace.mock.calls.length).toBeGreaterThan(1));

    // Back on the review lane the mounted views already show the new truth.
    fireEvent.click(screen.getByRole('button', { name: 'Review & publish' }));
    await screen.findByText('Security Runbook');
    await screen.findByText('workspace-snapshot-v2');
    expect(screen.queryByText('workspace-snapshot-v1')).toBeNull();
  });

  it('a successful revoke refreshes the mounted review queue and clears the selected workspace', async () => {
    accessRules = [platformRule];
    renderPlane();

    fireEvent.click(screen.getByRole('button', { name: 'Review & publish' }));
    await screen.findByRole('heading', { name: 'Employee Handbook' });
    await screen.findByText('workspace-snapshot-v1');

    fireEvent.click(screen.getByRole('button', { name: 'Access' }));
    await screen.findByRole('button', { name: 'Remove access' });
    fireEvent.change(screen.getByRole('textbox'), {
      target: { value: 'Re-scoping review authority' },
    });

    // Server-side effect of the upcoming revoke: the submitted review is no
    // longer authorized for this operator.
    queuedReviews = [];
    fireEvent.click(screen.getByRole('button', { name: 'Remove access' }));

    await waitFor(() =>
      expect(api.revokeAccess).toHaveBeenCalledWith('permission-1', 'Re-scoping review authority'),
    );
    await waitFor(() => expect(api.listReviews).toHaveBeenCalledTimes(2));
    await waitFor(() => expect(api.getReviewWorkspace).toHaveBeenCalledTimes(2));

    fireEvent.click(screen.getByRole('button', { name: 'Review & publish' }));
    await screen.findByText('No authorized review items are waiting for you.');
    await screen.findByText('Select an item to review its business content.');
    expect(screen.queryByText('Employee Handbook')).toBeNull();
  });
});
