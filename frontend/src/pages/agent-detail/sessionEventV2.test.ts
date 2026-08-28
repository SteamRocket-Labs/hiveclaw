import { describe, expect, it } from 'vitest';

import {
  createSessionEventStore,
  reduceSessionCompatibilityEvent,
  reduceSessionEvent,
  type SessionCompatibilityEvent,
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
    // Duplicate delivery exposes an empty transition truthfully (Codex
    // REQUEST_CHANGES #3 D): identity churns but nothing was applied.
    expect(duplicate.lastTransition.appliedEvents).toEqual([]);
    expect(duplicate.items).toBe(store.items);
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

describe('SessionEventStore transition application report (Codex REQUEST_CHANGES #3)', () => {
  function compatibility(sequence: number, overrides: Partial<Record<string, unknown>> = {}): SessionCompatibilityEvent {
    return {
      schema: 'hive.session_event_compatibility',
      schema_version: 1,
      compatibility_status: 'needs_reconciliation',
      event_id: `legacy-${sequence}`,
      sequence,
      reason: 'unmapped_legacy_kind',
      ...overrides,
    } as SessionCompatibilityEvent;
  }

  it('reports every canonical event applied to the contiguous projection per transition, in sequence order', () => {
    let store = createSessionEventStore();
    store = reduceSessionEvent(store, event(1));
    expect(store.lastTransition.appliedEvents.map((applied) => applied.event_id)).toEqual(['event-1']);

    // Buffered-only arrival: gap state advances, but nothing was applied.
    store = reduceSessionEvent(store, event(3));
    expect(store.projection.phase).toBe('gap_detected');
    expect(store.lastTransition.appliedEvents).toEqual([]);
    expect(store.lastTransition.compatibilityApplied).toBe(false);

    // Gap close drains in sequence order: carrier first, then the buffered tail.
    store = reduceSessionEvent(store, event(2));
    expect(store.lastTransition.appliedEvents.map((applied) => applied.event_id)).toEqual(['event-2', 'event-3']);

    const duplicate = reduceSessionEvent(store, event(2));
    // Duplicate delivery reports an empty transition truthfully (finding D).
    expect(duplicate.lastTransition.appliedEvents).toEqual([]);
    expect(duplicate.lastTransition.appliedCompatibilityEvents).toEqual([]);
  });

  it('excludes consistency conflicts and recovery holds from the applied report', () => {
    let store = reduceSessionEvent(createSessionEventStore(), event(1));
    store = reduceSessionEvent(store, event(1, { event_id: 'conflict', payload: { content: 'other', phase: 'unknown' } }));
    expect(store.consistencyIncident).toMatchObject({ sequence: 1, incomingEventId: 'conflict' });
    expect(store.lastTransition.appliedEvents).toEqual([]);

    const recovered = reduceSessionEvent(
      reduceSessionEvent(
        reduceSessionEvent(createSessionEventStore(0, 2), event(3)),
        event(4),
      ),
      event(5),
    );
    expect(recovered.recoveryRequired).toBe('full_hydration');
    expect(recovered.lastTransition.appliedEvents).toEqual([]);
  });

  it('excludes contiguous events ignored because the item was already terminal or the ordinal was stale', () => {
    let store = createSessionEventStore();
    store = reduceSessionEvent(store, event(1, {
      kind: 'assistant_text.completed', lifecycle: 'completed',
      payload_schema: 'hive.session.payload.assistant_text.completed.v2',
    }));
    store = reduceSessionEvent(store, event(2));
    expect(store.ignoredEventIds).toEqual(['event-2']);
    expect(store.lastTransition.appliedEvents).toEqual([]);

    store = createSessionEventStore();
    store = reduceSessionEvent(store, event(1));
    store = reduceSessionEvent(store, event(2, { ordinal: 0 }));
    expect(store.ignoredEventIds).toEqual(['event-2']);
    expect(store.lastTransition.appliedEvents).toEqual([]);
  });

  it('reports the compatibility carrier and drained canonical events of one transition', () => {
    let store = reduceSessionEvent(createSessionEventStore(), event(2));
    expect(store.lastTransition.appliedEvents).toEqual([]);

    store = reduceSessionCompatibilityEvent(store, compatibility(1));
    expect(store.lastTransition.compatibilityApplied).toBe(true);
    expect(store.lastTransition.appliedEvents.map((applied) => applied.event_id)).toEqual(['event-2']);

    const bufferedCompatibility = reduceSessionCompatibilityEvent(createSessionEventStore(), compatibility(5));
    expect(bufferedCompatibility.projection.phase).toBe('gap_detected');
    expect(bufferedCompatibility.lastTransition.appliedEvents).toEqual([]);
    expect(bufferedCompatibility.lastTransition.compatibilityApplied).toBe(false);
  });

  it('reports drained compatibility events in sequence order when a canonical carrier closes the gap (Codex finding C)', () => {
    let store = reduceSessionCompatibilityEvent(createSessionEventStore(), compatibility(2));
    store = reduceSessionCompatibilityEvent(store, compatibility(3));
    expect(store.lastTransition.appliedCompatibilityEvents).toEqual([]);

    store = reduceSessionEvent(store, event(1));
    expect(store.lastTransition.appliedEvents.map((applied) => applied.event_id)).toEqual(['event-1']);
    expect(store.lastTransition.compatibilityApplied).toBe(false);
    expect(store.lastTransition.appliedCompatibilityEvents.map((applied) => applied.event_id))
      .toEqual(['legacy-2', 'legacy-3']);
    expect(store.highestContiguousSequence).toBe(3);
    expect(store.bufferedCompatibilityEvents).toEqual({});
    expect(store.compatibilityQuarantine.map((quarantined) => quarantined.eventId))
      .toEqual(['legacy-2', 'legacy-3']);

    // At-least-once redelivery of a drained compatibility envelope consumed it
    // already: no second report, no projection change.
    const duplicate = reduceSessionCompatibilityEvent(store, compatibility(2));
    expect(duplicate.lastTransition.appliedCompatibilityEvents).toEqual([]);
    expect(duplicate.lastTransition.appliedEvents).toEqual([]);
    expect(duplicate.compatibilityQuarantine.map((quarantined) => quarantined.eventId))
      .toEqual(['legacy-2', 'legacy-3']);
  });

  it('reports compatibility events drained behind a compatibility carrier without double-counting the carrier (Codex finding C)', () => {
    let store = reduceSessionCompatibilityEvent(createSessionEventStore(), compatibility(2));
    store = reduceSessionCompatibilityEvent(store, compatibility(1));
    expect(store.lastTransition.compatibilityApplied).toBe(true);
    // The carrier itself is delivered through its own projection path; only the
    // drained buffered envelopes appear in appliedCompatibilityEvents.
    expect(store.lastTransition.appliedCompatibilityEvents.map((applied) => applied.event_id))
      .toEqual(['legacy-2']);
    expect(store.lastTransition.appliedEvents).toEqual([]);
    expect(store.highestContiguousSequence).toBe(2);
  });
});

describe('SessionEventStore lastTransition truth for direct reducer callers (Codex REQUEST_CHANGES #3 D)', () => {
  function compatibility(sequence: number): SessionCompatibilityEvent {
    return {
      schema: 'hive.session_event_compatibility',
      schema_version: 1,
      compatibility_status: 'needs_reconciliation',
      event_id: `legacy-${sequence}`,
      sequence,
      reason: 'legacy_generation',
    } as SessionCompatibilityEvent;
  }

  it('exposes an empty transition for a duplicate delivery instead of the prior applied report', () => {
    let store = reduceSessionEvent(createSessionEventStore(), event(1));
    expect(store.lastTransition.appliedEvents.map((applied) => applied.event_id)).toEqual(['event-1']);

    const duplicate = reduceSessionEvent(store, event(1));
    expect(duplicate.lastTransition.appliedEvents).toEqual([]);
    expect(duplicate.lastTransition.compatibilityApplied).toBe(false);
    expect(duplicate.items).toBe(store.items);
    expect(duplicate.highestContiguousSequence).toBe(store.highestContiguousSequence);

    const duplicateCompatibility = reduceSessionCompatibilityEvent(
      reduceSessionCompatibilityEvent(store, compatibility(2)),
      compatibility(2),
    );
    expect(duplicateCompatibility.lastTransition.appliedEvents).toEqual([]);
    expect(duplicateCompatibility.lastTransition.appliedCompatibilityEvents).toEqual([]);
    expect(duplicateCompatibility.lastTransition.compatibilityApplied).toBe(false);
  });

  it('exposes an empty transition for a late pre-cursor delivery instead of the prior applied report', () => {
    let store = reduceSessionEvent(createSessionEventStore(5), event(6));
    expect(store.lastTransition.appliedEvents.map((applied) => applied.event_id)).toEqual(['event-6']);

    const late = reduceSessionEvent(store, event(3));
    expect(late.lastTransition.appliedEvents).toEqual([]);
    expect(late.lastTransition.compatibilityApplied).toBe(false);
    expect(late.items).toBe(store.items);

    const lateCompatibility = reduceSessionCompatibilityEvent(store, compatibility(2));
    expect(lateCompatibility.lastTransition.appliedEvents).toEqual([]);
    expect(lateCompatibility.lastTransition.appliedCompatibilityEvents).toEqual([]);
    expect(lateCompatibility.lastTransition.compatibilityApplied).toBe(false);
  });

  it('exposes an empty transition while a recovery hold rejects every arrival', () => {
    let store = createSessionEventStore(0, 1);
    store = reduceSessionEvent(store, event(2));
    store = reduceSessionEvent(store, event(3));
    expect(store.recoveryRequired).toBe('full_hydration');
    expect(store.lastTransition.appliedEvents).toEqual([]);

    const held = reduceSessionEvent(store, event(7));
    expect(held.recoveryRequired).toBe('full_hydration');
    expect(held.lastTransition.appliedEvents).toEqual([]);
    expect(held.lastTransition.compatibilityApplied).toBe(false);
    expect(held.items).toBe(store.items);
  });
});
