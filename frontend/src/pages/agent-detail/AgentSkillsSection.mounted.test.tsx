// @vitest-environment jsdom
import { cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { afterEach, expect, it, vi } from 'vitest';
import { extensionsApi } from '../../api/domains/extensions';
import { fileApi } from '../../api/domains/files';
import '../../i18n';
import AgentSkillsSection from './AgentSkillsSection';

vi.mock('../../api/domains/extensions', () => ({ extensionsApi: { getAgentExtensions: vi.fn() } }));
vi.mock('../../api/domains/files', () => ({ fileApi: { uninstallSkill: vi.fn() } }));
vi.mock('../../components/AppDialogs', () => ({ showAppToast: vi.fn() }));
afterEach(() => { cleanup(); vi.resetAllMocks(); });

it('confirms the exact workspace folder, not the display name, and refreshes installed state', async () => {
  vi.mocked(extensionsApi.getAgentExtensions).mockResolvedValueOnce({ skills: [
    { id: 'Display Name', name: 'Display Name', source: 'workspace', status: 'available', folder_name: 'exact-folder' },
    { id: 'external', name: 'External package', source: 'imported', status: 'installed' },
  ], mcp_servers: [] }).mockResolvedValue({ skills: [], mcp_servers: [] });
  vi.mocked(fileApi.uninstallSkill).mockResolvedValue({ status: 'uninstalled', folder_name: 'exact-folder', files_removed: 1 });
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  render(<QueryClientProvider client={queryClient}><AgentSkillsSection agentId="agent-1" /></QueryClientProvider>);
  const button = await screen.findByRole('button', { name: 'Uninstall workspace copy' });
  expect(screen.getAllByRole('button', { name: 'Uninstall workspace copy' })).toHaveLength(1);
  fireEvent.click(button);
  expect(fileApi.uninstallSkill).not.toHaveBeenCalled();
  expect(screen.getByRole('dialog').textContent).toContain('skills/exact-folder');
  fireEvent.click(within(screen.getByRole('dialog')).getByRole('button', { name: 'Confirm' }));
  await waitFor(() => expect(fileApi.uninstallSkill).toHaveBeenCalledWith('agent-1', 'exact-folder'));
  await screen.findByText('No installed skills.');
  expect(fileApi.uninstallSkill).toHaveBeenCalledTimes(1);
  queryClient.clear();
});
