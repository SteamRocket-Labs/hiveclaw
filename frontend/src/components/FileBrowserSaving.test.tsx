// @vitest-environment jsdom
import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { afterEach, expect, it, vi } from 'vitest';
import FileBrowser, { type FileBrowserApi } from './FileBrowser';
import { ApiError } from '../api/core/errors';
import en from '../i18n/en.json';
import { translateFromCatalog } from '../test/i18nMock';

vi.mock('react-i18next', () => ({ useTranslation: () => ({ t: (...args: Parameters<typeof translateFromCatalog> extends [unknown, ...infer T] ? T : never) => translateFromCatalog(en, ...args) }) }));
vi.mock('./MarkdownRenderer', () => ({ default: ({ content }: { content: string }) => <pre>{content}</pre> }));
afterEach(cleanup);

function fixture() {
  return {
    list: vi.fn(async () => [{ name: 'report.md', path: 'workspace/report.md', is_dir: false }]),
    read: vi.fn(async () => ({ content: 'Original', content_hash: 'a'.repeat(64) })),
    write: vi.fn(async () => ({})),
    writeIfUnchanged: vi.fn(async () => ({ content_hash: 'b'.repeat(64) })),
    delete: vi.fn(async () => ({})),
  };
}

async function edit(api: FileBrowserApi) {
  render(<FileBrowser api={api} singleFile="workspace/report.md" />);
  await screen.findByText('Original');
  fireEvent.click(screen.getByRole('button', { name: 'Edit' }));
  fireEvent.change(screen.getByRole('textbox'), { target: { value: 'Local draft' } });
}

it('retains a conflicting draft and requires an explicit read before a merged save', async () => {
  const api = fixture();
  await edit(api);
  api.writeIfUnchanged.mockRejectedValueOnce(new ApiError(409, 'Changed', { code: 'workspace_content_conflict' }));
  fireEvent.click(screen.getByRole('button', { name: 'Save' }));
  await screen.findByText('The file has changed. Your draft is kept. Load the current version to compare before saving again.');
  expect((screen.getByRole('textbox') as HTMLTextAreaElement).value).toBe('Local draft');
  expect(api.write).not.toHaveBeenCalled();
  expect(api.read).toHaveBeenCalledTimes(1);
  expect((screen.getByRole('button', { name: 'Save' }) as HTMLButtonElement).disabled).toBe(true);
  api.read.mockResolvedValueOnce({ content: 'Other saved revision', content_hash: 'c'.repeat(64) });
  fireEvent.click(screen.getByRole('button', { name: 'Load current version' }));
  await screen.findByText('Other saved revision');
  expect((screen.getByRole('textbox') as HTMLTextAreaElement).value).toBe('Local draft');
  fireEvent.change(screen.getByRole('textbox'), { target: { value: 'Merged revision' } });
  fireEvent.click(screen.getByRole('button', { name: 'Save' }));
  await screen.findByText('Merged revision');
  expect(api.writeIfUnchanged).toHaveBeenLastCalledWith('workspace/report.md', 'Merged revision', 'c'.repeat(64));
});

it('keeps text typed while a save is pending and uses the saved hash for the next save', async () => {
  const api = fixture();
  await edit(api);
  let finish!: (value: { content_hash: string }) => void;
  api.writeIfUnchanged.mockImplementationOnce(() => new Promise(resolve => { finish = resolve; }));
  fireEvent.click(screen.getByRole('button', { name: 'Save' }));
  fireEvent.change(screen.getByRole('textbox'), { target: { value: 'Newer unsaved draft' } });
  await act(async () => finish({ content_hash: 'b'.repeat(64) }));
  expect((screen.getByRole('textbox') as HTMLTextAreaElement).value).toBe('Newer unsaved draft');
  fireEvent.click(screen.getByRole('button', { name: 'Save' }));
  await waitFor(() => expect(api.writeIfUnchanged).toHaveBeenLastCalledWith('workspace/report.md', 'Newer unsaved draft', 'b'.repeat(64)));
});

