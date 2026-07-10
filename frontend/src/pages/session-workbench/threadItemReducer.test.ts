import { describe, expect, it } from 'vitest';

import type { ThreadItem } from '../../api/domains/threadItems.generated';
import {
  THREAD_ITEM_TYPES,
  normalizeThreadItemPayload,
  reduceThreadItems,
  threadItemToAgentChatMessage,
} from './threadItemReducer';

function canonical(overrides: Partial<ThreadItem> = {}): ThreadItem {
  return {
    schema: 'hive.thread_item.v1',
    schema_version: 1,
    id: 'item-1',
    sequence: 7,
    session_id: 'session-1',
    run_id: 'run-1',
    message_id: null,
    parent_event_id: null,
    root_session_id: 'session-1',
    parent_session_id: null,
    turn_id: 'turn-1',
    causation_id: null,
    correlation_id: 'run-1',
    item_type: 'event',
    item_status: 'running',
    actor_type: 'system',
    event_type: 'hook_progress',
    type: 'hook_progress',
    role: 'system',
    visibility_scope: 'direct_user',
    listed_surface: 'chat',
    content: 'Working',
    parts: [],
    metadata: {},
    created_at: '2026-07-10T12:00:00Z',
    item_data: { event_type: 'hook_progress' },
    ...overrides,
  } as ThreadItem;
}

describe('typed ThreadItem reducer', () => {
  it('keeps the backend discriminated union exhaustive', () => {
    expect(THREAD_ITEM_TYPES).toEqual([
      'user_message',
      'agent_message',
      'reasoning',
      'tool_call',
      'tool_result',
      'approval_request',
      'approval_decision',
      'plan',
      'workflow_activity',
      'subagent_activity',
      'context_compaction',
      'artifact',
      'boundary',
      'error',
      'event',
    ]);
  });

  it('prefers the canonical discriminant over misleading legacy fields', () => {
    const item = normalizeThreadItemPayload({
      ...canonical(),
      workflow_run_id: 'must-not-reclassify',
    });

    expect(item?.item_type).toBe('event');
    expect(item?.item_data).toEqual({ event_type: 'hook_progress' });
  });

  it('normalizes historical events through an explicit compatibility map', () => {
    const item = normalizeThreadItemPayload({
      id: 'legacy-compact',
      sequence: 8,
      type: 'session_compact',
      status: 'completed',
      content: 'Context compressed',
      original_message_count: 120,
      kept_message_count: 24,
      continuity_sections_injected: ['Task Ledger'],
    });

    expect(item?.item_type).toBe('context_compaction');
    expect(item?.item_status).toBe('succeeded');
    if (item?.item_type !== 'context_compaction') throw new Error('expected compaction item');
    expect(item.item_data).toEqual({
      original_message_count: 120,
      kept_message_count: 24,
      continuity_sections_injected: ['Task Ledger'],
    });
  });

  it('does not infer a workflow from an unknown payload shape', () => {
    const item = normalizeThreadItemPayload({
      id: 'legacy-future',
      type: 'future_event',
      workflow_run_id: 'run-shaped-field',
    });

    expect(item?.item_type).toBe('event');
  });

  it('keeps legacy status normalization aligned with the backend backfill', () => {
    const cases = [
      ['denial', 'failed'],
      ['tool_failure', 'failed'],
      ['member_run_started', 'running'],
      ['run_cancelled', 'cancelled'],
      ['permission_request', 'waiting_user'],
    ] as const;

    for (const [eventType, expectedStatus] of cases) {
      expect(normalizeThreadItemPayload({ type: eventType })?.item_status).toBe(expectedStatus);
    }
  });

  it('replaces a running item in place and orders reconnect backfill by sequence', () => {
    const running = canonical({ id: 'same', sequence: 10, item_status: 'running' });
    const completed = canonical({ id: 'same', sequence: 10, item_status: 'succeeded', content: 'Done' });
    const earlier = canonical({ id: 'earlier', sequence: 9 });

    const items = reduceThreadItems(reduceThreadItems([running], completed), earlier);

    expect(items.map((item) => item.id)).toEqual(['earlier', 'same']);
    expect(items[1].item_status).toBe('succeeded');
    expect(items[1].content).toBe('Done');
  });

  it('projects approval details into the existing session action bridge', () => {
    const item = canonical({
      item_type: 'approval_request',
      event_type: 'permission_request',
      type: 'permission_request',
      item_status: 'waiting_user',
      item_data: {
        permission_request_id: 'permission-7',
        tool_name: 'write_file',
        arguments: { path: 'report.md' },
        permission_mode: 'default',
        risk_class: 'controlled_write',
        expires_at: '2026-07-10T12:30:00Z',
        allow_session_allowed: false,
        destructive: false,
      },
    } as Partial<ThreadItem>);

    const message = threadItemToAgentChatMessage(item);

    expect(message.threadItem).toBe(item);
    expect(message.eventStatus).toBe('session_permission_required');
    expect(message.sessionPermissionRequest).toMatchObject({
      permission_request_id: 'permission-7',
      tool_name: 'write_file',
      arguments: { path: 'report.md' },
      risk_class: 'controlled_write',
    });
  });

  it('projects Plan, Workflow, Sub-agent, cancellation, and retry evidence without shape inference', () => {
    const projections = [
      threadItemToAgentChatMessage(canonical({
        item_type: 'plan',
        event_type: 'plan_confirmed',
        item_status: 'succeeded',
        item_data: { plan_id: 'plan-1', phase: 'confirmed' },
      } as Partial<ThreadItem>)),
      threadItemToAgentChatMessage(canonical({
        item_type: 'workflow_activity',
        event_type: 'workflow_started',
        item_data: { workflow_run_id: 'workflow-1', runtime_task_id: 'task-workflow' },
      } as Partial<ThreadItem>)),
      threadItemToAgentChatMessage(canonical({
        item_type: 'subagent_activity',
        event_type: 'subagent_task_started',
        item_data: { runtime_task_id: 'task-subagent', child_session_id: 'child-1' },
      } as Partial<ThreadItem>)),
      threadItemToAgentChatMessage(canonical({
        item_type: 'boundary',
        event_type: 'run_cancelled',
        item_status: 'cancelled',
        item_data: { phase: 'cancelled', reason: 'user_stop' },
      } as Partial<ThreadItem>)),
      threadItemToAgentChatMessage(canonical({
        item_type: 'error',
        event_type: 'error',
        item_status: 'failed',
        item_data: { code: 'provider_timeout', reason: 'Timed out', retryable: true },
      } as Partial<ThreadItem>)),
    ];

    expect(projections[0]).toMatchObject({ eventStatus: 'confirmed', threadItem: { item_type: 'plan' } });
    expect(projections[1]).toMatchObject({ eventWorkflowRunId: 'workflow-1', eventRuntimeTaskId: 'task-workflow' });
    expect(projections[2]).toMatchObject({ eventChildSessionId: 'child-1', eventRuntimeTaskId: 'task-subagent' });
    expect(projections[3]).toMatchObject({ eventStatus: 'cancelled', eventReason: 'user_stop' });
    expect(projections[4]).toMatchObject({ eventStatus: 'failed', eventRetryable: true, eventReason: 'Timed out' });
  });
});
