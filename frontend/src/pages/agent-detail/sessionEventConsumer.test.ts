import { describe, expect, it } from 'vitest';

import type { ChatTranscriptEventPayload } from './chatRuntime';
import {
  consumeSessionEnvelope,
  projectSessionEventStoreToMessages,
} from './sessionEventConsumer';
import type { SessionEventStore, SessionEventV2 } from '../session-workbench/sessionEventStore';

function event(sequence: number, lifecycle: 'started' | 'delta' | 'completed'): SessionEventV2 {
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
    item_id: 'assistant-1',
    item_kind: 'assistant_text',
    kind: `assistant_text.${lifecycle}`,
    lifecycle,
    payload_schema: `hive.session.payload.assistant_text.${lifecycle}.v2`,
    actor: { type: 'assistant' },
    visibility: { audience: 'direct_user' },
    payload: { phase: 'unknown', content: lifecycle === 'delta' ? 'exact bytes' : '' },
    occurred_at: '2026-07-16T00:00:00Z',
    persisted_at: '2026-07-16T00:00:00Z',
  };
}

function replay(events: SessionEventV2[]): SessionEventStore {
  let store: SessionEventStore | undefined;
  for (const envelope of events) {
    store = consumeSessionEnvelope(
      envelope as unknown as ChatTranscriptEventPayload,
      store,
      0,
    ).store;
  }
  if (!store) throw new Error('fixture_did_not_create_store');
  return store;
}

describe('canonical Session event consumer', () => {
  it('uses the same highest-contiguous reducer for history, live, reconnect, and duplicate delivery', () => {
    const started = event(1, 'started');
    const delta = event(2, 'delta');
    const completed = event(3, 'completed');

    const history = replay([started, delta, completed]);
    const reconnect = replay([started, completed, delta, delta, completed]);

    expect(reconnect.items).toEqual(history.items);
    expect(reconnect.highestContiguousSequence).toBe(3);
    expect(reconnect.projection.phase).toBe('current');
    expect(reconnect.items['assistant-1']).toMatchObject({
      content: 'exact bytes',
      lifecycle: 'completed',
      terminal: true,
    });
  });

  it('projects canonical items from the shared reducer without reclassifying unknown text as final', () => {
    const assistantStarted = event(2, 'started');
    const assistantDelta = event(3, 'delta');
    const assistantCompleted = event(4, 'completed');
    const acceptedInput: SessionEventV2 = {
      ...event(1, 'completed'),
      ordinal: undefined,
      item_id: 'input-1',
      item_kind: 'human_input',
      kind: 'human_input.accepted',
      lifecycle: 'accepted',
      payload_schema: 'hive.session.payload.human_input.accepted.v2',
      scope: { level: 'session', session_id: 'session-1', thread_id: 'session-1' },
      actor: { type: 'user', id: 'user-1' },
      payload: { content: 'do the work', intent: 'start_turn' },
    };
    const finalEnvelope: SessionEventV2 = {
      ...event(5, 'completed'),
      item_id: 'final-1',
      item_kind: 'assistant_final',
      kind: 'assistant_final.completed',
      lifecycle: 'completed',
      payload_schema: 'hive.session.payload.assistant_final.completed.v2',
      payload: {
        phase: 'final',
        render_owner_id: 'render-owner-1',
        source_blocks: [
          { item_id: 'assistant-1', block_index: 0, content_hash: 'hash-1' },
        ],
      },
    };

    const store = replay([
      acceptedInput,
      assistantStarted,
      assistantDelta,
      assistantCompleted,
      finalEnvelope,
    ]);
    const messages = projectSessionEventStoreToMessages(store);

    expect(messages).toHaveLength(2);
    expect(messages[0]).toMatchObject({
      role: 'user',
      content: 'do the work',
      id: 'input-1',
      sessionItem: { kind: 'human_input' },
    });
    expect(messages[1]).toMatchObject({
      role: 'assistant',
      content: 'exact bytes',
      id: 'render-owner-1',
      sessionItem: {
        id: 'final-1',
        kind: 'assistant_final',
        lifecycle: 'completed',
      },
    });
    expect(messages.filter((message) => message.content === 'exact bytes')).toHaveLength(1);
  });

  it('projects a tool call and its exactly-one result as one stable timeline message', () => {
    const toolCall: SessionEventV2 = {
      ...event(1, 'started'),
      ordinal: undefined,
      item_id: 'tool-call-1',
      item_kind: 'tool_call',
      kind: 'tool_call.started',
      lifecycle: 'started',
      payload_schema: 'hive.session.payload.tool_call.started.v2',
      invocation_id: 'invocation-1',
      actor: { type: 'assistant' },
      payload: { tool_name: 'read_file' },
    };
    const toolResult: SessionEventV2 = {
      ...event(2, 'completed'),
      ordinal: undefined,
      item_id: 'tool-result-1',
      item_kind: 'tool_result',
      kind: 'tool_result.completed',
      lifecycle: 'completed',
      payload_schema: 'hive.session.payload.tool_result.completed.v2',
      invocation_id: 'invocation-1',
      parent_item_id: 'tool-call-1',
      actor: { type: 'tool' },
      payload: { outcome: 'completed', result: 'file bytes' },
    };

    const messages = projectSessionEventStoreToMessages(replay([toolCall, toolResult]));

    expect(messages).toHaveLength(1);
    expect(messages[0]).toMatchObject({
      role: 'tool_call',
      id: 'invocation-1',
      toolName: 'read_file',
      toolStatus: 'done',
      toolResult: 'file bytes',
      sessionItem: { id: 'tool-call-1', kind: 'tool_call' },
    });
  });
});
