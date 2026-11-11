// @vitest-environment jsdom
import { afterEach, expect, it, vi } from 'vitest';

import { uploadFileWithProgress } from './upload-progress';

afterEach(() => vi.unstubAllGlobals());

it('settles a cancelled upload so its caller can finish cancellation', async () => {
  const xhr = {
    open: vi.fn(), setRequestHeader: vi.fn(), send: vi.fn(), upload: {},
    onabort: null as (() => void) | null,
    abort: () => xhr.onabort?.(),
  };
  vi.stubGlobal('XMLHttpRequest', vi.fn(function () { return xhr; }));
  const request = uploadFileWithProgress('/chat/upload', new File(['sales'], 'sales.csv'));
  let failure: unknown;
  void request.promise.catch(error => { failure = error; });
  request.abort();
  await Promise.resolve();
  expect(failure).toEqual(new Error('Upload cancelled'));
});
