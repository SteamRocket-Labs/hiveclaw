// @vitest-environment jsdom
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { afterEach, expect, it, vi } from 'vitest';
import { ArtifactPreviewPanel } from './ArtifactSurface';
import { fileApi } from '../../api/domains/files';
import en from '../../i18n/en.json';
import { translateFromCatalog } from '../../test/i18nMock';

const t = (...args: Parameters<typeof translateFromCatalog> extends [unknown, ...infer T] ? T : never) => translateFromCatalog(en, ...args);
vi.mock('react-i18next', () => ({ useTranslation: () => ({ t: (...args: Parameters<typeof t>) => t(...args) }) }));
vi.mock('../../components/MarkdownRenderer', () => ({ default: ({ content }: { content: string }) => <pre>{content}</pre> }));
afterEach(() => { cleanup(); vi.restoreAllMocks(); });
const preview = { artifact: { id: 'delivery', name: 'report.md', path: 'workspace/report.md', downloadAgentId: 'employee' },
  content: 'Delivered version', usingSnapshot: true };

it('edits the current employee file from its delivery card and preserves the draft across preview switches', async () => {
  const read = vi.spyOn(fileApi, 'read').mockResolvedValue({ path: preview.artifact.path, content: 'Saved version', content_hash: 'a'.repeat(64) });
  const save = vi.spyOn(fileApi, 'writeIfUnchanged').mockResolvedValue({ content_hash: 'b'.repeat(64) });
  const versions = vi.spyOn(fileApi, 'versions').mockResolvedValue({ path: preview.artifact.path,
    current: { exists: true, content_hash: 'a'.repeat(64), size: 13 }, versions: [], total: 0,
    offset: 0, limit: 20, has_more: false, coverage_complete: false });
  render(<ArtifactPreviewPanel preview={preview} agentId="coordinator" onClose={vi.fn()} t={t as never} />);
  expect(screen.getByText('Delivered version')).toBeTruthy();
  expect(read).not.toHaveBeenCalled();
  fireEvent.click(screen.getByRole('button', { name: 'Open current file' }));
  await screen.findByText('Saved version');
  expect(read).toHaveBeenCalledWith('employee', 'workspace/report.md', undefined);
  fireEvent.click(screen.getByRole('button', { name: 'Version history' }));
  await waitFor(() => expect(versions).toHaveBeenCalledWith('employee', 'workspace/report.md', 0, 20, undefined));
  fireEvent.click(screen.getAllByRole('button', { name: 'Close' }).at(-1)!);
  fireEvent.click(screen.getByRole('button', { name: 'Edit' }));
  fireEvent.change(screen.getByRole('textbox'), { target: { value: 'Human revision' } });
  fireEvent.click(screen.getByRole('button', { name: 'Back to delivery' }));
  expect(screen.getByText('Delivered version')).toBeTruthy();
  fireEvent.click(screen.getByRole('button', { name: 'Open current file' }));
  expect((screen.getByRole('textbox') as HTMLTextAreaElement).value).toBe('Human revision');
  fireEvent.click(screen.getByRole('button', { name: 'Save' }));
  await waitFor(() => expect(save).toHaveBeenCalledWith('employee', 'workspace/report.md', 'Human revision', 'a'.repeat(64)));
  fireEvent.click(screen.getByRole('button', { name: 'Back to delivery' }));
  expect(screen.getByText('Delivered version')).toBeTruthy();
});

it('keeps operator current-file reads audited and editing unavailable', async () => {
  const read = vi.spyOn(fileApi, 'read').mockResolvedValue({ path: preview.artifact.path, content: 'Audited current version', content_hash: 'a'.repeat(64) });
  const save = vi.spyOn(fileApi, 'writeIfUnchanged');
  vi.spyOn(fileApi, 'versions').mockResolvedValue({ path: preview.artifact.path,
    current: { exists: true, content_hash: 'a'.repeat(64), size: 13 }, versions: [{ version_id: 'v1',
      created_at: '2026-10-18T00:00:00Z', state: 'available', size: 7, content_hash: 'b'.repeat(64), restorable: true }],
    total: 1, offset: 0, limit: 20, has_more: false, coverage_complete: true });
  vi.spyOn(fileApi, 'readVersion').mockResolvedValue({ path: preview.artifact.path, version_id: 'v1',
    state: 'available', content: 'History version', content_hash: 'b'.repeat(64), size: 7, is_binary: false });
  render(<ArtifactPreviewPanel preview={preview} agentId="coordinator" workspaceAuthority={{ operatorView: true, reason: 'Case review' }}
    onClose={vi.fn()} t={t as never} />);
  fireEvent.click(screen.getByRole('button', { name: 'Open current file' }));
  await screen.findByText('Audited current version');
  expect(read).toHaveBeenCalledWith('employee', 'workspace/report.md', { operatorView: true, reason: 'Case review' });
  expect(screen.queryByRole('button', { name: 'Edit' })).toBeNull();
  fireEvent.click(screen.getByRole('button', { name: 'Version history' }));
  fireEvent.click(await screen.findByRole('button', { name: /Available/ }));
  await screen.findByText('History version');
  expect(screen.queryByRole('button', { name: 'Restore this version' })).toBeNull();
  expect(save).not.toHaveBeenCalled();
});
