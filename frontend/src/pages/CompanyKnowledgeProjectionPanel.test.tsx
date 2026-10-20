// @vitest-environment jsdom
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { afterEach, beforeEach, expect, it, vi } from 'vitest';
import { companyKnowledgeApi, type CompanyProjectionView } from '../api/domains/companyKnowledge';
import CompanyKnowledgeProjectionPanel from './CompanyKnowledgeProjectionPanel';
import '../i18n';

vi.mock('../api/domains/companyKnowledge', () => ({ companyKnowledgeApi: {
  getProjection: vi.fn(), backfillProjection: vi.fn(), confirmProjection: vi.fn(),
} }));
const api = vi.mocked(companyKnowledgeApi);
let view: CompanyProjectionView;
function mount() {
  return render(<QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } })}>
    <CompanyKnowledgeProjectionPanel publicationKey="publication-current" />
  </QueryClientProvider>);
}
beforeEach(() => {
  vi.clearAllMocks();
  view = { publication_id: 'publication-current', content_hash: 'source-current', status: 'pending_types',
    job: { id: 'analysis-current', status: 'completed', error_code: null, can_retry: false, model_execution: 'not_in_progress' },
    candidate_hash: 'candidate-current', candidate: {
      entities: [{ key: 'instrument', type_ref: 'not_installed.Instrument', display_name: 'Source candidate', properties: { notes: 'Complete source detail' } }],
      assertions: [], links: [], covered_source_refs: ['evidence-source'], unresolved_conflicts: [], resolved_conflicts: [], duplicate_candidates: [], unresolved_questions: [],
    }, can_manage: true, schemas: [{ activation_id: 'schema-current', namespace: 'project', entity_types: ['project.Owner'], property_refs: ['project.name'], link_types: [] }] };
  api.getProjection.mockImplementation(async () => view);
  api.confirmProjection.mockImplementation(async () => { view = { ...view, status: 'active' }; });
  api.backfillProjection.mockResolvedValue(undefined);
});
afterEach(cleanup);

it('requires exact manual type mapping and a reason, then consumes the refreshed server status', async () => {
  mount();
  expect(api.getProjection).not.toHaveBeenCalled();
  fireEvent.click(screen.getByRole('button', { name: 'Knowledge graph' }));
  await screen.findByText('Source candidate · not_installed.Instrument');
  fireEvent.change(screen.getByLabelText('Installed schema'), { target: { value: 'schema-current' } });
  const button = screen.getByRole('button', { name: 'Confirm types and build graph' });
  expect((button as HTMLButtonElement).disabled).toBe(true);
  fireEvent.change(screen.getByLabelText('not_installed.Instrument'), { target: { value: 'project.Owner' } });
  fireEvent.change(screen.getByLabelText('notes'), { target: { value: 'project.name' } });
  expect((button as HTMLButtonElement).disabled).toBe(true);
  fireEvent.change(screen.getByLabelText('Reason for this schema confirmation'), { target: { value: 'Confirmed against installed definitions' } });
  fireEvent.click(button);
  await waitFor(() => expect(api.confirmProjection).toHaveBeenCalledTimes(1));
  expect(api.confirmProjection.mock.calls[0]).toEqual([
    expect.objectContaining({ publication_id: 'publication-current', content_hash: 'source-current', candidate_hash: 'candidate-current' }),
    'schema-current', { entity_types: { 'not_installed.Instrument': 'project.Owner' }, property_refs: { notes: 'project.name' }, link_types: {} },
    'Confirmed against installed definitions',
  ]);
  await screen.findByText('The approved graph projection is available.');
});

it('keeps unknown model execution read-only and never queues a second request', async () => {
  view = { ...view, status: 'analysis_queued', candidate: null, schemas: [],
    job: { id: 'analysis-current', status: 'held', error_code: 'company_analysis_provider_execution_unknown', can_retry: false, model_execution: 'unknown' } };
  mount();
  fireEvent.click(screen.getByRole('button', { name: 'Knowledge graph' }));
  await screen.findByText(/The model request has an unknown outcome/);
  expect(screen.queryByRole('button', { name: 'Retry analysis safely' })).toBeNull();
  expect(screen.queryByRole('button', { name: 'Analyze this existing publication' })).toBeNull();
  fireEvent.click(screen.getByRole('button', { name: 'Refresh analysis' }));
  await waitFor(() => expect(api.getProjection.mock.calls.length).toBeGreaterThan(1));
  expect(api.backfillProjection).not.toHaveBeenCalled();
  expect(api.confirmProjection).not.toHaveBeenCalled();
});

it('explains an output budget hold without automatically requesting more generation', async () => {
  view = { ...view, status: 'analysis_queued', candidate: null, schemas: [],
    job: { id: 'analysis-current', status: 'held', error_code: 'company_analysis_output_incomplete', can_retry: true, model_execution: 'not_in_progress' } };
  mount();
  fireEvent.click(screen.getByRole('button', { name: 'Knowledge graph' }));
  await screen.findByText(/Analysis reached the configured output budget/);
  expect(api.backfillProjection).not.toHaveBeenCalled();
  expect(api.confirmProjection).not.toHaveBeenCalled();
  expect(screen.getByRole('button', { name: 'Retry analysis safely' })).toBeTruthy();
});
