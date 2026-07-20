import { describe, expect, it } from 'vitest';

import {
  chatTransportPhase,
  mergeTranscriptBackfill,
  reconnectDelayMs,
  shouldReconnectSessionSocket,
  transportPollIntervalMs,
} from './chatTransportRecovery';

describe('chat transport recovery policy', () => {
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
