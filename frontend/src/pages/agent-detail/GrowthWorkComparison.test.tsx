/** @vitest-environment jsdom */
import React from 'react';
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { MemoryRouter } from 'react-router-dom';
import { afterEach, describe, expect, it, vi } from 'vitest';
import en from '../../i18n/en.json';
import { translateFromCatalog } from '../../test/i18nMock';
import { knowledgeApi, type GrowthWorkComparison, type GrowthWorkResult } from '../../api/domains/knowledge';
import GrowthWorkComparisonCard from './GrowthWorkComparison';

vi.mock('react-i18next', () => ({ useTranslation: () => ({
  t: (key: string) => translateFromCatalog(en, key),
}) }));
vi.mock('../../api/domains/knowledge', () => ({ knowledgeApi: { observability: vi.fn() } }));

const text = en.agent.evolution.workComparison;
function work(id: string): GrowthWorkResult {
  return { run_id: id, session_id: `session-${id}`, status: 'completed', result: `RESULT_${id}`,
    work_url: `/agents/agent/sessions/session-${id}`, duration_seconds: 1.5,
    usage: { effective_tokens: 20, recorded_rounds: 1 },
    cost: { amount: null, currency: null, status: 'not_recorded' }, human_corrections: null,
    feedback: { useful: id === 'candidate' ? 1 : 0, misleading: 0 },
    learning_sources: [], request_hashes: [`different-request-hash-${id}`] };
}
const comparison: GrowthWorkComparison = { schema: 'hive.growth_work_comparison.v1',
  interpretation: 'observations_only', status: 'insufficient_evidence', baseline: work('baseline'), candidate: work('candidate'),
  conditions: [{ key: 'model', status: 'same', baseline: 'controlled-model', candidate: 'controlled-model' },
    { key: 'budget', status: 'unknown', baseline: null, candidate: null }] };
const samples = { items: ['baseline', 'candidate'].map(id => ({ run_id: id, session_id: `session-${id}`,
  source_channel: 'web', completed_at: '2026-10-21T10:00:00Z' })), next_cursor: null };

afterEach(() => { cleanup(); vi.resetAllMocks(); });

function mount() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  render(<QueryClientProvider client={client}><MemoryRouter>
    <GrowthWorkComparisonCard agentId="agent" active />
  </MemoryRouter></QueryClientProvider>);
  return client;
}

describe('recorded work comparison', () => {
  it('selects original runs through the existing growth API and displays unknown metrics and authorized work links', async () => {
    vi.mocked(knowledgeApi.observability).mockImplementation(async (_agent, options) => ({
      debt: {}, debt_history: [], label_axes: {}, growth: options?.includeWorkSamples ? { work_samples: samples } : { work_comparison: comparison },
    }));
    const client = mount();
    await screen.findAllByRole('option', { name: /baseline/ });
    fireEvent.change(screen.getByLabelText(text.baseline), { target: { value: 'baseline' } });
    fireEvent.change(screen.getByLabelText(text.candidate), { target: { value: 'candidate' } });
    await screen.findByText('RESULT_candidate');
    expect(knowledgeApi.observability).toHaveBeenCalledWith('agent', { baselineRunId: 'baseline', candidateRunId: 'candidate' });
    expect(screen.getAllByText(text.unknown).length).toBeGreaterThanOrEqual(4);
    expect(screen.getByText(text.observations)).toBeTruthy();
    expect(screen.getAllByRole('link', { name: text.openWork }).map(link => link.getAttribute('href')))
      .toEqual(['/agents/agent/sessions/session-baseline', '/agents/agent/sessions/session-candidate']);
    vi.mocked(knowledgeApi.observability).mockRejectedValue(new Error('Current work access could not be verified'));
    await client.invalidateQueries({ queryKey: ['growth-work-comparison'] });
    await screen.findByRole('alert');
    expect(screen.queryByText('RESULT_candidate')).toBeNull();
    cleanup(); client.clear();
  });

  it('keeps same-run selection local and can page work metadata without running a comparison', async () => {
    vi.mocked(knowledgeApi.observability).mockImplementation(async (_agent, options) => ({
      debt: {}, debt_history: [], label_axes: {}, growth: { work_samples: options?.beforeRunId
        ? { items: [{ ...samples.items[0], run_id: 'older' }], next_cursor: null }
        : { ...samples, next_cursor: 'candidate' } },
    }));
    const client = mount();
    await screen.findAllByRole('option', { name: /baseline/ });
    fireEvent.change(screen.getByLabelText(text.baseline), { target: { value: 'baseline' } });
    fireEvent.change(screen.getByLabelText(text.candidate), { target: { value: 'baseline' } });
    expect(screen.getByText(text.chooseDifferent)).toBeTruthy();
    fireEvent.click(screen.getByRole('button', { name: text.more }));
    await waitFor(() => expect(knowledgeApi.observability).toHaveBeenCalledWith('agent', {
      includeWorkSamples: true, beforeRunId: 'candidate',
    }));
    await screen.findAllByRole('option', { name: /older/ });
    expect(vi.mocked(knowledgeApi.observability).mock.calls.every(([, options]) => options?.includeWorkSamples)).toBe(true);
    cleanup(); client.clear();
  });
});
