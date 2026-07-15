import { SESSION_EVENT_CONTRACT } from './sessionEventContract.generated';

export type SessionScopeV2 =
  | { level: 'session'; session_id: string; thread_id: string }
  | { level: 'turn'; session_id: string; thread_id: string; turn_id: string }
  | { level: 'run'; session_id: string; thread_id: string; turn_id: string; run_id: string }
  | { level: 'round'; session_id: string; thread_id: string; turn_id: string; run_id: string; round_id: string };

export type SessionEventV2 = {
  schema: 'hive.session_event';
  schema_version: 2;
  event_id: string;
  sequence: number;
  ordinal?: number;
  command_id?: string;
  tenant_id: string;
  scope: SessionScopeV2;
  item_id: string;
  item_kind: string;
  kind: string;
  lifecycle: string;
  payload_schema: string;
  input_id?: string;
  result_id?: string;
  invocation_id?: string;
  provider_tool_use_id?: string;
  content_hash?: string;
  parent_item_id?: string;
  actor: { type: string; id?: string };
  visibility: { audience: string; redacted_fields?: string[] };
  payload: Record<string, unknown>;
  display?: { title?: string; summary?: string; detail_ref?: string };
  occurred_at: string;
  persisted_at: string;
};

export type SessionItemV2 = {
  id: string;
  kind: string;
  scope: SessionScopeV2;
  lifecycle: string;
  terminal: boolean;
  revision: number;
  content: string;
  summary?: string;
  source_blocks?: Array<{ item_id: string; block_index: number; content_hash: string }>;
  first_sequence: number;
  last_sequence: number;
  last_ordinal?: number;
};

export type ProjectionSyncState = {
  phase: 'hydrating' | 'catching_up' | 'current' | 'gap_detected' | 'stale';
  highest_contiguous_sequence: number;
  buffered_sequences: number[];
};

export type SessionEventStore = {
  items: Record<string, SessionItemV2>;
  highestContiguousSequence: number;
  projection: ProjectionSyncState;
  bufferedEvents: Record<number, SessionEventV2>;
  eventIdBySequence: Record<number, string>;
  seenEventIds: Record<string, true>;
  ignoredEventIds: string[];
  consistencyIncident?: { sequence: number; existingEventId: string; incomingEventId: string };
};

type EventRule = {
  lifecycles: ReadonlySet<string>;
  scopes: ReadonlySet<SessionScopeV2['level']>;
  terminal: ReadonlySet<string>;
};
type HookRule = {
  lifecycles: ReadonlySet<string>;
  scopes: ReadonlySet<SessionScopeV2['level']>;
  sources: ReadonlySet<string>;
  sourceRequired: boolean;
};

const EVENT_RULES: Record<string, EventRule> = Object.fromEntries(
  Object.entries(SESSION_EVENT_CONTRACT.event_rules).map(([itemKind, eventRule]) => [
    itemKind,
    {
      lifecycles: new Set(eventRule.lifecycles),
      scopes: new Set(eventRule.scopes) as ReadonlySet<SessionScopeV2['level']>,
      terminal: new Set(eventRule.terminal),
    },
  ]),
);
const HOOK_RULES: Record<string, HookRule> = Object.fromEntries(
  Object.entries(SESSION_EVENT_CONTRACT.hook_rules).map(([boundary, hookRule]) => [
    boundary,
    {
      lifecycles: new Set(hookRule.lifecycles),
      scopes: new Set(hookRule.scopes) as ReadonlySet<SessionScopeV2['level']>,
      sources: new Set(hookRule.sources),
      sourceRequired: hookRule.source_required,
    },
  ]),
);
const ACTOR_TYPES = new Set<string>(SESSION_EVENT_CONTRACT.actor_types);
const AUDIENCES = new Set<string>(SESSION_EVENT_CONTRACT.audiences);
const SCOPE_FIELDS: Record<SessionScopeV2['level'], ReadonlySet<string>> = Object.fromEntries(
  Object.entries(SESSION_EVENT_CONTRACT.scope_fields).map(([level, fields]) => [level, new Set(fields)]),
) as unknown as Record<SessionScopeV2['level'], ReadonlySet<string>>;
const SCOPE_REQUIRED_IDS: Record<SessionScopeV2['level'], readonly string[]> =
  SESSION_EVENT_CONTRACT.scope_required_ids;
