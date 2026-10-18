// @vitest-environment jsdom
import { useState } from 'react';
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { afterEach, expect, it, vi } from 'vitest';
import { ProjectContextPicker, type SelectedProjectContext } from './ProjectContextPicker';
import { projectsApi, type ProjectDetail } from '../../api/domains/projects';
import en from '../../i18n/en.json';
import { translateFromCatalog } from '../../test/i18nMock';

vi.mock('react-i18next', () => ({ useTranslation: () => ({ t: (...args: any[]) => translateFromCatalog(en, ...args as [string]) }) }));
afterEach(() => { cleanup(); vi.restoreAllMocks(); });
const project: ProjectDetail = { id: 'project', title: 'Research', revision: 1, owner_user_id: 'user', archived: false,
  can_manage: true, can_write: true, next_offset: 1, entries: [{ id: 'file', target_kind: 'workspace_file', section: '',
    removed: false, state: 'linked', title: 'Saved source' }] };
function Mounted() {
  const [selection, setSelection] = useState<SelectedProjectContext | null>(null);
  return <><ProjectContextPicker scope={{ userId: 'user', tenantId: 'company' }} disabled={false}
    selection={selection} onSelect={setSelection} /><output>{JSON.stringify(selection)}</output></>;
}

it('retains explicit selections across pagination and never auto-selects new project entries', async () => {
  vi.spyOn(projectsApi, 'list').mockResolvedValue({ projects: [project], next_offset: null });
  vi.spyOn(projectsApi, 'read').mockResolvedValueOnce(project).mockResolvedValueOnce({ ...project, next_offset: null,
    entries: [{ ...project.entries[0], id: 'work', title: 'Original work', target_kind: 'session' },
      { ...project.entries[0], id: 'denied', state: 'denied', title: undefined }] });
  render(<Mounted />);
  fireEvent.click(screen.getByRole('button', { name: 'Project context' }));
  await screen.findByRole('option', { name: 'Research' });
  fireEvent.change(screen.getByRole('combobox'), { target: { value: 'project' } });
  fireEvent.click(await screen.findByRole('checkbox', { name: 'Saved source' }));
  fireEvent.click(screen.getByRole('button', { name: 'Load more' }));
  await screen.findByRole('checkbox', { name: 'Original work' });
  expect((screen.getByRole('checkbox', { name: 'Saved source' }) as HTMLInputElement).checked).toBe(true);
  expect((screen.getByRole('checkbox', { name: 'Original work' }) as HTMLInputElement).checked).toBe(false);
  expect((screen.getByRole('checkbox', { name: 'Source unavailable' }) as HTMLInputElement).disabled).toBe(true);
  expect(screen.getByText('1 project references selected')).toBeTruthy();
  fireEvent.click(screen.getByRole('checkbox', { name: 'Original work' }));
  await waitFor(() => expect(screen.getByText('2 project references selected')).toBeTruthy());
});

it('ignores a response delivered after the source picker is unmounted', async () => {
  let resolve!: (value: { projects: typeof project[]; next_offset: null }) => void;
  vi.spyOn(projectsApi, 'list').mockImplementation(() => new Promise(r => { resolve = r; }));
  const selected = vi.fn();
  const mounted = render(<ProjectContextPicker scope={{ userId: 'user', tenantId: 'company' }} disabled={false}
    selection={null} onSelect={selected} />);
  fireEvent.click(screen.getByRole('button', { name: 'Project context' }));
  mounted.unmount(); resolve({ projects: [project], next_offset: null });
  await Promise.resolve();
  expect(selected).not.toHaveBeenCalled();
});
