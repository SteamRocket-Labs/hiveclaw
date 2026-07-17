import { describe, expect, it } from 'vitest';

import {
  beginConnectionAttempt,
  buildSessionSubscribeMessage,
  connectionClosed,
  createSessionConnectionState,
  parseSessionReady,
  receiveSessionReady,
} from './sessionConnectionStore';

describe('Session connection generation and ready fence', () => {
  it('uses initializing before the first ready and reconnecting only afterwards', () => {
    let state = beginConnectionAttempt(createSessionConnectionState(), 'attempt-1');
    expect(state.transport.phase).toBe('initializing');
    expect(connectionClosed(state, 'attempt-1', true).transport.phase).toBe('initializing');
    state = receiveSessionReady(state, 'attempt-1', 'subscription-1', 4);
    state = connectionClosed(state, 'attempt-1', true);
    state = beginConnectionAttempt(state, 'attempt-2');
    expect(state.transport.phase).toBe('reconnecting');
  });

  it('ignores stale ready and close callbacks from an older StrictMode socket', () => {
    let state = beginConnectionAttempt(createSessionConnectionState(), 'attempt-1');
    state = beginConnectionAttempt(state, 'attempt-2');
    const staleReady = receiveSessionReady(state, 'attempt-1', 'stale-subscription', 9);
    const staleClose = connectionClosed(staleReady, 'attempt-1', true);
    expect(staleClose).toBe(state);
    expect(staleClose.transport.phase).toBe('initializing');
  });

  it('subscribes from the highest-contiguous cursor and binds ready to the active attempt', () => {
    expect(buildSessionSubscribeMessage('session-1', 7, 'attempt-2')).toEqual({
      type: 'session.subscribe',
      session_id: 'session-1',
      after_sequence: 7,
      schema_version: 2,
      connection_attempt_id: 'attempt-2',
    });
    expect(parseSessionReady({
      type: 'session.ready',
      session_id: 'session-1',
      subscription_id: 'subscription-2',
      accepted_after_sequence: 7,
      last_committed_sequence: 9,
      schema_version: 2,
      connection_attempt_id: 'attempt-2',
    }, 'session-1', 'attempt-2', 7)).toMatchObject({
      subscriptionId: 'subscription-2',
      lastCommittedSequence: 9,
    });
    expect(() => parseSessionReady({
      type: 'session.ready',
      session_id: 'session-1',
      subscription_id: 'subscription-stale',
      accepted_after_sequence: 7,
      last_committed_sequence: 9,
      schema_version: 2,
      connection_attempt_id: 'attempt-1',
    }, 'session-1', 'attempt-2', 7)).toThrow('session_ready_attempt_mismatch');
  });

  it('asks the server for a live-tail watermark when canonical history has no safe cursor yet', () => {
    expect(buildSessionSubscribeMessage('session-1', null, 'attempt-tail')).toEqual({
      type: 'session.subscribe',
      session_id: 'session-1',
      after_sequence: 0,
      cursor_mode: 'live_tail',
      schema_version: 2,
      connection_attempt_id: 'attempt-tail',
    });

    expect(parseSessionReady({
      type: 'session.ready',
      session_id: 'session-1',
      subscription_id: 'subscription-tail',
      accepted_after_sequence: 3200,
      last_committed_sequence: 3200,
      schema_version: 2,
      connection_attempt_id: 'attempt-tail',
    }, 'session-1', 'attempt-tail', null)).toMatchObject({
      subscriptionId: 'subscription-tail',
      acceptedAfterSequence: 3200,
      lastCommittedSequence: 3200,
      cursorMode: 'live_tail',
    });
  });
});
