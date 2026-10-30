// @vitest-environment jsdom
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { afterEach, expect, it, vi } from 'vitest';
import { ArtifactPreviewPanel } from './ArtifactSurface';
import en from '../../i18n/en.json';
import { translateFromCatalog } from '../../test/i18nMock';
import { fileApi } from '../../api/domains/files';
import { ApiError } from '../../api/core';

const t = (...args: Parameters<typeof translateFromCatalog> extends [unknown, ...infer T] ? T : never) => translateFromCatalog(en, ...args);
vi.mock('react-i18next', () => ({ useTranslation: () => ({ t: (...args: Parameters<typeof t>) => t(...args) }) }));
vi.mock('../../components/MarkdownRenderer', () => ({ default: ({ content }: { content: string }) => <pre>{content}</pre> }));
afterEach(() => { cleanup(); vi.restoreAllMocks(); });
const preview = { artifact: { id: 'delivery', name: 'report.md', path: 'workspace/report.md', downloadAgentId: 'employee' },
  content: 'Original delivery bytes', usingSnapshot: true };

it('continues the saved file in the original work without sending the preview bytes', async () => {
  const send = vi.fn().mockResolvedValue(undefined);
  render(<ArtifactPreviewPanel preview={preview} agentId="employee" onClose={vi.fn()} t={t as never}
    continuation={{ sessionId: 'original-work', send }} />);
  const input = screen.getByRole('textbox', { name: 'Continue with this file' });
  fireEvent.change(input, { target: { value: 'Revise the conclusion using my saved changes.' } });
  fireEvent.click(screen.getByRole('button', { name: 'Continue in this work' }));
  await waitFor(() => expect(send).toHaveBeenCalledTimes(1));
  const [content, options] = send.mock.calls[0];
  expect(content).toContain('workspace/report.md');
  expect(content).toContain('Revise the conclusion using my saved changes.');
  expect(content).not.toContain('Original delivery bytes');
  expect(options.expectedSessionId).toBe('original-work');
  expect(options.inputId).toMatch(/^[0-9a-f-]{36}$/);
});

it('keeps the draft and input identity after an unconfirmed response', async () => {
  const send = vi.fn().mockRejectedValueOnce(new Error('Response lost')).mockResolvedValue(undefined);
  render(<ArtifactPreviewPanel preview={preview} agentId="employee" onClose={vi.fn()} t={t as never}
    continuation={{ sessionId: 'original-work', send }} />);
  const input = screen.getByRole('textbox', { name: 'Continue with this file' });
  fireEvent.change(input, { target: { value: 'Use my saved changes.' } });
  fireEvent.click(screen.getByRole('button', { name: 'Continue in this work' }));
  await screen.findByText('Response lost');
  expect((input as HTMLTextAreaElement).value).toBe('Use my saved changes.');
  fireEvent.click(screen.getByRole('button', { name: 'Continue in this work' }));
  await waitFor(() => expect(send).toHaveBeenCalledTimes(2));
  expect(send.mock.calls[1]).toEqual(send.mock.calls[0]);
  await waitFor(() => expect((input as HTMLTextAreaElement).value).toBe(''));
});

it('keeps a newer draft when the previous input is recorded', async () => {
  let finish!: () => void;
  const send = vi.fn().mockReturnValue(new Promise<void>((resolve) => { finish = resolve; }));
  render(<ArtifactPreviewPanel preview={preview} agentId="employee" onClose={vi.fn()} t={t as never}
    continuation={{ sessionId: 'original-work', send }} />);
  const input = screen.getByRole('textbox', { name: 'Continue with this file' });
  fireEvent.change(input, { target: { value: 'First follow-up' } });
  fireEvent.click(screen.getByRole('button', { name: 'Continue in this work' }));
  fireEvent.change(input, { target: { value: 'Next follow-up draft' } });
  finish();
  await screen.findByText('Your input is recorded. Follow progress in this work.');
  expect((input as HTMLTextAreaElement).value).toBe('Next follow-up draft');
});

it('blocks unsaved, saving and conflicting file edits while keeping both drafts', async () => {
  vi.spyOn(fileApi, 'read').mockResolvedValue({ path: preview.artifact.path, content: 'Saved file', content_hash: 'a'.repeat(64) });
  let rejectSave!: (error: unknown) => void;
  const write = vi.spyOn(fileApi, 'writeIfUnchanged').mockReturnValue(new Promise((_resolve, reject) => { rejectSave = reject; }));
  const send = vi.fn();
  render(<ArtifactPreviewPanel preview={preview} agentId="employee" onClose={vi.fn()} t={t as never}
    continuation={{ sessionId: 'original-work', send }} />);
  const followUp = screen.getByRole('textbox', { name: 'Continue with this file' });
  fireEvent.change(followUp, { target: { value: 'Continue after saving.' } });
  fireEvent.click(screen.getByRole('button', { name: 'Open current file' }));
  await screen.findByText('Saved file');
  fireEvent.click(screen.getByRole('button', { name: 'Edit' }));
  const editor = screen.getByRole('textbox', { name: 'Your draft' });
  fireEvent.change(editor, { target: { value: 'Unsaved human revision' } });
  const submit = screen.getByRole('button', { name: 'Continue in this work' }) as HTMLButtonElement;
  await waitFor(() => expect(submit.disabled).toBe(true));
  fireEvent.click(screen.getByRole('button', { name: 'Save' }));
  await waitFor(() => expect(write).toHaveBeenCalledOnce());
  expect(submit.disabled).toBe(true);
  rejectSave(new ApiError(409, 'Another version was saved'));
  await screen.findByText(en.agent.workspace.fileSave.conflict);
  expect(submit.disabled).toBe(true);
  expect((editor as HTMLTextAreaElement).value).toBe('Unsaved human revision');
  expect((followUp as HTMLTextAreaElement).value).toBe('Continue after saving.');
  expect(send).not.toHaveBeenCalled();
});

