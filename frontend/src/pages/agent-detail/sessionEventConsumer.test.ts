import { describe, expect, it } from 'vitest';

import type { ChatTranscriptEventPayload } from './chatRuntime';
import { buildRunTimelineFromMessages } from './chatDisclosureReducer';
import {
  consumeSessionEnvelope,
  hydrateSessionTranscriptEvents,
  projectSessionEventStoreToMessages,
} from './sessionEventConsumer';
import {
  sessionPayloadContent,
  type SessionEventStore,
  type SessionEventV2,
} from '../session-workbench/sessionEventStore';

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
  it('uses the backend canonical rendering contract for multipart user input', () => {
    expect(sessionPayloadContent({
      content_parts: [
        { type: 'text', text: '研究这个文件' },
        { type: 'file', z: 2, a: 'report.pdf' },
      ],
    })).toBe('[{"text":"研究这个文件","type":"text"},{"a":"report.pdf","type":"file","z":2}]');
  });

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

  it('advances live public commentary across a redacted provider-private continuity event', () => {
    const started = event(1, 'started');
    const privateContinuity: SessionEventV2 = {
      ...event(2, 'delta'),
      item_id: 'private-reasoning-1',
      item_kind: 'assistant_reasoning_private',
      kind: 'assistant_reasoning_private.delta',
      payload_schema: 'hive.session.payload.assistant_reasoning_private.delta.v2',
      visibility: {
        audience: 'private_provider',
        redacted_fields: ['/payload/content'],
      },
      payload: { phase: 'reasoning_private' },
    };
    const publicDelta = event(3, 'delta');

    const store = replay([started, privateContinuity, publicDelta]);
    const messages = projectSessionEventStoreToMessages(store);

    expect(store.highestContiguousSequence).toBe(3);
    expect(store.projection).toMatchObject({ phase: 'current', buffered_sequences: [] });
    expect(store.items['private-reasoning-1']).toMatchObject({
      content: '',
      visibility: { audience: 'private_provider' },
    });
    expect(messages).toEqual(expect.arrayContaining([
      expect.objectContaining({ role: 'event', content: '', eventType: 'assistant_reasoning_private' }),
      expect.objectContaining({ role: 'assistant', content: 'exact bytes' }),
    ]));
    expect(JSON.stringify(messages)).not.toContain('provider-private-reasoning-secret');
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
      payload: { content_parts: [{ type: 'text', text: 'do the work' }], intent: 'start_turn' },
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
      payload: { tool_name: 'read_file', arguments: { path: 'workspace/report.md' } },
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
      toolArgs: { path: 'workspace/report.md' },
      toolStatus: 'done',
      toolResult: 'file bytes',
      sessionItem: { id: 'tool-call-1', kind: 'tool_call' },
    });
  });

  it('recovers a legacy persisted tool envelope during a rolling canonical replay', () => {
    const progress = 'LIVE_PROGRESS_REPLAY_0717: checking the durable Session path.';
    const legacyToolCall: SessionEventV2 = {
      ...event(1, 'started'),
      ordinal: undefined,
      item_id: 'legacy-progress-call-1',
      item_kind: 'tool_call',
      kind: 'tool_call.started',
      lifecycle: 'started',
      payload_schema: 'hive.session.payload.tool_call.started.v2',
      actor: { type: 'tool' },
      payload: {
        content: JSON.stringify({
          name: 'report_progress',
          args: { message: progress },
          status: 'running',
          tool_call_id: 'progress-call-1',
        }),
        parts: [],
        metadata: { tool_name: 'report_progress', tool_call_id: 'progress-call-1' },
        legacy: true,
      },
    };

    const messages = projectSessionEventStoreToMessages(replay([legacyToolCall]));

    expect(messages).toHaveLength(1);
    expect(messages[0]).toMatchObject({
      role: 'tool_call',
      toolName: 'report_progress',
      toolArgs: { message: progress },
      toolStatus: 'running',
    });
    expect(buildRunTimelineFromMessages(messages).steps).toEqual([
      expect.objectContaining({
        kind: 'commentary',
        title: 'Progress update',
        details: progress,
      }),
    ]);
  });

  it('projects a native report_progress commentary item identically after live delivery and replay', () => {
    const progress = 'LIVE_PROGRESS_NATIVE_0717: validating live delivery and reload.';
    const commentary: SessionEventV2 = {
      ...event(1, 'completed'),
      ordinal: undefined,
      item_id: 'progress-commentary-1',
      item_kind: 'assistant_commentary',
      kind: 'assistant_commentary.completed',
      lifecycle: 'completed',
      payload_schema: 'hive.session.payload.assistant_commentary.completed.v2',
      invocation_id: 'progress-invocation-1',
      parent_item_id: 'progress-tool-call-1',
      actor: { type: 'assistant' },
      payload: { phase: 'commentary', content: progress },
    };

    const messages = projectSessionEventStoreToMessages(replay([commentary]));
    const timeline = buildRunTimelineFromMessages(messages);

    expect(messages).toEqual([
      expect.objectContaining({
        role: 'assistant',
        content: progress,
        eventType: 'assistant_commentary',
      }),
    ]);
    expect(timeline.steps).toEqual([
      expect.objectContaining({
        kind: 'commentary',
        title: 'Progress update',
        details: progress,
      }),
    ]);
  });

  it('preserves no-phase public model text as assistant_text without forging commentary semantics', () => {
    const progress = 'I found the failing path. Next I am checking the durable task state.';
    const delta = {
      ...event(1, 'delta'),
      payload: { phase: 'unknown', content: progress },
    } as SessionEventV2;
    const snapshot = {
      ...event(2, 'completed'),
      item_kind: 'assistant_text',
      kind: 'assistant_text.snapshot',
      lifecycle: 'snapshot',
      payload_schema: 'hive.session.payload.assistant_text.snapshot.v2',
      payload: { phase: 'unknown', content: progress },
    } as SessionEventV2;
    const completed = {
      ...event(3, 'completed'),
      item_kind: 'assistant_text',
      kind: 'assistant_text.completed',
      lifecycle: 'completed',
      payload_schema: 'hive.session.payload.assistant_text.completed.v2',
      payload: { phase: 'unknown', content: '' },
    } as SessionEventV2;

    const messages = projectSessionEventStoreToMessages(replay([delta, snapshot, completed]));

    expect(messages).toEqual([
      expect.objectContaining({
        role: 'assistant',
        content: progress,
        eventType: 'assistant_text',
        eventStatus: 'completed',
        sessionItem: expect.objectContaining({ kind: 'assistant_text' }),
      }),
    ]);
  });

  it('projects durable artifact parts on the canonical final message', () => {
    const source = {
      ...event(1, 'completed'),
      item_id: 'assistant-source-1',
      item_kind: 'assistant_text',
      kind: 'assistant_text.completed',
      lifecycle: 'completed',
      payload_schema: 'hive.session.payload.assistant_text.completed.v2',
      actor: { type: 'assistant' as const },
      payload: { content: 'Final answer' },
    };
    const final = {
      ...event(2, 'completed'),
      item_id: 'assistant-final-1',
      item_kind: 'assistant_final',
      kind: 'assistant_final.completed',
      lifecycle: 'completed',
      payload_schema: 'hive.session.payload.assistant_final.completed.v2',
      actor: { type: 'assistant' as const },
      payload: {
        source_blocks: [{ item_id: 'assistant-source-1', block_index: 0, content_hash: 'hash-1' }],
        parts: [
          {
            type: 'artifact',
            artifact_id: 'artifact-1',
            path: 'workspace/final-report.md',
            name: 'final-report.md',
            preview_kind: 'markdown',
            source: 'workspace_write',
            runtime_task_id: 'run-1',
          },
        ],
      },
    };

    const messages = projectSessionEventStoreToMessages(replay([source, final]));

    expect(messages).toHaveLength(1);
    expect(messages[0]).toMatchObject({
      role: 'assistant',
      content: 'Final answer',
      artifacts: [
        {
          id: 'artifact-1',
          path: 'workspace/final-report.md',
          name: 'final-report.md',
          previewKind: 'markdown',
          source: 'workspace_write',
          runtimeTaskId: 'run-1',
        },
      ],
    });
  });

  it('keeps a legacy message at its original sequence when later compatibility events add no message', () => {
    const legacyAssistant = {
      schema: 'hive.session_event_compatibility',
      schema_version: 1,
      compatibility_status: 'needs_reconciliation',
      event_id: 'legacy-assistant-1',
      sequence: 1,
      reason: 'legacy_generation',
      legacy_event_type: 'assistant_message',
      payload: {
        content: 'OLDER_LEGACY_FINAL',
        legacy_run_id: 'run-old',
        metadata: {},
      },
    } as unknown as ChatTranscriptEventPayload;
    const acceptedInput: SessionEventV2 = {
      ...event(2, 'completed'),
      ordinal: undefined,
      item_id: 'input-new',
      item_kind: 'human_input',
      kind: 'human_input.accepted',
      lifecycle: 'accepted',
      payload_schema: 'hive.session.payload.human_input.accepted.v2',
      scope: { level: 'session', session_id: 'session-1', thread_id: 'session-1' },
      actor: { type: 'user', id: 'user-1' },
      payload: { content: 'NEWER_USER_PROMPT' },
    };
    const latestProgress: SessionEventV2 = {
      ...event(3, 'completed'),
      ordinal: undefined,
      item_id: 'latest-progress',
      item_kind: 'assistant_commentary',
      kind: 'assistant_commentary.completed',
      lifecycle: 'completed',
      payload_schema: 'hive.session.payload.assistant_commentary.completed.v2',
      payload: { phase: 'commentary', content: 'LATEST_NATIVE_PROGRESS' },
    };
    const laterCompatibilityPhase = {
      schema: 'hive.session_event_compatibility',
      schema_version: 1,
      compatibility_status: 'needs_reconciliation',
      event_id: 'legacy-phase-4',
      sequence: 4,
      reason: 'legacy_generation',
      legacy_event_type: 'phase',
      payload: { content: '', metadata: { phase: 'done' } },
    } as unknown as ChatTranscriptEventPayload;

    const hydrated = hydrateSessionTranscriptEvents([
      legacyAssistant,
      acceptedInput as unknown as ChatTranscriptEventPayload,
      latestProgress as unknown as ChatTranscriptEventPayload,
      laterCompatibilityPhase,
    ]);

    expect(hydrated.messages.map((message) => message.content)).toEqual([
      'OLDER_LEGACY_FINAL',
      'NEWER_USER_PROMPT',
      'LATEST_NATIVE_PROGRESS',
    ]);
  });

  it('lets a canonical assistant_final supersede a legacy assistant_message bound to the same run', () => {
    const unrelatedLegacy = {
      schema: 'hive.session_event_compatibility',
      schema_version: 1,
      compatibility_status: 'needs_reconciliation',
      event_id: 'legacy-final-unrelated',
      sequence: 1,
      reason: 'legacy_generation',
      legacy_event_type: 'assistant_message',
      payload: {
        content: 'UNRELATED_LEGACY_FINAL',
        legacy_run_id: 'run-unrelated',
        metadata: {},
      },
    } as unknown as ChatTranscriptEventPayload;
    const source: SessionEventV2 = {
      ...event(2, 'completed'),
      ordinal: undefined,
      item_id: 'canonical-source',
      item_kind: 'assistant_text',
      kind: 'assistant_text.completed',
      lifecycle: 'completed',
      payload_schema: 'hive.session.payload.assistant_text.completed.v2',
      payload: { phase: 'unknown', content: 'CANONICAL_FINAL_BYTES' },
    };
    const final: SessionEventV2 = {
      ...event(3, 'completed'),
      ordinal: undefined,
      item_id: 'canonical-final',
      item_kind: 'assistant_final',
      kind: 'assistant_final.completed',
      lifecycle: 'completed',
      payload_schema: 'hive.session.payload.assistant_final.completed.v2',
      payload: {
        phase: 'final',
        render_owner_id: 'canonical-render-owner',
        source_blocks: [
          { item_id: 'canonical-source', block_index: 0, content_hash: 'hash-1' },
        ],
      },
    };
    const legacyDuplicate = {
      schema: 'hive.session_event_compatibility',
      schema_version: 1,
      compatibility_status: 'needs_reconciliation',
      event_id: 'legacy-final-duplicate',
      sequence: 4,
      reason: 'legacy_generation',
      legacy_event_type: 'assistant_message',
      payload: {
        content: 'STALE_LEGACY_PROJECTION',
        legacy_run_id: 'run-1',
        metadata: {},
      },
    } as unknown as ChatTranscriptEventPayload;

    const hydrated = hydrateSessionTranscriptEvents([
      unrelatedLegacy,
      source as unknown as ChatTranscriptEventPayload,
      final as unknown as ChatTranscriptEventPayload,
      legacyDuplicate,
    ]);

    expect(hydrated.messages).toEqual([
      expect.objectContaining({
        role: 'assistant',
        content: 'UNRELATED_LEGACY_FINAL',
      }),
      expect.objectContaining({
        role: 'assistant',
        content: 'CANONICAL_FINAL_BYTES',
        sessionItem: expect.objectContaining({ kind: 'assistant_final' }),
      }),
    ]);
  });
});
