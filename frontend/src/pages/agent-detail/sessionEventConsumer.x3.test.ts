import { describe, expect, it } from 'vitest';

import type { AgentChatMessage, ChatTranscriptEventPayload } from './chatRuntime';
import { buildRunTimelineFromMessages } from './chatDisclosureReducer';
import {
  consumeSessionEnvelope,
  hydrateSessionTranscriptEvents,
  projectSessionEventStoreToMessages,
} from './sessionEventConsumer';
import type { SessionEventStore, SessionEventV2 } from '../session-workbench/sessionEventStore';

// X3: the typed member-tool summary flows identically through live delivery
// (consumeSessionEnvelope — the socket/reconcile plane) and through hydration
// (hydrateSessionTranscriptEvents — the reload plane). The raw result bytes
// the model saw never enter the message.

const MEMORY_BODY = [
  '## 读取到的记忆',
  '管理引用（不要向用户展示）：id=explicit_284ff1e71ca75b9f92800d2c6fc40ab9',
  'session-tool-invocation:4f10ca36-8bed-5bc0-88f2-ff509041ab08 scope=sessions',
].join('\n');

function canonicalEvent(sequence: number): SessionEventV2 {
  return {
    schema: 'hive.session_event',
    schema_version: 2,
    event_id: `event-${sequence}`,
    sequence,
    ordinal: sequence - 1,
    tenant_id: 'tenant-1',
    scope: { level: 'round', session_id: 'session-1', thread_id: 'session-1', turn_id: 'turn-1', run_id: 'run-1', round_id: 'round-1' },
    item_id: `item-${sequence}`,
    item_kind: 'assistant_text',
    kind: 'assistant_text.completed',
    lifecycle: 'completed',
    payload_schema: 'hive.session.payload.assistant_text.completed.v2',
    actor: { type: 'assistant' },
    visibility: { audience: 'direct_user' },
    payload: {},
    occurred_at: '2026-11-12T00:00:00Z',
    persisted_at: '2026-11-12T00:00:00Z',
  };
}

function summarizedToolPair(): SessionEventV2[] {
  const call: SessionEventV2 = {
    ...canonicalEvent(1), item_id: 'call', item_kind: 'tool_call',
    kind: 'tool_call.completed', payload_schema: 'hive.session.payload.tool_call.completed.v2',
    invocation_id: 'inv-x3',
    payload: { tool_name: 'load_memory', args: { ids: ['explicit_284ff1e71ca75b9f92800d2c6fc40ab9'] } },
  };
  const result: SessionEventV2 = {
    ...canonicalEvent(2), item_id: 'result', item_kind: 'tool_result',
    kind: 'tool_result.completed', payload_schema: 'hive.session.payload.tool_result.completed.v2',
    invocation_id: 'inv-x3',
    payload: {
      tool_name: 'load_memory',
      status: 'completed',
      result: '',
      content: '',
      member_summary: { kind: 'memory_read', state: 'done' },
    },
  };
  return [call, result];
}

/** The compatibility envelope exactly as the member backend serializes it:
 * raw result and envelope JSON emptied, typed summary attached. */
function compatibilityToolEnvelope(): Record<string, unknown> {
  return {
    schema: 'hive.session_event_compatibility',
    schema_version: 1,
    compatibility_status: 'needs_reconciliation',
    reason: 'insufficient_legacy_scope',
    legacy_kind: 'tool_result',
    legacy_lifecycle: 'legacy_unknown',
    legacy_event_type: 'tool_result',
    legacy_item_type: 'tool_result',
    legacy_item_status: 'succeeded',
    event_id: 'compat-x3',
    sequence: 3,
    tenant_id: 'tenant-1',
    session_id: 'session-1',
    occurred_at: '2026-11-12T00:00:02Z',
    payload: {
      // r2 ruling 5: the legacy envelope minus its raw result serializes back
      // into content, so the member view keeps the pairing mechanics.
      content: JSON.stringify({
        name: 'search_memory',
        args: { query: '周报 格式' },
        status: 'done',
        tool_call_id: 'call_x3',
        visibility: 'collapsed',
        step_id: 'step_9',
        duration_ms: 812,
      }),
      parts: [],
      metadata: { tool_name: 'search_memory', status: 'done', member_summary: { kind: 'memory_search', state: 'done' } },
    },
  };
}