it('does not offer a mutable file follow-up in operator inspection', () => {
  const send = vi.fn();
  render(<ArtifactPreviewPanel preview={preview} agentId="employee" onClose={vi.fn()} t={t as never}
    workspaceAuthority={{ operatorView: true, reason: 'Inspect delivery' }} continuation={{ sessionId: 'original-work', send }} />);
  expect(screen.queryByRole('textbox', { name: 'Continue with this file' })).toBeNull();
  expect(send).not.toHaveBeenCalled();
});

it('continues after an uncertain save is read back and explicitly saved successfully', async () => {
  const read = vi.spyOn(fileApi, 'read')
    .mockResolvedValueOnce({ path: preview.artifact.path, content: 'Saved file', content_hash: 'a'.repeat(64) })
    .mockResolvedValueOnce({ path: preview.artifact.path, content: 'Current saved file', content_hash: 'c'.repeat(64) });
  const write = vi.spyOn(fileApi, 'writeIfUnchanged')
    .mockRejectedValueOnce(new ApiError(503, 'Save not confirmed', { outcome: 'unknown' }))
    .mockResolvedValueOnce({ content_hash: 'b'.repeat(64) });
  const send = vi.fn().mockResolvedValue(undefined);
  render(<ArtifactPreviewPanel preview={preview} agentId="employee" onClose={vi.fn()} t={t as never}
    continuation={{ sessionId: 'original-work', send }} />);
  const followUp = screen.getByRole('textbox', { name: 'Continue with this file' });
  fireEvent.change(followUp, { target: { value: 'Continue with my saved revision.' } });
  fireEvent.click(screen.getByRole('button', { name: 'Open current file' }));
  await screen.findByText('Saved file');
  fireEvent.click(screen.getByRole('button', { name: 'Edit' }));
  const editor = screen.getByRole('textbox', { name: 'Your draft' });
  fireEvent.change(editor, { target: { value: 'Human revision' } });
  fireEvent.click(screen.getByRole('button', { name: 'Save' }));
  await screen.findByText(en.agent.workspace.fileSave.unknown);
  const submit = screen.getByRole('button', { name: 'Continue in this work' }) as HTMLButtonElement;
  expect(submit.disabled).toBe(true);
  expect((editor as HTMLTextAreaElement).value).toBe('Human revision');
  expect(write).toHaveBeenCalledTimes(1);
  expect(send).not.toHaveBeenCalled();
  fireEvent.click(screen.getByRole('button', { name: 'Load current version' }));
  await screen.findByText('Current saved file');
  expect(read).toHaveBeenCalledTimes(2);
  expect(write).toHaveBeenCalledTimes(1);
  expect(submit.disabled).toBe(true);
  expect((editor as HTMLTextAreaElement).value).toBe('Human revision');
  fireEvent.click(screen.getByRole('button', { name: 'Save' }));
  await waitFor(() => expect(screen.queryByRole('textbox', { name: 'Your draft' })).toBeNull());
  expect(write).toHaveBeenLastCalledWith('employee', preview.artifact.path, 'Human revision', 'c'.repeat(64));
  await waitFor(() => expect(submit.disabled).toBe(false));
  fireEvent.click(submit);
  await waitFor(() => expect(send).toHaveBeenCalledTimes(1));
  expect(send.mock.calls[0][1].expectedSessionId).toBe('original-work');
  expect(send.mock.calls[0][0]).toContain('"human_saved_hash":"' + 'b'.repeat(64) + '"');
  expect(send.mock.calls[0][0]).not.toContain('Human revision');
});

it('uses a new input for a newly acknowledged human file version after an uncertain request', async () => {
  vi.spyOn(fileApi, 'read').mockResolvedValue({ path: 'workspace/report.md', content: 'Saved file', content_hash: 'a'.repeat(64) });
  vi.spyOn(fileApi, 'writeIfUnchanged').mockResolvedValue({ content_hash: 'b'.repeat(64) });
  const send = vi.fn().mockRejectedValue(new Error('Acknowledgement lost'));
  render(<ArtifactPreviewPanel preview={preview} agentId="employee" onClose={vi.fn()} t={t as never}
    continuation={{ sessionId: 'original-work', send }} />);
  fireEvent.change(screen.getByRole('textbox', { name: 'Continue with this file' }), { target: { value: 'Continue this version.' } });
  fireEvent.click(screen.getByRole('button', { name: 'Continue in this work' }));
  await screen.findByText('Acknowledgement lost');
  fireEvent.click(screen.getByRole('button', { name: 'Open current file' }));
  await screen.findByText('Saved file');
  fireEvent.click(screen.getByRole('button', { name: 'Edit' }));
  fireEvent.change(screen.getByRole('textbox', { name: 'Your draft' }), { target: { value: 'New saved bytes' } });
  fireEvent.click(screen.getByRole('button', { name: 'Save' }));
  await waitFor(() => expect(screen.queryByRole('textbox', { name: 'Your draft' })).toBeNull());
  await waitFor(() => expect((screen.getByRole('button', { name: 'Continue in this work' }) as HTMLButtonElement).disabled).toBe(false));
  fireEvent.click(screen.getByRole('button', { name: 'Continue in this work' }));
  await waitFor(() => expect(send).toHaveBeenCalledTimes(2));
  expect(send.mock.calls[1][1].inputId).not.toBe(send.mock.calls[0][1].inputId);
  expect(send.mock.calls[1][0]).toContain('"human_saved_hash":"' + 'b'.repeat(64) + '"');
  expect(send.mock.calls[1][0]).not.toContain('New saved bytes');
});
