// @vitest-environment jsdom
import { act, cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { afterEach, beforeEach, expect, it, vi } from 'vitest';
import { MemoryRouter, Route, Routes } from 'react-router-dom';
import { ApiError } from '../api/core';
import { agentApi } from '../api/domains/agents';
import { fileApi } from '../api/domains/files';
import { projectsApi, type ProjectDetail } from '../api/domains/projects';
import Projects, { ProjectWorkbench } from './Projects';
import { useAuthStore } from '../stores';
import type { User } from '../types';
import en from '../i18n/en.json';
import { translateFromCatalog } from '../test/i18nMock';

vi.mock('react-i18next', () => ({ useTranslation: () => ({ t: (...args: any[]) => translateFromCatalog(en, ...args as [string]) }) }));
vi.mock('../components/MarkdownRenderer', () => ({ default: ({ content }: { content: string }) => <pre>{content}</pre> }));
const scope = { userId: 'owner', tenantId: 'company' };
const base: ProjectDetail = { id: 'project', title: 'Research project', revision: 3, owner_user_id: 'owner',
  can_manage: true, can_write: true, archived: false, next_offset: null, grants: [], entries: [
    { id: 'file', target_kind: 'workspace_file', section: 'Evidence', removed: false, state: 'linked',
      agent_id: 'employee', path: 'workspace/report.md', title: 'report.md' },
    { id: 'work', target_kind: 'session', section: 'Work', removed: false, state: 'linked',
      agent_id: 'employee', session_id: 'original-work', title: 'Original work' }] };
beforeEach(() => {
  vi.spyOn(projectsApi, 'list').mockResolvedValue({ projects: [base], next_offset: null });
  vi.spyOn(projectsApi, 'read').mockResolvedValue(base);
  vi.spyOn(agentApi, 'list').mockResolvedValue([]);
  vi.spyOn(fileApi, 'read').mockResolvedValue({ path: 'workspace/report.md', content: 'Actual saved version', content_hash: 'a'.repeat(64) });
});
afterEach(() => { cleanup(); vi.restoreAllMocks(); useAuthStore.getState().logout(); });
function mount() {
  return render(<MemoryRouter initialEntries={['/projects/project']}><Routes><Route path="/projects/:projectId"
    element={<ProjectWorkbench scope={scope} projectId="project" />} /></Routes></MemoryRouter>);
}

it('opens the canonical file editor and original work while retaining active file edits', async () => {
  const save = vi.spyOn(fileApi, 'writeIfUnchanged').mockResolvedValue({ content_hash: 'b'.repeat(64) });
  mount();
  const work = await screen.findByRole('link', { name: 'Open original work' });
  expect(work.getAttribute('href')).toBe('/agents/employee/sessions/original-work');
  fireEvent.click(screen.getByRole('button', { name: 'Open current file' }));
  const file = await screen.findByRole('region', { name: 'Current canonical file' });
  await within(file).findByText('Actual saved version');
  expect(fileApi.read).toHaveBeenCalledWith('employee', 'workspace/report.md', undefined);
  fireEvent.click(within(file).getByRole('button', { name: 'Edit' }));
  fireEvent.change(within(file).getByRole('textbox'), { target: { value: 'Human project correction' } });
  await screen.findByText(en.projects.finishEditing);
  fireEvent.click(screen.getByRole('button', { name: 'Open current file' }));
  expect(screen.getByText(en.projects.finishEditing)).toBeTruthy();
  fireEvent.click(within(file).getByRole('button', { name: 'Save' }));
  await waitFor(() => expect(save).toHaveBeenCalledWith('employee', 'workspace/report.md', 'Human project correction', 'a'.repeat(64)));
  await within(file).findByText('Human project correction');
});

it('keeps a conflicting title until the current project is read and the draft is explicitly saved', async () => {
  const edit = vi.spyOn(projectsApi, 'edit').mockRejectedValueOnce(new ApiError(409, 'Revision changed', { outcome: 'not_applied' })).mockResolvedValue({});
  mount();
  const name = await screen.findByRole('textbox', { name: 'Project name' });
  fireEvent.change(name, { target: { value: 'My retained title' } });
  fireEvent.click(screen.getByRole('button', { name: 'Save' }));
  await screen.findByText(en.projects.conflict);
  vi.mocked(projectsApi.read).mockResolvedValue({ ...base, title: 'Another title', revision: 4 });
  fireEvent.click(screen.getByRole('button', { name: 'Refresh' }));
  await screen.findByRole('heading', { name: 'Another title' });
  expect((screen.getByRole('textbox', { name: 'Project name' }) as HTMLInputElement).value).toBe('My retained title');
  fireEvent.click(screen.getByRole('button', { name: 'Save' }));
  await waitFor(() => expect(edit).toHaveBeenLastCalledWith(scope, 'project', { expected_revision: 4, title: 'My retained title' }));
});

it('does not turn a confirmed project write into an unknown effect when its subsequent read fails', async () => {
  vi.spyOn(projectsApi, 'edit').mockResolvedValue({ id: 'project', revision: 4 });
  vi.mocked(projectsApi.read).mockResolvedValueOnce(base).mockRejectedValue(new ApiError(503, 'Read unavailable'));
  mount();
  const name = await screen.findByRole('textbox', { name: 'Project name' });
  fireEvent.change(name, { target: { value: 'Confirmed new title' } });
  await act(async () => { fireEvent.click(screen.getByRole('button', { name: 'Save' })); });
  expect(projectsApi.edit).toHaveBeenCalledOnce();
  expect(screen.queryByText(en.projects.unknown)).toBeNull();
  expect(screen.queryByRole('button', { name: 'Retry the same request' })).toBeNull();
  expect(screen.getByText('Read unavailable')).toBeTruthy();
});

it('preserves a title draft after write-only denial and purges source fields after read denial', async () => {
  vi.spyOn(projectsApi, 'edit').mockRejectedValue(new ApiError(403, 'Editing revoked'));
  mount();
  const name = await screen.findByRole('textbox', { name: 'Project name' });
  fireEvent.change(name, { target: { value: 'Retained authorized title' } });
  vi.mocked(projectsApi.read).mockResolvedValue({ ...base, can_manage: false, can_write: false });
  fireEvent.click(screen.getByRole('button', { name: 'Save' }));
  await screen.findByText(en.projects.writeDenied);
  expect(screen.getByDisplayValue('Retained authorized title')).toBeTruthy();
  vi.mocked(projectsApi.read).mockRejectedValue(new ApiError(403, 'Reading revoked'));
  fireEvent.click(screen.getByRole('button', { name: 'Refresh' }));
  await waitFor(() => expect(screen.queryByDisplayValue('Retained authorized title')).toBeNull());
  expect(screen.queryByText('report.md')).toBeNull();
  expect(screen.queryByRole('heading', { name: base.title })).toBeNull();
});

it('closes an open canonical editor when refreshed source authority is denied', async () => {
  mount();
  fireEvent.click(await screen.findByRole('button', { name: 'Open current file' }));
  await screen.findByText('Actual saved version');
  const file = screen.getByRole('region', { name: 'Current canonical file' });
  fireEvent.click(within(file).getByRole('button', { name: 'Edit' }));
  fireEvent.change(within(file).getByRole('textbox'), { target: { value: 'Revoked source draft' } });
  vi.mocked(projectsApi.read).mockResolvedValue({ ...base, entries: [
    { id: 'file', target_kind: 'workspace_file', section: 'Evidence', removed: false, state: 'denied' }] });
  fireEvent.click(screen.getByRole('button', { name: 'Refresh' }));
  await screen.findByText(en.projects.sourceDenied);
  expect(screen.queryByText('Actual saved version')).toBeNull();
  expect(screen.queryByText('report.md')).toBeNull();
  expect(screen.queryByDisplayValue('Revoked source draft')).toBeNull();
  await waitFor(() => expect(screen.getByRole('link', { name: base.title }).getAttribute('aria-disabled')).toBe('false'));
});

it('retains an open file draft when refreshing a different page of project references', async () => {
  const readEntry = vi.spyOn(projectsApi, 'readEntry').mockResolvedValue(base.entries[0]);
  mount();
  fireEvent.click(await screen.findByRole('button', { name: 'Open current file' }));
  const file = await screen.findByRole('region', { name: 'Current canonical file' });
  await within(file).findByText('Actual saved version');
  fireEvent.click(within(file).getByRole('button', { name: 'Edit' }));
  fireEvent.change(within(file).getByRole('textbox'), { target: { value: 'Draft on another reference page' } });
  vi.mocked(projectsApi.read).mockResolvedValue({ ...base, entries: [base.entries[1]], next_offset: 50 });
  fireEvent.click(screen.getByRole('button', { name: 'Refresh' }));
  await screen.findByRole('button', { name: 'Load more' });
  expect(screen.getByDisplayValue('Draft on another reference page')).toBeTruthy();
  expect(readEntry).toHaveBeenCalledWith(scope, 'project', 'file');
});

it('discards late project data after the authenticated account changes', async () => {
  let finish!: (value: ProjectDetail) => void;
  vi.mocked(projectsApi.read).mockImplementationOnce(() => new Promise(resolve => { finish = resolve; }))
    .mockRejectedValue(new ApiError(403, 'Other account'));
  vi.mocked(projectsApi.list).mockImplementation(async actor => ({ projects: actor.userId === 'owner' ? [base] : [], next_offset: null }));
  useAuthStore.getState().setAuth({ id: 'owner', tenant_id: 'company', role: 'member' } as User, 'synthetic-owner');
  render(<MemoryRouter initialEntries={['/projects/project']}><Routes><Route path="/projects/:projectId" element={<Projects />} /></Routes></MemoryRouter>);
  await waitFor(() => expect(projectsApi.read).toHaveBeenCalledOnce());
  await act(async () => { useAuthStore.getState().setAuth({ id: 'other', tenant_id: 'company', role: 'member' } as User, 'synthetic-other'); });
  await act(async () => { finish(base); });
  expect(screen.queryByRole('heading', { name: base.title })).toBeNull();
  expect(screen.queryByText(base.title)).toBeNull();
  expect(screen.queryByText('report.md')).toBeNull();
});
