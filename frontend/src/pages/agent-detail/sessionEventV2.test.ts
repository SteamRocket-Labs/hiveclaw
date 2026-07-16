import { describe, expect, it } from 'vitest';

import {
  createSessionEventStore,
  reduceSessionCompatibilityEvent,
  reduceSessionEvent,
  type SessionEventV2,
} from '../session-workbench/sessionEventStore';

function event(sequence: number, overrides: Partial<SessionEventV2> = {}): SessionEventV2 {
  const lifecycle = sequence === 1 ? 'started' : 'delta';
  return {
    schema: 'hive.session_event',
    schema_version: 2,
    event_id: `event-${sequence}`,
    sequence,
    ordinal: sequence - 1,
    tenant_id: 'tenant-1',
    scope: {
      level: 'round',
      session_id: 'session-1',
      thread_id: 'session-1',
      turn_id: 'turn-1',
      run_id: 'run-1',
      round_id: 'round-1',
    },
    item_id: 'item-1',
    item_kind: 'assistant_text',
    kind: `assistant_text.${lifecycle}`,
    lifecycle,
    payload_schema: `hive.session.payload.assistant_text.${lifecycle}.v2`,
    actor: { type: 'assistant' },
    visibility: { audience: 'direct_user' },
    payload: { content: sequence === 1 ? '' : String(sequence), phase: 'unknown' },
    occurred_at: '2026-07-16T00:00:00Z',
    persisted_at: '2026-07-16T00:00:00Z',
    ...overrides,
  } as SessionEventV2;
}