/** Ruling 5's frontend half, in the compatibility shape the backend emits:
 * a failed web_fetch whose envelope keeps status/duration_ms/step_id and
 * whose result slot is emptied ("" not deleted). */
function compatibilityFailedFetchEnvelope(): Record<string, unknown> {
  return {
    schema: 'hive.session_event_compatibility',
    schema_version: 1,
    compatibility_status: 'needs_reconciliation',
    reason: 'insufficient_legacy_scope',
    legacy_kind: 'tool_result',
    legacy_lifecycle: 'legacy_unknown',
    legacy_event_type: 'tool_result',
    legacy_item_type: 'tool_result',
    legacy_item_status: 'succeeded',
    event_id: 'compat-x3-failed-fetch',
    sequence: 1,
    tenant_id: 'tenant-1',
    session_id: 'session-1',
    occurred_at: '2026-11-12T00:00:03Z',
    payload: {
      content: JSON.stringify({
        args: { url: 'https://deepwiki.com/react' },
        duration_ms: 812,
        name: 'web_fetch',
        result: '',
        status: 'failed',
        step_id: 'step_9',
        tool_call_id: 'call_x3',
        visibility: 'collapsed',
      }),
      parts: [],
      metadata: { tool_name: 'web_fetch', status: 'failed', member_summary: { kind: 'web_fetch', state: 'not_done', host: 'deepwiki.com' } },
    },
  };
}

function toolMessagesOf(messages: AgentChatMessage[]): AgentChatMessage[] {
  return messages.filter((message) => message.role === 'tool_call');
}

