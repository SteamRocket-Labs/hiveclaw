// @vitest-environment jsdom
import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { afterEach, beforeEach, expect, it, vi } from 'vitest';
import i18next from 'i18next';
import { I18nextProvider } from 'react-i18next';
import { ArtifactPreviewPanel } from './ArtifactSurface';
import { fileApi } from '../../api/domains/files';
import { officeApi, type OfficeContentModel } from '../../api/domains/office';
import { ApiError } from '../../api/core';
import en from '../../i18n/en.json';

const base: OfficeContentModel = { path: 'workspace/human.docx', kind: 'docx', content_hash: 'a'.repeat(64),
  fields: [{ node_path: '/body/p[1]', value: 'Original office text', name: '' }] };
const originalURL = URL;
beforeEach(() => {
  vi.stubGlobal('URL', class extends originalURL {
    static createObjectURL = vi.fn(() => 'blob:owned-office-preview');
    static revokeObjectURL = vi.fn();
  });
  vi.spyOn(officeApi, 'getWorkspacePreview').mockResolvedValue(new Blob(['<html>Office preview</html>'], { type: 'text/html' }));
  vi.spyOn(officeApi, 'readContent').mockResolvedValue(base);
  vi.spyOn(fileApi, 'read').mockRejectedValue(new Error('Office bytes must not enter the text reader'));
});
afterEach(() => { cleanup(); vi.restoreAllMocks(); vi.unstubAllGlobals(); });

async function mount(operator = false) {
  const language = i18next.createInstance();
  await language.init({ lng: 'en', resources: { en: { translation: en } }, interpolation: { escapeValue: false } });
  const send = vi.fn().mockResolvedValue(undefined);
  render(<I18nextProvider i18n={language}><ArtifactPreviewPanel agentId="employee"
    preview={{ artifact: { id: 'delivery', name: 'human.docx', path: base.path }, url: 'blob:delivery', usingSnapshot: true }}
    onClose={vi.fn()} t={language.t as never} continuation={{ sessionId: 'original-work', send }}
    workspaceAuthority={operator ? { operatorView: true, reason: 'Inspect office source' } : undefined} /></I18nextProvider>);
  fireEvent.click(screen.getByRole('button', { name: 'Open current file' }));
  await screen.findByTitle(base.path);
  return send;
}

it('edits the current Office carrier through the real workspace adapter and preserves a newer draft', async () => {
  let finish!: (value: { status: string; path: string; content_hash: string }) => void;
  const save = vi.spyOn(officeApi, 'writeContent').mockReturnValue(new Promise(resolve => { finish = resolve; }));
  officeApi.readContent = vi.mocked(officeApi.readContent).mockResolvedValueOnce(base).mockResolvedValue({ ...base,
    content_hash: 'b'.repeat(64), fields: [{ ...base.fields[0], node_path: '/body/p[@paraId=12]', value: 'Submitted office edit' }] });
  const send = await mount();
  expect((screen.getByTitle(base.path) as HTMLIFrameElement).getAttribute('sandbox')).toBe('');
  fireEvent.change(screen.getByRole('textbox', { name: 'Continue with this file' }), { target: { value: 'Continue saved Office' } });
  expect((screen.getByRole('button', { name: 'Continue in this work' }) as HTMLButtonElement).disabled).toBe(false);
  fireEvent.click(screen.getByRole('button', { name: 'Edit contents' }));
  const field = await screen.findByRole('textbox', { name: 'Text block 1' });
  fireEvent.change(field, { target: { value: 'Submitted office edit' } });
  const continuation = screen.getByRole('button', { name: 'Continue in this work' }) as HTMLButtonElement;
  await waitFor(() => expect(continuation.disabled).toBe(true));
  fireEvent.click(screen.getByRole('button', { name: 'Save' }));
  await waitFor(() => expect(save).toHaveBeenCalledWith('employee', base.path, {
    expected_current_hash: base.content_hash, changes: [{ node_path: '/body/p[1]', value: 'Submitted office edit' }] }));
  fireEvent.change(field, { target: { value: 'Newer office edit' } });
  await act(async () => { finish({ status: 'ok', path: base.path, content_hash: 'b'.repeat(64) }); });
  expect(screen.getByDisplayValue('Newer office edit')).toBeTruthy();
  expect(screen.getByDisplayValue('Submitted office edit')).toBeTruthy();
  expect(continuation.disabled).toBe(true);
  expect(fileApi.read).not.toHaveBeenCalled();
  expect(send).not.toHaveBeenCalled();
  fireEvent.click(screen.getByRole('button', { name: 'Discard this retained edit' }));
  await waitFor(() => expect(continuation.disabled).toBe(false));
  fireEvent.click(continuation);
  await waitFor(() => expect(send).toHaveBeenCalledOnce());
  expect(send.mock.calls[0][0]).not.toContain('Submitted office edit');
  expect(send.mock.calls[0][0]).not.toContain('Newer office edit');
});

