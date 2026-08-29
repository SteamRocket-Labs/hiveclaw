import { describe, expect, it } from 'vitest';

import {
  chatTransportPhase,
  isRetryableSessionReadError,
  mergeTranscriptBackfill,
  reconnectDelayMs,
  retrySessionRead,
  shouldReconnectSessionSocket,
  transportPollIntervalMs,
} from './chatTransportRecovery';

describe('chat transport recovery policy', () => {
  it('keeps retrying transient session reads until durable history is available', async () => {
    let reads = 0;
    const waits: number[] = [];
    const retries: Array<{ attempt: number; delayMs: number }> = [];

    const result = await retrySessionRead(async () => {
      reads += 1;
      if (reads < 3) throw { status: 504 };
      return ['durable-message'];
    }, {
      randomValue: () => 0,
      wait: async (delayMs) => { waits.push(delayMs); },
      onRetry: (retry) => { retries.push(retry); },
    });

    expect(result).toEqual(['durable-message']);
    expect(reads).toBe(3);
    expect(waits).toEqual([1000, 2000]);
    expect(retries).toEqual([
      { attempt: 1, delayMs: 1000 },
      { attempt: 2, delayMs: 2000 },
    ]);
  });

  it('retries network and server failures but never retries authority failures', async () => {
    expect(isRetryableSessionReadError(new TypeError('Failed to fetch'))).toBe(true);
    expect(isRetryableSessionReadError({ status: 408 })).toBe(true);
    expect(isRetryableSessionReadError({ status: 429 })).toBe(true);
    expect(isRetryableSessionReadError({ status: 502 })).toBe(true);
    expect(isRetryableSessionReadError({ status: 504 })).toBe(true);
    expect(isRetryableSessionReadError({ status: 403 })).toBe(false);
    expect(isRetryableSessionReadError({ status: 404 })).toBe(false);

    let reads = 0;
    await expect(retrySessionRead(async () => {
      reads += 1;
      throw { status: 403 };
    }, { wait: async () => undefined })).rejects.toEqual({ status: 403 });
    expect(reads).toBe(1);
  });

  it('turns a session switch into an explicit abort instead of a visible read failure', async () => {
    const controller = new AbortController();
    const read = retrySessionRead(async () => {
      controller.abort();
      throw { status: 504 };
    }, { signal: controller.signal, wait: async () => undefined });

    await expect(read).rejects.toMatchObject({ name: 'AbortError' });
  });

  it('retries forever with capped exponential backoff instead of a terminal attempt count', () => {
    expect(reconnectDelayMs(0, 0)).toBe(1000);
    expect(reconnectDelayMs(5, 1)).toBe(60000);
    expect(reconnectDelayMs(200, 1)).toBe(60000);
    expect(reconnectDelayMs(200, 0)).toBe(30000);
  });

  it('reconnects after a server normal close unless the client explicitly disposed the socket', () => {
    expect(shouldReconnectSessionSocket(1000, false)).toBe(true);
    expect(shouldReconnectSessionSocket(1000, true)).toBe(false);
    expect(shouldReconnectSessionSocket(1011, false)).toBe(true);
  });

  it('never reconnects an authentication or authorization close', () => {
    for (const code of [4002, 4003, 4401, 4403]) {
      expect(shouldReconnectSessionSocket(code, false)).toBe(false);
    }
  });

  it('separates offline, reconnecting, degraded, connected, and auth-failed states', () => {
    expect(chatTransportPhase({ online: false, connected: false, attempts: 99, everReady: false })).toBe('offline');
    expect(chatTransportPhase({ online: true, connected: true, attempts: 99, everReady: true })).toBe('connected');
    expect(chatTransportPhase({ online: true, connected: false, attempts: 0, everReady: false })).toBe('initializing');
    expect(chatTransportPhase({ online: true, connected: false, attempts: 2, everReady: true })).toBe('reconnecting');
    expect(chatTransportPhase({ online: true, connected: false, attempts: 5, everReady: false })).toBe('degraded');
    expect(chatTransportPhase({ online: true, connected: false, attempts: 0, everReady: false, authFailed: true })).toBe('auth_failed');
  });

  it('polls durable backfill only while the network is available and realtime is degraded', () => {
    expect(transportPollIntervalMs('connected', true)).toBeNull();
    expect(transportPollIntervalMs('auth_failed', true)).toBeNull();
    expect(transportPollIntervalMs('offline', true)).toBeNull();
    expect(transportPollIntervalMs('reconnecting', true)).toBe(3000);
    expect(transportPollIntervalMs('degraded', true)).toBe(3000);
    expect(transportPollIntervalMs('degraded', false)).toBe(10000);
  });

  it('merges reconnect backfill by durable identity and sequence without replaying duplicates', () => {
    const merged = mergeTranscriptBackfill(
      [
        { id: 'event-2', sequence: 2, event_type: 'tool_call' },
        { id: 'event-1', sequence: 1, event_type: 'user_message' },
      ],
      [
        { id: 'event-2-durable', sequence: 2, event_type: 'tool_call' },
        { id: 'event-3', sequence: 3, event_type: 'assistant_message' },
      ],
    );

    expect(merged.map((event) => event.id)).toEqual(['event-1', 'event-2-durable', 'event-3']);
  });
});