it('ignores a save that completes after navigation to another file', async () => {
  const api = fixture();
  const view = render(<FileBrowser api={api} singleFile="workspace/report.md" />);
  await screen.findByText('Original');
  fireEvent.click(screen.getByRole('button', { name: 'Edit' }));
  let finish!: (value: { content_hash: string }) => void;
  api.writeIfUnchanged.mockImplementationOnce(() => new Promise(resolve => { finish = resolve; }));
  fireEvent.click(screen.getByRole('button', { name: 'Save' }));
  api.read.mockResolvedValueOnce({ content: 'Other file', content_hash: 'c'.repeat(64) });
  view.rerender(<FileBrowser api={api} singleFile="workspace/other.md" />);
  await screen.findByText('Other file');
  await act(async () => finish({ content_hash: 'b'.repeat(64) }));
  expect(screen.getByText('Other file')).toBeTruthy();
});

it('keeps an uncertain save blocked when the current read is denied', async () => {
  const api = fixture();
  await edit(api);
  api.writeIfUnchanged.mockRejectedValueOnce(new ApiError(503, 'Unconfirmed', { outcome: 'unknown' }));
  fireEvent.click(screen.getByRole('button', { name: 'Save' }));
  await screen.findByText('The save could not be confirmed. Your draft is kept. Load the current version before saving again.');
  api.read.mockRejectedValueOnce(new ApiError(403, 'Revoked'));
  fireEvent.click(screen.getByRole('button', { name: 'Load current version' }));
  await screen.findByText('The file could not be read. Check your access and try again.');
  expect((screen.getByRole('textbox') as HTMLTextAreaElement).value).toBe('Local draft');
  expect((screen.getByRole('button', { name: 'Save' }) as HTMLButtonElement).disabled).toBe(true);
  expect(api.writeIfUnchanged).toHaveBeenCalledTimes(1);
});

it('ignores a slow read from the file left behind', async () => {
  const api = fixture();
  let finish!: (value: { content: string; content_hash: string }) => void;
  api.read.mockImplementationOnce(() => new Promise(resolve => { finish = resolve; }));
  const view = render(<FileBrowser api={api} singleFile="workspace/report.md" />);
  expect((screen.getByRole('button', { name: 'Edit' }) as HTMLButtonElement).disabled).toBe(true);
  api.read.mockResolvedValueOnce({ content: 'Other file', content_hash: 'c'.repeat(64) });
  view.rerender(<FileBrowser api={api} singleFile="workspace/other.md" />);
  await screen.findByText('Other file');
  await act(async () => finish({ content: 'Late old content', content_hash: 'a'.repeat(64) }));
  expect(screen.queryByText('Late old content')).toBeNull();
  fireEvent.click(screen.getByRole('button', { name: 'Edit' }));
  fireEvent.change(screen.getByRole('textbox'), { target: { value: 'Updated other file' } });
  fireEvent.click(screen.getByRole('button', { name: 'Save' }));
  await waitFor(() => expect(api.writeIfUnchanged).toHaveBeenLastCalledWith('workspace/other.md', 'Updated other file', 'c'.repeat(64)));
});

it('opens a newly created file for editing after reading its saved baseline', async () => {
  const api = fixture();
  api.read.mockResolvedValue({ content: '', content_hash: 'c'.repeat(64) });
  render(<FileBrowser api={api} rootPath="workspace" features={{ newFile: true }} />);
  fireEvent.click(screen.getByRole('button', { name: /New File/ }));
  fireEvent.change(screen.getByPlaceholderText('filename.md'), { target: { value: 'new.md' } });
  fireEvent.click(screen.getByRole('button', { name: 'OK' }));
  const draft = await screen.findByRole('textbox', { name: 'Your draft' });
  fireEvent.change(draft, { target: { value: 'New result' } });
  fireEvent.click(screen.getByRole('button', { name: 'Save' }));
  await waitFor(() => expect(api.writeIfUnchanged).toHaveBeenLastCalledWith('workspace/new.md', 'New result', 'c'.repeat(64)));
});