const ASSISTANT_PHASES: Record<string, string> = SESSION_EVENT_CONTRACT.assistant_phases;

export function createSessionEventStore(): SessionEventStore {
  return {
    items: {}, highestContiguousSequence: 0,
    projection: { phase: 'hydrating', highest_contiguous_sequence: 0, buffered_sequences: [] },
    bufferedEvents: {}, eventIdBySequence: {}, seenEventIds: {}, ignoredEventIds: [],
  };
}

function assertEnvelope(event: SessionEventV2): void {
  if (event.schema !== SESSION_EVENT_CONTRACT.schema || event.schema_version !== SESSION_EVENT_CONTRACT.schema_version) throw new Error('unsupported_session_event_schema');
  const eventRule = EVENT_RULES[event.item_kind];
  if (!eventRule || !eventRule.lifecycles.has(event.lifecycle)) throw new Error('unsupported_session_event_kind_lifecycle');
  if (event.kind !== `${event.item_kind}.${event.lifecycle}`) throw new Error('session_event_kind_mismatch');
  if (event.payload_schema !== `hive.session.payload.${event.item_kind}.${event.lifecycle}.v2`) throw new Error('session_event_payload_schema_mismatch');
  if (!Number.isSafeInteger(event.sequence) || event.sequence <= 0) throw new Error('invalid_session_sequence');
  if (!event.event_id || !event.item_id || !event.tenant_id) throw new Error('incomplete_session_event_identity');
  if (!event.scope || !eventRule.scopes.has(event.scope.level)) throw new Error('invalid_session_event_scope');
  const allowedScopeFields = SCOPE_FIELDS[event.scope.level];
  if (!allowedScopeFields || Object.keys(event.scope).some((key) => !allowedScopeFields.has(key))) throw new Error('invalid_session_event_scope_fields');
  const requiredScopeIds = SCOPE_REQUIRED_IDS[event.scope.level];
  if (!requiredScopeIds || requiredScopeIds.some((key) => {
    const value = (event.scope as unknown as Record<string, unknown>)[key];
    return typeof value !== 'string' || value.trim().length === 0;
  })) throw new Error('missing_session_event_scope_identity');
  if (!event.scope.session_id || event.scope.thread_id !== event.scope.session_id) throw new Error('invalid_session_event_thread_scope');
  if (!ACTOR_TYPES.has(event.actor?.type)) throw new Error('unsupported_session_event_actor');
  if (!AUDIENCES.has(event.visibility?.audience)) throw new Error('unsupported_session_event_audience');
  if (!event.payload || typeof event.payload !== 'object' || Array.isArray(event.payload)) throw new Error('invalid_session_event_payload');

  const expectedPhase = ASSISTANT_PHASES[event.item_kind];
  const hasPhase = Object.prototype.hasOwnProperty.call(event.payload, 'phase');
  if (expectedPhase !== undefined) {
    if (hasPhase && event.payload.phase !== expectedPhase) throw new Error('invalid_session_event_assistant_phase');
  } else if (hasPhase) {
    throw new Error('illegal_session_event_assistant_phase');
  }

  if (event.item_kind === 'hook') {
    const boundary = typeof event.payload.boundary === 'string' ? event.payload.boundary : '';
    const hookRule = HOOK_RULES[boundary];
    if (!hookRule || !hookRule.lifecycles.has(event.lifecycle) || !hookRule.scopes.has(event.scope.level)) {
      throw new Error('invalid_session_event_hook_boundary');
    }
    const hasSource = Object.prototype.hasOwnProperty.call(event.payload, 'source');
    if (hookRule.sourceRequired) {
      if (!hasSource || typeof event.payload.source !== 'string' || !hookRule.sources.has(event.payload.source)) {
        throw new Error('invalid_session_event_hook_source');
      }
    } else if (hasSource) {
      throw new Error('illegal_session_event_hook_source');
    }
  }
}

