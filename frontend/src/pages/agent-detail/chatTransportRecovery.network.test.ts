import { afterEach, expect, it, vi } from 'vitest';
import { get } from '../../api/core/request';
import { retrySessionRead } from './chatTransportRecovery';

afterEach(() => vi.unstubAllGlobals());

it('retries a real request-layer network failure and returns the recovered session read', async () => {
  vi.stubGlobal('localStorage', { getItem: () => null });
  const fetch = vi.fn().mockRejectedValueOnce(new TypeError('Failed to fetch'))
    .mockResolvedValueOnce(new Response(JSON.stringify([{ id: 'recovered-message' }])));
  vi.stubGlobal('fetch', fetch);
  const wait = vi.fn().mockResolvedValue(undefined);
  await expect(retrySessionRead(() => get('/agents/bot/sessions/work/transcript'), { wait }))
    .resolves.toEqual([{ id: 'recovered-message' }]);
  expect(fetch).toHaveBeenCalledTimes(2);
  expect(wait).toHaveBeenCalledOnce();
});

it('does not retry a request-layer abort', async () => {
  vi.stubGlobal('localStorage', { getItem: () => null });
  const abort = new DOMException('Read aborted', 'AbortError');
  const fetch = vi.fn().mockRejectedValue(abort);
  vi.stubGlobal('fetch', fetch);
  const wait = vi.fn().mockResolvedValue(undefined);
  await expect(retrySessionRead(() => get('/agents/bot/sessions/work/transcript'), { wait })).rejects.toBe(abort);
  expect(fetch).toHaveBeenCalledOnce();
  expect(wait).not.toHaveBeenCalled();
});
