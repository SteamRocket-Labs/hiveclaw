// @vitest-environment jsdom
import { act, cleanup, render, screen, waitFor } from '@testing-library/react';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { afterEach, expect, it, vi } from 'vitest';
import TeamMemorySummaryCard from './TeamMemorySummaryCard';
import { memoryApi } from '../../api/domains/memory';
import { ApiError } from '../../api/core/errors';

vi.mock('../../api/domains/memory', () => ({ memoryApi: { listShared: vi.fn(), getShared: vi.fn() } }));
vi.mock('react-i18next', () => ({ useTranslation: () => ({ t: (key: string, fallback?: string | Record<string, unknown>) => typeof fallback === 'string' ? fallback : key }) }));
afterEach(cleanup);

it('withholds cached title, snippet and body after a detail read is denied', async () => {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  const entry = { tenant_id: 'tenant', workspace_key: 'workspace', key: 'report', title: 'Restricted title',
    updated_at: '', path: 'report.md', absolute_path: '/fixture/report.md', snippet: 'Restricted snippet', content: 'Restricted body' };
  vi.mocked(memoryApi.listShared).mockResolvedValue([entry]);
  vi.mocked(memoryApi.getShared).mockResolvedValue(entry);
  render(<QueryClientProvider client={client}><TeamMemorySummaryCard agentId="agent" section="aware" /></QueryClientProvider>);
  await screen.findByText('Restricted body');
  vi.mocked(memoryApi.getShared).mockRejectedValue(new ApiError(403, 'Source revoked'));
  await act(async () => { await client.invalidateQueries({ queryKey: ['team-memory-entry'] }); });
  await waitFor(() => {
    expect(screen.queryByText('Restricted body')).toBeNull();
    expect(screen.queryByText('Restricted snippet')).toBeNull();
    expect(screen.queryByText('Restricted title')).toBeNull();
  });
  expect(screen.getByRole('alert')).toBeTruthy();
  expect(client.getQueryData(['team-memory-entry', 'workspace', 'report'])).toBeUndefined();
  expect(client.getQueryData(['team-memory', 'workspace'])).toEqual([]);
  client.clear();
});