describe('member tool summary through the session planes', () => {
  it('live delivery carries the typed summary and no raw bytes', () => {
    let store: SessionEventStore | undefined;
    for (const envelope of summarizedToolPair()) {
      store = consumeSessionEnvelope(envelope as unknown as ChatTranscriptEventPayload, store, 0).store;
    }
    if (!store) throw new Error('fixture_did_not_create_store');
    const messages = toolMessagesOf(projectSessionEventStoreToMessages(store));
    const result = messages.find((message) => message.toolName === 'load_memory');
    expect(result?.toolMemberSummary).toEqual({ kind: 'memory_read', state: 'done' });
    // The rendered result text is empty; typed args (the member's own
    // request) remain, but no result prose or management notes arrive.
    expect(result?.toolRawResult ?? '').toBe('');
    expect(result?.toolResult ?? '').toBe('');
    expect(JSON.stringify(messages)).not.toContain('不要向用户展示');
    expect(JSON.stringify(messages)).not.toContain('## 读取到的记忆');
  });

  it('reload hydration shows the same sentence as live', () => {
    const live: SessionEventStore | undefined = summarizedToolPair().reduce(
      (store: SessionEventStore | undefined, envelope) =>
        consumeSessionEnvelope(envelope as unknown as ChatTranscriptEventPayload, store, 0).store,
      undefined,
    );
    const reloaded = hydrateSessionTranscriptEvents(
      summarizedToolPair() as unknown as ChatTranscriptEventPayload[],
      0,
    );
    const liveSummary = toolMessagesOf(projectSessionEventStoreToMessages(live!)).find((m) => m.toolName === 'load_memory')?.toolMemberSummary;
    const reloadSummary = toolMessagesOf(reloaded.messages).find((m) => m.toolName === 'load_memory')?.toolMemberSummary;
    expect(reloadSummary).toEqual(liveSummary);
    // The expanded step the member opens renders the summary, not raw text.
    const timeline = buildRunTimelineFromMessages(reloaded.messages);
    const details = timeline?.steps.find((step) => step.details && (step.details as Record<string, unknown>).memberSummary)?.details as Record<string, unknown>;
    expect(details?.memberSummary).toEqual({ kind: 'memory_read', state: 'done' });
  });

  it('the compatibility plane (persisted V1 tool results) carries the summary through reload', () => {
    const reloaded = hydrateSessionTranscriptEvents(
      [...summarizedToolPair(), compatibilityToolEnvelope()] as unknown as ChatTranscriptEventPayload[],
      0,
    );
    const compat = toolMessagesOf(reloaded.messages).find((message) => message.toolName === 'search_memory');
    expect(compat?.toolMemberSummary).toEqual({ kind: 'memory_search', state: 'done' });
    expect(compat?.toolRawResult ?? '').toBe('');
    expect(JSON.stringify(reloaded.messages)).not.toContain(MEMORY_BODY);
  });

  it('a non-summarized tool keeps its raw result through both planes', () => {
    const call: SessionEventV2 = {
      ...canonicalEvent(1), item_id: 'call', item_kind: 'tool_call',
      kind: 'tool_call.completed', payload_schema: 'hive.session.payload.tool_call.completed.v2',
      invocation_id: 'inv-plain',
      payload: { tool_name: 'read_file', args: { path: 'uploads/sales.csv' } },
    };
    const result: SessionEventV2 = {
      ...canonicalEvent(2), item_id: 'result', item_kind: 'tool_result',
      kind: 'tool_result.completed', payload_schema: 'hive.session.payload.tool_result.completed.v2',
      invocation_id: 'inv-plain',
      payload: { tool_name: 'read_file', status: 'completed', result: 'date,sales\n2026-11-01,35', content: '' },
    };
    const reloaded = hydrateSessionTranscriptEvents([call, result] as unknown as ChatTranscriptEventPayload[], 0);
    const plain = toolMessagesOf(reloaded.messages).find((message) => message.toolName === 'read_file');
    expect(plain?.toolMemberSummary).toBeUndefined();
    expect(plain?.toolRawResult).toContain('2026-11-01,35');
  });

  it('a colleague still working carries the in_progress state through the real projection (r2 ruling 2)', () => {
    const call: SessionEventV2 = {
      ...canonicalEvent(1), item_id: 'call', item_kind: 'tool_call',
      kind: 'tool_call.completed', payload_schema: 'hive.session.payload.tool_call.completed.v2',
      invocation_id: 'inv-running',
      payload: { tool_name: 'check_async_task', args: { task_id: 't-1' } },
    };
    // Copied verbatim from the backend test output of
    // serialize_session_event(..., audience="user") for a running delegation
    // check (r3 ruling 6): content and result emptied, no raw JSON, the
    // typed summary attached.
    const result: SessionEventV2 = {
      ...canonicalEvent(2), item_id: 'result', item_kind: 'tool_result',
      kind: 'tool_result.completed', payload_schema: 'hive.session.payload.tool_result.completed.v2',
      invocation_id: 'inv-running',
      payload: {
        content: '',
        content_hash: 'hhhhhhhh',
        content_or_error_ref: 'receipt:x3',
        invocation_id: 'inv-running',
        member_summary: { kind: 'colleague_result', state: 'in_progress' },
        model_visible_content: '',
        outcome: 'success',
        parts: [],
        provider_request_id: 'req-x3',
        provider_tool_use_id: 'ptu-x3',
        result: '',
        retryable: false,
        tool_name: 'check_async_task',
      },
    };
    const reloaded = hydrateSessionTranscriptEvents([call, result] as unknown as ChatTranscriptEventPayload[], 0);
    const message = toolMessagesOf(reloaded.messages).find((m) => m.toolName === 'check_async_task');
    expect(message?.toolMemberSummary).toEqual({ kind: 'colleague_result', state: 'in_progress' });
    const timeline = buildRunTimelineFromMessages(reloaded.messages);
    const details = timeline?.steps.find((step) => (step.details as Record<string, unknown>)?.memberSummary)?.details as Record<string, unknown>;
    expect((details?.memberSummary as Record<string, unknown>)?.state).toBe('in_progress');
  });

  it('a standalone non-summarized result keeps its old title (r2 ruling 9 / N4)', () => {
    // Only summarized tools take a standalone result's title from its payload
    // tool_name; read_file keeps the pre-X3 generic title behavior.
    const result: SessionEventV2 = {
      ...canonicalEvent(1), item_id: 'result', item_kind: 'tool_result',
      kind: 'tool_result.completed', payload_schema: 'hive.session.payload.tool_result.completed.v2',
      invocation_id: 'inv-standalone',
      payload: { tool_name: 'read_file', invocation_id: 'inv-standalone', outcome: 'success', content: 'a,b\n1,2', parts: [] },
    };
    const reloaded = hydrateSessionTranscriptEvents([result] as unknown as ChatTranscriptEventPayload[], 0);
    const message = toolMessagesOf(reloaded.messages)[0];
    expect(message?.toolName).toBeUndefined();
    const summarized: SessionEventV2 = {
      ...result, event_id: 'event-summarized', item_id: 'result-summarized',
      payload: { tool_name: 'load_memory', invocation_id: 'inv-s2', outcome: 'success', content: '', member_summary: { kind: 'memory_read', state: 'done' }, parts: [] },
    };
    const withSummary = hydrateSessionTranscriptEvents([summarized] as unknown as ChatTranscriptEventPayload[], 0);
    expect(toolMessagesOf(withSummary.messages)[0]?.toolName).toBe('load_memory');
  });

  it('not-done outcomes mark the step failed or cancelled, agreeing with the sentence (r2 ruling 3)', () => {
    for (const [outcome, expected] of [['denied', 'failed'], ['aborted', 'cancelled'], ['cancelled', 'cancelled'], ['unavailable', 'failed']] as const) {
      const result: SessionEventV2 = {
        ...canonicalEvent(1), item_id: `result-${outcome}`, item_kind: 'tool_result',
        kind: 'tool_result.completed', payload_schema: 'hive.session.payload.tool_result.completed.v2',
        invocation_id: `inv-${outcome}`,
        payload: { tool_name: 'web_fetch', invocation_id: `inv-${outcome}`, outcome, content: '', parts: [] },
      };
      const reloaded = hydrateSessionTranscriptEvents([result] as unknown as ChatTranscriptEventPayload[], 0);
      const timeline = buildRunTimelineFromMessages(reloaded.messages);
      expect(timeline?.steps[0]?.status).toBe(expected);
    }
  });

  it('the summary state owns the step status when a refusal rides a success frame (r3 ruling 4)', () => {
    // Copied from backend output: a stale_resource refusal page arrives with
    // outcome success; the step must still read as failed.
    const stale: SessionEventV2 = {
      ...canonicalEvent(1), item_id: 'result-stale', item_kind: 'tool_result',
      kind: 'tool_result.completed', payload_schema: 'hive.session.payload.tool_result.completed.v2',
      invocation_id: 'inv-stale',
      payload: {
        content: '', content_hash: 'hhhhhhhh', content_or_error_ref: 'receipt:x3',
        invocation_id: 'inv-stale',
        member_summary: { kind: 'colleague_result', state: 'not_done' },
        model_visible_content: '', outcome: 'success', parts: [],
        provider_request_id: 'req-x3', provider_tool_use_id: 'ptu-x3',
        result: '', retryable: false, tool_name: 'read_runtime_result',
      },
    };
    const staleTimeline = buildRunTimelineFromMessages(
      hydrateSessionTranscriptEvents([stale] as unknown as ChatTranscriptEventPayload[], 0).messages,
    );
    expect(staleTimeline?.steps[0]?.status).toBe('failed');

    // check_async_task forbidden page, outcome success -> failed.
    const forbidden: SessionEventV2 = {
      ...canonicalEvent(1), event_id: 'event-forbidden', item_id: 'result-forbidden', item_kind: 'tool_result',
      kind: 'tool_result.completed', payload_schema: 'hive.session.payload.tool_result.completed.v2',
      invocation_id: 'inv-forbidden',
      payload: {
        content: '', content_hash: 'hhhhhhhh', content_or_error_ref: 'receipt:x3',
        invocation_id: 'inv-forbidden',
        member_summary: { kind: 'colleague_result', state: 'not_done' },
        model_visible_content: '', outcome: 'success', parts: [],
        provider_request_id: 'req-x3', provider_tool_use_id: 'ptu-x3',
        result: '', retryable: false, tool_name: 'check_async_task',
      },
    };
    const forbiddenTimeline = buildRunTimelineFromMessages(
      hydrateSessionTranscriptEvents([forbidden] as unknown as ChatTranscriptEventPayload[], 0).messages,
    );
    expect(forbiddenTimeline?.steps[0]?.status).toBe('failed');

    // An unknown outcome maps to failed — no existing member step status
    // distinguishes unconfirmed (chatDisclosureReducer has no unknown
    // mapping; RunDisclosureBlock renders the sentence on the text layer).
    const unknown: SessionEventV2 = {
      ...canonicalEvent(1), event_id: 'event-unknown', item_id: 'result-unknown', item_kind: 'tool_result',
      kind: 'tool_result.completed', payload_schema: 'hive.session.payload.tool_result.completed.v2',
      invocation_id: 'inv-unknown',
      payload: {
        content: '', content_hash: 'hhhhhhhh', content_or_error_ref: 'receipt:x3',
        invocation_id: 'inv-unknown',
        member_summary: { kind: 'web_fetch', state: 'unknown' },
        model_visible_content: '', outcome: 'unknown', parts: [],
        provider_request_id: 'req-x3', provider_tool_use_id: 'ptu-x3',
        result: '', retryable: false, tool_name: 'web_fetch',
      },
    };
    const unknownTimeline = buildRunTimelineFromMessages(
      hydrateSessionTranscriptEvents([unknown] as unknown as ChatTranscriptEventPayload[], 0).messages,
    );
    expect(unknownTimeline?.steps[0]?.status).toBe('failed');

    // approval_required keeps the existing blocked mapping (N3).
    const approval: SessionEventV2 = {
      ...canonicalEvent(1), event_id: 'event-approval', item_id: 'result-approval', item_kind: 'tool_result',
      kind: 'tool_result.completed', payload_schema: 'hive.session.payload.tool_result.completed.v2',
      invocation_id: 'inv-approval',
      payload: {
        content: '', content_hash: 'hhhhhhhh', content_or_error_ref: 'receipt:x3',
        invocation_id: 'inv-approval',
        member_summary: { kind: 'web_fetch', state: 'not_done' },
        model_visible_content: '', outcome: 'approval_required', parts: [],
        provider_request_id: 'req-x3', provider_tool_use_id: 'ptu-x3',
        result: '', retryable: false, tool_name: 'web_fetch',
      },
    };
    const approvalTimeline = buildRunTimelineFromMessages(
      hydrateSessionTranscriptEvents([approval] as unknown as ChatTranscriptEventPayload[], 0).messages,
    );
    expect(approvalTimeline?.steps[0]?.status).toBe('blocked');
  });

  it('a failed compatibility fetch keeps its failed status, duration and step id (r3 ruling 6)', () => {
    const reloaded = hydrateSessionTranscriptEvents(
      [compatibilityFailedFetchEnvelope()] as unknown as ChatTranscriptEventPayload[],
      0,
    );
    const timeline = buildRunTimelineFromMessages(reloaded.messages);
    const step = timeline?.steps[0];
    // The step is failed, the duration the UI reads from runtime_step meta
    // survives, and the summary carries the not-done state.
    expect(step?.status).toBe('failed');
    expect(step?.durationMs).toBe(812);
    expect((step?.details as Record<string, unknown>)?.memberSummary)
      .toEqual({ kind: 'web_fetch', state: 'not_done', host: 'deepwiki.com' });
    // No raw page body anywhere in the messages.
    expect(JSON.stringify(reloaded.messages)).not.toContain('Firecrawl');
  });
});