it('keeps conflicting edits until the current saved fields are reviewed and explicitly rebased', async () => {
  vi.spyOn(officeApi, 'writeContent').mockRejectedValue(new ApiError(409, 'Saved version changed'));
  vi.mocked(officeApi.readContent).mockResolvedValueOnce(base).mockResolvedValue({ ...base, content_hash: 'c'.repeat(64),
    fields: [{ ...base.fields[0], value: 'Another writer correction' }] });
  await mount();
  fireEvent.click(screen.getByRole('button', { name: 'Edit contents' }));
  const field = await screen.findByRole('textbox', { name: 'Text block 1' });
  fireEvent.change(field, { target: { value: 'My retained correction' } });
  fireEvent.click(screen.getByRole('button', { name: 'Save' }));
  await screen.findByText(en.agent.workspace.fileSave.conflict);
  expect((field as HTMLTextAreaElement).value).toBe('My retained correction');
  expect((screen.getByRole('button', { name: 'Save' }) as HTMLButtonElement).disabled).toBe(true);
  fireEvent.click(screen.getByRole('button', { name: 'Load current version' }));
  await screen.findByText(/Another writer correction/);
  fireEvent.click(screen.getByRole('button', { name: 'Apply my edits to this version' }));
  expect((field as HTMLTextAreaElement).value).toBe('My retained correction');
  expect((screen.getByRole('button', { name: 'Save' }) as HTMLButtonElement).disabled).toBe(false);
});

it('purges cached Office fields when the content grant is denied', async () => {
  vi.spyOn(officeApi, 'writeContent').mockRejectedValue(new ApiError(403, 'Source access revoked'));
  vi.mocked(officeApi.readContent).mockResolvedValueOnce(base).mockRejectedValue(new ApiError(403, 'Source access revoked'));
  await mount();
  fireEvent.click(screen.getByRole('button', { name: 'Edit contents' }));
  const field = await screen.findByRole('textbox', { name: 'Text block 1' });
  fireEvent.change(field, { target: { value: 'Private source correction' } });
  fireEvent.click(screen.getByRole('button', { name: 'Save' }));
  await screen.findByText(en.agent.workspace.office.accessChanged);
  expect(screen.queryByDisplayValue('Private source correction')).toBeNull();
  expect(screen.queryByDisplayValue('Original office text')).toBeNull();
});

it('keeps an authorized local draft when only save permission is denied', async () => {
  vi.spyOn(officeApi, 'writeContent').mockRejectedValue(new ApiError(403, 'Save access denied'));
  await mount();
  fireEvent.click(screen.getByRole('button', { name: 'Edit contents' }));
  const field = await screen.findByRole('textbox', { name: 'Text block 1' });
  fireEvent.change(field, { target: { value: 'Retained local draft' } });
  fireEvent.click(screen.getByRole('button', { name: 'Save' }));
  await waitFor(() => expect(officeApi.writeContent).toHaveBeenCalledOnce());
  await waitFor(() => expect(officeApi.readContent).toHaveBeenCalledTimes(2));
  expect(screen.getByDisplayValue('Retained local draft')).toBeTruthy();
  expect((screen.getByRole('button', { name: 'Save' }) as HTMLButtonElement).disabled).toBe(true);
});

it('offers the same Office renderer without editing in operator inspection', async () => {
  const save = vi.spyOn(officeApi, 'writeContent');
  await mount(true);
  expect(screen.queryByRole('button', { name: 'Edit contents' })).toBeNull();
  expect(screen.queryByRole('button', { name: 'Continue in this work' })).toBeNull();
  expect(officeApi.readContent).not.toHaveBeenCalled();
  expect(save).not.toHaveBeenCalled();
  expect(officeApi.getWorkspacePreview).toHaveBeenCalledWith('employee', base.path,
    { operatorView: true, reason: 'Inspect office source' });
});
