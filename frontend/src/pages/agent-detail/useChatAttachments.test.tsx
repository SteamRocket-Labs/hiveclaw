// @vitest-environment jsdom
import type React from 'react';
import type { TFunction } from 'i18next';
import { act, cleanup, renderHook } from '@testing-library/react';
import { afterEach, expect, it, vi } from 'vitest';

import { uploadFileWithProgress } from '../../api/core/upload-progress';
import { useChatAttachments } from './useChatAttachments';

vi.mock('../../api/core/upload-progress', () => ({ uploadFileWithProgress: vi.fn() }));
const t = ((key: string, fallback?: string) => fallback || key) as TFunction;
afterEach(() => { cleanup(); vi.resetAllMocks(); });

function pendingUpload() {
  let resolve!: (value: unknown) => void;
  let reject!: (reason: Error) => void;
  const promise = new Promise((yes, no) => { resolve = yes; reject = no; });
  const abort = vi.fn();
  vi.mocked(uploadFileWithProgress).mockReturnValueOnce({ promise, abort });
  return { resolve, reject, abort };
}

function selection(files: File[]) {
  return { target: { files } } as unknown as React.ChangeEvent<HTMLInputElement>;
}

const uploaded = (filename: string) => ({ filename, extracted_text: 'sales data', workspace_path: `workspace/uploads/${filename}` });

it('shows an indeterminate upload until the response and then attaches the file', async () => {
  const request = pendingUpload();
  const { result } = renderHook(() => useChatAttachments('agent-1', t, vi.fn()));
  let uploading!: Promise<void>;
  act(() => { uploading = result.current.handleChatFile(selection([new File(['sales'], 'sales.csv')])); });
  expect(result.current.uploading).toBe(true);
  expect(result.current.uploadProgress).toBeGreaterThan(100);
  expect(result.current.attachedFiles).toEqual([]);
  await act(async () => { request.resolve(uploaded('sales.csv')); await uploading; });
  expect(result.current.uploading).toBe(false);
  expect(result.current.uploadProgress).toBe(-1);
  expect(result.current.attachedFiles).toMatchObject([{ name: 'sales.csv', path: 'workspace/uploads/sales.csv' }]);
});

it.each(['picker', 'paste'] as const)('never appends a cancelled %s upload, even if it resolves after another upload starts', async (source) => {
  const request = pendingUpload();
  const toast = vi.fn();
  const { result } = renderHook(() => useChatAttachments('agent-1', t, toast));
  const file = new File(['image'], 'cancelled.png', { type: 'image/png' });
  let cancelled!: Promise<void>;
  act(() => {
    cancelled = source === 'picker' ? result.current.handleChatFile(selection([file]))
      : result.current.handlePaste({ clipboardData: { items: [{ type: file.type, getAsFile: () => file }] },
        preventDefault: vi.fn() } as unknown as React.ClipboardEvent<HTMLTextAreaElement>);
  });
  act(() => result.current.uploadAbortRef.current?.());
  expect(request.abort).toHaveBeenCalledOnce();
  expect(result.current.uploading).toBe(false);
  const next = pendingUpload();
  let nextUploading!: Promise<void>;
  act(() => { nextUploading = result.current.handleChatFile(selection([new File(['next'], 'next.csv')])); });
  await act(async () => { request.resolve(uploaded('cancelled.png')); await cancelled; });
  expect(result.current.attachedFiles).toEqual([]);
  expect(result.current.uploading).toBe(true);
  await act(async () => { next.resolve(uploaded('next.csv')); await nextUploading; });
  expect(result.current.attachedFiles.map(file => file.name)).toEqual(['next.csv']);
  expect(toast).not.toHaveBeenCalled();
});

it('keeps sending paused until concurrent uploads finish and counts in-flight files toward the ten-file limit', async () => {
  const requests = Array.from({ length: 10 }, pendingUpload);
  const toast = vi.fn();
  const { result } = renderHook(() => useChatAttachments('agent-1', t, toast));
  let first!: Promise<void>;
  let second!: Promise<void>;
  act(() => { first = result.current.handleChatFile(selection([new File(['0'], '0.csv')])); });
  act(() => { second = result.current.handleChatFile(selection(Array.from({ length: 9 }, (_, i) => new File(['x'], `${i + 1}.csv`)))); });
  await act(async () => { await result.current.handleChatFile(selection([new File(['extra'], 'extra.csv')])); });
  expect(uploadFileWithProgress).toHaveBeenCalledTimes(10);
  await act(async () => { requests[0].resolve(uploaded('0.csv')); await first; });
  expect(result.current.uploading).toBe(true);
  await act(async () => { requests.slice(1).forEach((request, i) => request.resolve(uploaded(`${i + 1}.csv`))); await second; });
  expect(result.current.uploading).toBe(false);
  expect(result.current.attachedFiles).toHaveLength(10);
  expect(toast).toHaveBeenCalledWith('Limit of 10 attached files reached.', 'error');
});

it('aborts the rest of a failed batch before allowing another send', async () => {
  const failed = pendingUpload();
  const pending = pendingUpload();
  const toast = vi.fn();
  const { result } = renderHook(() => useChatAttachments('agent-1', t, toast));
  let uploading!: Promise<void>;
  act(() => { uploading = result.current.handleChatFile(selection([new File(['a'], 'a.csv'), new File(['b'], 'b.csv')])); });
  await act(async () => { failed.reject(new Error('Upload network error')); await uploading; });
  expect(pending.abort).toHaveBeenCalledOnce();
  expect(result.current.uploading).toBe(false);
  expect(result.current.attachedFiles).toEqual([]);
  expect(toast).toHaveBeenCalledWith('agent.upload.failed', 'error');
  await act(async () => pending.resolve(uploaded('b.csv')));
  expect(result.current.attachedFiles).toEqual([]);
});
