/** Mounted project organization and canonical editing against the owned HTTP service. */
import { cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { MemoryRouter, Route, Routes } from 'react-router-dom';
import { I18nextProvider } from 'react-i18next';
import i18next from 'i18next';
import { expect, it } from 'vitest';
import Projects from '../src/pages/Projects';
import { projectsApi } from '../src/api/domains/projects';
import { chatApi } from '../src/api/domains/chat';
import { useAuthStore } from '../src/stores';
import type { User } from '../src/types';
import en from '../src/i18n/en.json';

it('reconciles a committed project creation and edits the original file before continuing its original work', async () => {
  const base = process.env.HIVE_G90_NATIVE_URL || '';
  const agent = process.env.HIVE_G90_AGENT_ID || '';
  const tenant = process.env.HIVE_G90_TENANT_ID || '';
  const user = process.env.HIVE_G90_USER_ID || '';
  const session = process.env.HIVE_PROJECT_SESSION_ID || '';
  expect(base.startsWith('http://127.0.0.1:')).toBe(true);
  expect(agent && tenant && user && session).toBeTruthy();
  const original = globalThis.fetch;
  let createCalls = 0;
  globalThis.fetch = async (input, options) => {
    if (typeof input !== 'string' || !input.startsWith('/api/')) throw new Error('Only the owned loopback API is permitted');
    const response = await original(new URL(input, base), options);
    if (options?.method === 'POST' && new URL(input, base).pathname === '/api/projects') {
      createCalls += 1;
      if (response.status === 201 && createCalls === 1) throw new Error('Synthetic lost ACK after real project creation');
    }
    return response;
  };
  const language = i18next.createInstance();
  await language.init({ lng: 'en', resources: { en: { translation: en } }, interpolation: { escapeValue: false } });
  useAuthStore.getState().setAuth({ id: user, tenant_id: tenant, role: 'member', username: 'Project owner' } as User, 'synthetic-project-owner');
  try {
    render(<I18nextProvider i18n={language}><MemoryRouter initialEntries={['/projects']}><Routes>
      <Route path="/projects" element={<Projects />} /><Route path="/projects/:projectId" element={<Projects />} />
    </Routes></MemoryRouter></I18nextProvider>);
    fireEvent.change(screen.getByRole('textbox', { name: 'New project name' }), { target: { value: 'Native project research' } });
    fireEvent.click(screen.getByRole('button', { name: 'Create project' }));
    await screen.findByText(en.projects.unknown);
    fireEvent.click(screen.getByRole('button', { name: 'Refresh' }));
    await screen.findByRole('heading', { name: 'Native project research' });
    expect(createCalls).toBe(1);
    const scope = { userId: user, tenantId: tenant };
    const listed = await projectsApi.list(scope);
    expect(listed.projects).toHaveLength(1);
    const project = listed.projects[0].id;
    const employees = screen.getByRole('combobox', { name: 'Employee' });
    await waitFor(() => expect(within(employees).getAllByRole('option').some(option => (option as HTMLOptionElement).value === agent)).toBe(true));
    fireEvent.change(employees, { target: { value: agent } });
    const fileRow = await screen.findByText('project.md');
    fireEvent.click(within(fileRow.closest('li')!).getByRole('button', { name: 'Add file reference' }));
    fireEvent.click(await screen.findByRole('button', { name: 'Open current file' }));
    const file = await screen.findByRole('region', { name: 'Current canonical file' });
    await within(file).findByText('PROJECT_INITIAL_NATIVE_BYTES');
    fireEvent.click(within(file).getByRole('button', { name: 'Edit' }));
    fireEvent.change(within(file).getByRole('textbox'), { target: { value: 'PROJECT_HUMAN_SAVED_决定性尾部' } });
    fireEvent.click(within(file).getByRole('button', { name: 'Save' }));
    await within(file).findByText('PROJECT_HUMAN_SAVED_决定性尾部');
    fireEvent.change(screen.getByRole('combobox', { name: 'Source type' }), { target: { value: 'session' } });
    fireEvent.click(await screen.findByRole('button', { name: 'Add work reference' }));
    const originalWork = await screen.findByRole('link', { name: 'Open original work' });
    expect(originalWork.getAttribute('href')).toBe(`/agents/${agent}/sessions/${session}`);
    const current = await projectsApi.read(scope, project);
    expect(current.entries).toHaveLength(2);
    expect(current.entries.find(entry => entry.target_kind === 'workspace_file')?.path).toBe('workspace/project.md');
    const work = current.entries.find(entry => entry.target_kind === 'session')!;
    const inputId = crypto.randomUUID();
    // The existing work route remains the conversation entry. This controlled
    // transport submits its next input through that route's real Session API.
    const input = { content: 'Read the saved correction from workspace/project.md and deliver a derived report in this work.',
      input_id: inputId, idempotency_key: `session-input:${inputId}`, permission_mode: 'bypassPermissions' as const };
    expect(input.content).not.toContain('PROJECT_HUMAN_SAVED');
    await chatApi.startSessionRun(work.agent_id!, work.session_id!, input);
    console.info(`HIVE_PROJECT_ID=${project}`);
    console.info(`HIVE_PROJECT_INPUT_ID=${inputId}`);
  } finally {
    cleanup(); globalThis.fetch = original; useAuthStore.getState().logout();
  }
});