describe('SessionEventStore', () => {
  it('holds sequence 3 until sequence 2 arrives and advances highest contiguous cursor only', () => {
    let store = createSessionEventStore();
    store = reduceSessionEvent(store, event(1));
    store = reduceSessionEvent(store, event(3));

    expect(store.highestContiguousSequence).toBe(1);
    expect(store.projection.phase).toBe('gap_detected');
    expect(store.items['item-1']?.content).toBe('');

    store = reduceSessionEvent(store, event(2));

    expect(store.highestContiguousSequence).toBe(3);
    expect(store.projection.phase).toBe('current');
    expect(store.items['item-1']?.content).toBe('23');

    const duplicate = reduceSessionEvent(store, event(2));
    expect(duplicate).toBe(store);
  });

  it('marks same-sequence different-event as stale instead of last-write-wins', () => {
    let store = reduceSessionEvent(createSessionEventStore(), event(1));
    store = reduceSessionEvent(store, event(1, { event_id: 'conflict', payload: { content: 'other' } }));

    expect(store.projection.phase).toBe('stale');
    expect(store.consistencyIncident).toMatchObject({ sequence: 1, existingEventId: 'event-1', incomingEventId: 'conflict' });
    expect(store.items['item-1']?.content).toBe('');
  });

  it('keeps one stable item and never regresses a committed terminal final', () => {
    let store = createSessionEventStore();
    store = reduceSessionEvent(store, event(1));
    store = reduceSessionEvent(store, event(2, { payload: { content: 'hello ', phase: 'unknown' } }));
    store = reduceSessionEvent(store, event(3, {
      kind: 'assistant_text.completed',
      lifecycle: 'completed',
      payload_schema: 'hive.session.payload.assistant_text.completed.v2',
      payload: { content: 'world', phase: 'unknown' },
    }));
    store = reduceSessionEvent(store, event(4, {
      kind: 'assistant_text.failed',
      lifecycle: 'failed',
      payload_schema: 'hive.session.payload.assistant_text.failed.v2',
      payload: { content: 'ignored', phase: 'unknown' },
    }));

    expect(Object.keys(store.items)).toEqual(['item-1']);
    expect(store.items['item-1']).toMatchObject({
      id: 'item-1', lifecycle: 'completed', terminal: true, content: 'hello world', first_sequence: 1, last_sequence: 3,
    });
  });

  it('keeps zero-copy final references without duplicating source bytes', () => {
    let store = reduceSessionEvent(createSessionEventStore(), event(1, {
      kind: 'assistant_text.completed', lifecycle: 'completed',
      payload_schema: 'hive.session.payload.assistant_text.completed.v2',
      payload: { content: 'original bytes', phase: 'unknown', block_index: 0 }, content_hash: 'a'.repeat(64),
    }));
    store = reduceSessionEvent(store, event(2, {
      item_id: 'final-1', item_kind: 'assistant_final', kind: 'assistant_final.completed', lifecycle: 'completed',
      payload_schema: 'hive.session.payload.assistant_final.completed.v2', result_id: 'result-1',
      payload: { render_owner_id: 'render-1', source_blocks: [{ item_id: 'item-1', block_index: 0, content_hash: 'a'.repeat(64) }], result_content_hash: 'b'.repeat(64), result_id: 'result-1' },
    }));

    expect(store.items['item-1']?.content).toBe('original bytes');
    expect(store.items['final-1']?.content).toBe('');
    expect(store.items['final-1']?.source_blocks).toEqual([{ item_id: 'item-1', block_index: 0, content_hash: 'a'.repeat(64) }]);
  });

  it('fails closed for unknown kinds, lifecycles, scopes, actors, and audiences', () => {
    const invalidEvents = [
      event(1, { item_kind: 'future_magic', kind: 'future_magic.completed', lifecycle: 'completed', payload_schema: 'hive.session.payload.future_magic.completed.v2' }),
      event(1, { lifecycle: 'delivered', kind: 'assistant_text.delivered', payload_schema: 'hive.session.payload.assistant_text.delivered.v2' }),
      event(1, { scope: { level: 'run', session_id: 'session-1', thread_id: 'session-1', turn_id: 'turn-1', run_id: 'run-1' } }),
      event(1, { actor: { type: 'future_actor' } }),
      event(1, { visibility: { audience: 'everyone' } }),
    ];

    for (const invalid of invalidEvents) {
      expect(() => reduceSessionEvent(createSessionEventStore(), invalid)).toThrow();
    }
  });

  it('validates generated scope identities, assistant phases, and exact hook boundary sources', () => {
    const validSessionStart = event(1, {
      item_kind: 'hook', kind: 'hook.started', lifecycle: 'started',
      payload_schema: 'hive.session.payload.hook.started.v2',
      scope: { level: 'session', session_id: 'session-1', thread_id: 'session-1' },
      actor: { type: 'hook' },
      payload: { boundary: 'SessionStart', source: 'startup' },
    });
    expect(() => reduceSessionEvent(createSessionEventStore(), validSessionStart)).not.toThrow();

    const invalidEvents = [
      event(1, {
        scope: {
          level: 'round', session_id: 'session-1', thread_id: 'session-1',
          turn_id: 'turn-1', run_id: 'run-1', round_id: '',
        },
      }),
      event(1, { payload: { content: 'bytes', phase: 'final' } }),
      event(1, {
        item_kind: 'runtime_failure', kind: 'runtime_failure.recorded', lifecycle: 'recorded',
        payload_schema: 'hive.session.payload.runtime_failure.recorded.v2',
        scope: { level: 'session', session_id: 'session-1', thread_id: 'session-1' },
        actor: { type: 'runtime' }, payload: { domain: 'runtime', code: 'fixture', phase: 'unknown' },
      }),
      { ...validSessionStart, payload: { boundary: 'SessionStart' } },
      { ...validSessionStart, payload: { boundary: 'SessionStart', source: 'invalid' } },
      {
        ...validSessionStart,
        scope: { level: 'run', session_id: 'session-1', thread_id: 'session-1', turn_id: 'turn-1', run_id: 'run-1' },
        payload: { boundary: 'Stop', source: 'startup' },
      },
      {
        ...validSessionStart,
        kind: 'hook.blocked', lifecycle: 'blocked',
        payload_schema: 'hive.session.payload.hook.blocked.v2',
      },
    ] as SessionEventV2[];

    for (const invalid of invalidEvents) {
      expect(() => reduceSessionEvent(createSessionEventStore(), invalid)).toThrow();
    }
  });

  it('rejects compatibility envelopes instead of treating them as canonical events', () => {
    const compatibility = {
      schema: 'hive.session_event_compatibility', schema_version: 1,
      event_id: 'legacy-1', sequence: 1, compatibility_status: 'needs_reconciliation',
    };
    expect(() => reduceSessionEvent(createSessionEventStore(), compatibility as unknown as SessionEventV2)).toThrow('unsupported_session_event_schema');
  });

  it('quarantines a mixed-generation compatibility envelope while advancing only the contiguous delivery cursor', () => {
    const store = reduceSessionCompatibilityEvent(createSessionEventStore(4), {
      schema: 'hive.session_event_compatibility',
      schema_version: 1,
      compatibility_status: 'needs_reconciliation',
      event_id: 'legacy-5',
      sequence: 5,
      reason: 'unmapped_legacy_kind',
    });

    expect(store.highestContiguousSequence).toBe(5);
    expect(store.items).toEqual({});
    expect(store.projection.phase).toBe('stale');
    expect(store.compatibilityQuarantine).toEqual([
      { eventId: 'legacy-5', sequence: 5, reason: 'unmapped_legacy_kind' },
    ]);
  });

  it('switches to explicit full hydration when the out-of-order buffer reaches its resource ceiling', () => {
    let store = createSessionEventStore(0, 2);
    store = reduceSessionEvent(store, event(3));
    store = reduceSessionEvent(store, event(4));
    store = reduceSessionEvent(store, event(5));

    expect(store.projection.phase).toBe('stale');
    expect(store.recoveryRequired).toBe('full_hydration');
    expect(store.bufferedEvents).toEqual({});
    expect(store.highestContiguousSequence).toBe(0);
  });
});