function isTerminal(event: SessionEventV2): boolean {
  return EVENT_RULES[event.item_kind].terminal.has(event.lifecycle);
}

function reduceContiguous(store: SessionEventStore, event: SessionEventV2): SessionEventStore {
  const prior = store.items[event.item_id];
  let items = store.items;
  let ignoredEventIds = store.ignoredEventIds;
  const ordinal = event.ordinal;
  if (prior?.terminal || (prior?.last_ordinal !== undefined && ordinal !== undefined && ordinal <= prior.last_ordinal)) {
    ignoredEventIds = [...ignoredEventIds, event.event_id];
  } else {
    const contentDelta = typeof event.payload.content === 'string' ? event.payload.content : '';
    const content = prior
      ? event.lifecycle === 'snapshot' ? contentDelta : `${prior.content}${contentDelta}`
      : contentDelta;
    const sourceBlocks = Array.isArray(event.payload.source_blocks)
      ? event.payload.source_blocks as SessionItemV2['source_blocks']
      : prior?.source_blocks;
    items = {
      ...items,
      [event.item_id]: {
        id: event.item_id, kind: event.item_kind, scope: event.scope,
        lifecycle: event.lifecycle, terminal: isTerminal(event), revision: (prior?.revision ?? 0) + 1,
        content, summary: event.display?.summary ?? prior?.summary, source_blocks: sourceBlocks,
        first_sequence: prior?.first_sequence ?? event.sequence, last_sequence: event.sequence,
        last_ordinal: ordinal ?? prior?.last_ordinal,
      },
    };
  }
  return {
    ...store, items, ignoredEventIds,
    highestContiguousSequence: event.sequence,
    eventIdBySequence: { ...store.eventIdBySequence, [event.sequence]: event.event_id },
    seenEventIds: { ...store.seenEventIds, [event.event_id]: true },
  };
}

export function reduceSessionEvent(store: SessionEventStore, event: SessionEventV2): SessionEventStore {
  assertEnvelope(event);
  if (store.seenEventIds[event.event_id]) return store;
  const existingEventId = store.eventIdBySequence[event.sequence] ?? store.bufferedEvents[event.sequence]?.event_id;
  if (existingEventId && existingEventId !== event.event_id) {
    return {
      ...store,
      projection: { ...store.projection, phase: 'stale' },
      consistencyIncident: { sequence: event.sequence, existingEventId, incomingEventId: event.event_id },
    };
  }
  if (event.sequence <= store.highestContiguousSequence) return store;
  if (event.sequence > store.highestContiguousSequence + 1) {
    const bufferedEvents = { ...store.bufferedEvents, [event.sequence]: event };
    const bufferedSequences = Object.keys(bufferedEvents).map(Number).sort((a, b) => a - b);
    return {
      ...store, bufferedEvents,
      eventIdBySequence: { ...store.eventIdBySequence, [event.sequence]: event.event_id },
      seenEventIds: { ...store.seenEventIds, [event.event_id]: true },
      projection: { phase: 'gap_detected', highest_contiguous_sequence: store.highestContiguousSequence, buffered_sequences: bufferedSequences },
    };
  }

  let next = reduceContiguous(store, event);
  const bufferedEvents = { ...next.bufferedEvents };
  while (bufferedEvents[next.highestContiguousSequence + 1]) {
    const contiguous = bufferedEvents[next.highestContiguousSequence + 1];
    delete bufferedEvents[contiguous.sequence];
    next = reduceContiguous({ ...next, bufferedEvents }, contiguous);
  }
  const bufferedSequences = Object.keys(bufferedEvents).map(Number).sort((a, b) => a - b);
  return {
    ...next, bufferedEvents,
    projection: {
      phase: bufferedSequences.length ? 'gap_detected' : 'current',
      highest_contiguous_sequence: next.highestContiguousSequence,
      buffered_sequences: bufferedSequences,
    },
  };
}
