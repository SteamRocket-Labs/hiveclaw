// @vitest-environment jsdom
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { afterEach, beforeEach, expect, it, vi } from 'vitest';

import { fileApi } from '../../api/domains/files';
import en from '../../i18n/en.json';
import { translateFromCatalog } from '../../test/i18nMock';
import AgentMindSection from './AgentMindSection';

vi.mock('react-i18next', () => ({ useTranslation: () => ({
  t: (...args: Parameters<typeof translateFromCatalog> extends [unknown, ...infer T] ? T : never) => translateFromCatalog(en, ...args),
}) }));
vi.mock('../../components/MarkdownRenderer', () => ({ default: ({ content }: { content: string }) => <pre>{content}</pre> }));
vi.mock('../../api/domains/files', () => ({ fileApi: {
  list: vi.fn(), read: vi.fn(), write: vi.fn(), delete: vi.fn(), download: vi.fn(),
} }));

const clients: QueryClient[] = [];
beforeEach(() => vi.clearAllMocks());
afterEach(() => { cleanup(); clients.splice(0).forEach(client => client.clear()); });

it.each([
  { root: 'memory', folder: 'knowledge', name: 'entry.md' },
  { root: 'evolution', folder: 'snapshots', name: 'ledger.json' },
])('browses $root descendants and returns to its root without mutation controls', async ({ root, folder, name }) => {
  vi.mocked(fileApi.list).mockImplementation(async (_agent, path) => {
    if (path === root) return [{ name: folder, path: `${root}/${folder}`, is_dir: true, type: 'directory', size: 0 }];
    if (path === `${root}/${folder}`) return [{ name, path: `${root}/${folder}/${name}`, is_dir: false, type: 'file', size: 16 }];
    return [];
  });
  vi.mocked(fileApi.read).mockImplementation(async (_agent, path) => ({ path, content: path === 'soul.md' ? '' : 'Complete entry' }));
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  clients.push(client);
  render(<QueryClientProvider client={client}><AgentMindSection agentId="agent-1" canEdit /></QueryClientProvider>);

  fireEvent.click(await screen.findByText(folder, { exact: true }));
  await waitFor(() => expect(fileApi.list).toHaveBeenCalledWith('agent-1', `${root}/${folder}`));
  fireEvent.click(await screen.findByText(name, { exact: true }));
  await screen.findByText('Complete entry');
  expect(fileApi.read).toHaveBeenCalledWith('agent-1', `${root}/${folder}/${name}`);
  expect(screen.queryByRole('button', { name: /^Edit$/ })).toBeNull();
  expect(screen.queryByRole('button', { name: /^Save$/ })).toBeNull();
  expect(screen.queryByRole('button', { name: /^Delete$/ })).toBeNull();

  fireEvent.click(screen.getByRole('button', { name: /^← Back$/ }));
  await screen.findByText(name, { exact: true });
  fireEvent.click(screen.getByText(`📁 ${root}`, { exact: true }));
  await screen.findByText(folder, { exact: true });
  expect(vi.mocked(fileApi.list).mock.calls.every(([, path]) => ['memory', 'evolution', `${root}/${folder}`].includes(path || ''))).toBe(true);
  expect(fileApi.write).not.toHaveBeenCalled();
  expect(fileApi.delete).not.toHaveBeenCalled();
});
