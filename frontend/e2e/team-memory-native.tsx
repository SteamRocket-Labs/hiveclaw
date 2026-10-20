/** The mounted editor consumes the actual app-role PostgreSQL memory API. */
import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { I18nextProvider } from 'react-i18next';
import i18next from 'i18next';
import { expect, it } from 'vitest';
import TeamMemorySummaryCard from '../src/pages/agent-detail/TeamMemorySummaryCard';
import { useAuthStore } from '../src/stores';
import type { User } from '../src/types';
import { memoryApi } from '../src/api/domains/memory';
import en from '../src/i18n/en.json';

it('selects a real team, saves and reopens a revised note, then withholds it after withdrawal', async () => {
  const base = process.env.HIVE_G90_NATIVE_URL || '';
  const user = process.env.HIVE_G90_USER_ID || '';
  const tenant = process.env.HIVE_G90_TENANT_ID || '';
  const agent = process.env.HIVE_G90_AGENT_ID || '';
  const workspace = process.env.HIVE_M4E_WORKSPACE || '';
  expect(base.startsWith('http://127.0.0.1:')).toBe(true);
  expect(user && tenant && agent && workspace).toBeTruthy();
  const original = globalThis.fetch;
  globalThis.fetch = (url, options) => {
    if (typeof url !== 'string' || !url.startsWith('/api/')) throw new Error('Only the owned loopback API is permitted');
    return original(new URL(url, base), options);
  };
  const language = i18next.createInstance();
  await language.init({ lng: 'en', resources: { en: { translation: en } }, interpolation: { escapeValue: false } });
  useAuthStore.getState().setAuth({ id: user, tenant_id: tenant, role: 'member', username: 'Team member' } as User, 'synthetic-team-member');
  const client = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } });
  const mount = () => render(<I18nextProvider i18n={language}><QueryClientProvider client={client}>
    <TeamMemorySummaryCard agentId={agent} section="aware" />
  </QueryClientProvider></I18nextProvider>);
  async function choose() {
    const picker = await screen.findByLabelText('Team memory');
    await waitFor(() => expect(picker.querySelector(`option[value="${workspace}"]`)).toBeTruthy());
    fireEvent.change(picker, { target: { value: workspace } });
  }
  try {
    let view = mount();
    await choose();
    fireEvent.click(await screen.findByRole('button', { name: 'Edit' }));
    fireEvent.change(await screen.findByPlaceholderText('Entry key'), { target: { value: 'release' } });
    fireEvent.change(screen.getByPlaceholderText('Entry title'), { target: { value: 'Release cadence' } });
    fireEvent.change(screen.getByPlaceholderText('Write the shared note or playbook here'), { target: { value: 'NATIVE_TEAM_MEMORY_INITIAL' } });
    fireEvent.click(screen.getByRole('button', { name: 'Save to shared memory' }));
    await waitFor(() => expect(screen.getByText('NATIVE_TEAM_MEMORY_INITIAL', { selector: '.team-memory-detail-content' })).toBeTruthy());
    expect((await memoryApi.getShared('release', workspace, tenant)).revision).toBe(1);
    fireEvent.change(await screen.findByPlaceholderText('Write the shared note or playbook here'), { target: { value: 'NATIVE_TEAM_MEMORY_REVISED' } });
    fireEvent.click(screen.getByRole('button', { name: 'Save to shared memory' }));
    await waitFor(() => expect(screen.getByText('NATIVE_TEAM_MEMORY_REVISED', { selector: '.team-memory-detail-content' })).toBeTruthy());
    expect((await memoryApi.getShared('release', workspace, tenant)).revision).toBe(2);
    view.unmount();
    view = mount();
    await choose();
    await screen.findByText('NATIVE_TEAM_MEMORY_REVISED', { selector: '.team-memory-detail-content' });
    const response = await fetch('/api/__reference/revoke-team-memory', { method: 'POST' });
    expect(response.ok).toBe(true);
    await act(async () => { await client.invalidateQueries({ queryKey: ['team-memory-workspaces'] }); });
    await waitFor(() => {
      expect(screen.queryByText('NATIVE_TEAM_MEMORY_REVISED')).toBeNull();
      expect(screen.queryByText('Release cadence')).toBeNull();
    });
    expect((await memoryApi.listWorkspaces(agent, tenant)).workspaces.some(value => value.workspace_key === workspace)).toBe(false);
    view.unmount();
  } finally { cleanup(); client.clear(); globalThis.fetch = original; useAuthStore.getState().logout(); }
});
