import { describe, expect, it } from 'vitest';

import type { ChatTranscriptEventPayload } from './chatRuntime';
import { buildRunTimelineFromMessages } from './chatDisclosureReducer';
import {
  consumeSessionEnvelope,
  hydrateSessionTranscriptEvents,
  projectSessionEventStoreToMessages,
} from './sessionEventConsumer';
import type { SessionEventStore, SessionEventV2 } from '../session-workbench/sessionEventStore';

// X3c: run 19 proved production member events never carry the arguments — a
// native tool_call has only args_hash, a native tool_result has no args. The
// names the member sees (file base name, fetch host, trusted MCP app label)
// therefore arrive only through the backend's typed member summary, and these
// fixtures copy the production member-transcript shapes exactly. Every case
// runs through the real live consumer and the real reload hydration.

let eventCounter = 0;

/** The production member shape of one settled tool call: a tool_call pair
 * (started carries only args_hash) and a tool_result without args. */
function productionToolPair(
  toolName: string,
  resultPayload: Record<string, unknown>,
  callExtra: Record<string, unknown> = {},
): SessionEventV2[] {
  // Each returned pair is its own fresh session fixture: identities are
  // unique per call and sequences start at 1 (a new store's delivery ledger
  // buffers a leading gap forever).
  const invocation = `inv-x3c-${toolName}-${Date.now()}-${Math.random().toString(36).slice(2, 8)}`;
  let localSequence = 0;
  const base = (itemKind: 'tool_call' | 'tool_result', lifecycle: string) => {
    const sequence = ++localSequence;
    return {
      schema: 'hive.session_event' as const,
      schema_version: 2,
      event_id: `event-${sequence}`,
      sequence,
      tenant_id: 'tenant-x3c',
      scope: { level: 'round', session_id: 'session-x3c', thread_id: 'session-x3c', turn_id: 'turn-x3c', run_id: 'run-x3c', round_id: 'round-x3c' },
      item_id: `${itemKind}-${invocation}`,
      item_kind: itemKind,
      kind: `${itemKind}.${lifecycle}`,
      lifecycle,
      payload_schema: `hive.session.payload.${itemKind}.${lifecycle}.v2`,
      actor: { type: itemKind === 'tool_call' ? 'assistant' : 'tool' },
      visibility: { audience: 'direct_user' },
      invocation_id: invocation,
      occurred_at: '2026-11-13T16:20:38.000Z',
      persisted_at: '2026-11-13T16:20:38.000Z',
    };
  };
  return [
    {
      ...base('tool_call', 'started'),
      payload: { tool_name: toolName, invocation_id: invocation, args_hash: 'a'.repeat(64), ...callExtra },
    } as SessionEventV2,
    {
      ...base('tool_call', 'completed'),
      payload: { invocation_id: invocation, outcome: 'success', retryable: false, ...callExtra },
    } as SessionEventV2,
    {
      ...base('tool_result', 'completed'),
      payload: {
        invocation_id: invocation,
        tool_name: toolName,
        outcome: 'success',
        retryable: false,
        content: '',
        content_hash: 'h'.repeat(8),
        parts: [],
        ...resultPayload,
      },
    } as SessionEventV2,
  ];
}

function toolMessageOf(events: SessionEventV2[], toolName: string) {
  const reloaded = hydrateSessionTranscriptEvents(events as unknown as ChatTranscriptEventPayload[], 0);
  let live: SessionEventStore | undefined;
  for (const envelope of events) {
    live = consumeSessionEnvelope(envelope as unknown as ChatTranscriptEventPayload, live, 0).store;
  }
  const reloadMessage = reloaded.messages.find((message) => message.toolName === toolName);
  const liveMessage = live
    ? projectSessionEventStoreToMessages(live).find((message) => message.toolName === toolName)
    : undefined;
  return { reloadMessage, liveMessage, reloaded };
}

describe('X3c production-shaped rows name the target through both planes', () => {
  it('a read row names the file from the typed member summary', () => {
    const { reloadMessage, liveMessage } = toolMessageOf(productionToolPair('read_file', {
      member_summary: { kind: 'file_read', state: 'done', filename: 'sales-run19.csv' },
    }), 'read_file');
    for (const message of [reloadMessage, liveMessage]) {
      expect(message?.toolMemberSummary?.filename).toBe('sales-run19.csv');
    }
    // The typed arguments never arrive; nothing else names the file.
    expect(reloadMessage?.toolArgs).toBeUndefined();
  });

  it('a web row names the host, and a failed fetch names the host that did not open', () => {
    const ok = toolMessageOf(productionToolPair('web_fetch', {
      member_summary: { kind: 'web_fetch', state: 'done', host: 'deepwiki.com' },
    }), 'web_fetch');
    expect(ok.reloadMessage?.toolMemberSummary?.host).toBe('deepwiki.com');
    expect(ok.liveMessage?.toolMemberSummary?.host).toBe('deepwiki.com');

    const failed = toolMessageOf(productionToolPair('firecrawl_fetch', {
      member_summary: { kind: 'web_fetch', state: 'not_done', host: 'example.org' },
    }), 'firecrawl_fetch');
    const timeline = buildRunTimelineFromMessages(failed.reloaded.messages);
    expect(timeline?.steps.at(-1)?.status).toBe('failed');
  });

  it('write and edit rows carry their own summarized kinds', () => {
    for (const [tool, kind, filename] of [
      ['write_file', 'file_write', 'weekly-report.md'],
      ['edit_file', 'file_edit', 'weekly-report.md'],
    ] as const) {
      const { reloadMessage, liveMessage } = toolMessageOf(productionToolPair(tool, {
        member_summary: { kind, state: 'done', filename },
      }), tool);
      expect(reloadMessage?.toolMemberSummary?.kind).toBe(kind);
      expect(reloadMessage?.toolMemberSummary?.filename).toBe(filename);
      expect(liveMessage?.toolMemberSummary?.filename).toBe(filename);
    }
  });

  it('a company MCP row names the trusted connection label', () => {
    const { reloadMessage, liveMessage } = toolMessageOf(productionToolPair('mcp_ask_wiki_question', {
      member_summary: { kind: 'mcp_app', state: 'done', app_label: 'DeepWiki' },
    }), 'mcp_ask_wiki_question');
    expect(reloadMessage?.toolMemberSummary?.appLabel).toBe('DeepWiki');
    expect(liveMessage?.toolMemberSummary?.appLabel).toBe('DeepWiki');
  });

  it('old history rows without a typed target keep the generic summary', () => {
    const { reloadMessage } = toolMessageOf(productionToolPair('read_file', {
      member_summary: { kind: 'file_read', state: 'done' },
    }), 'read_file');
    expect(reloadMessage?.toolMemberSummary).toEqual({ kind: 'file_read', state: 'done' });
  });


  it('never shows a secret-shaped filename or a malformed host (X3c r2)', () => {
    const secret = toolMessageOf(productionToolPair('read_file', {
      member_summary: { kind: 'file_read', state: 'done', filename: 'client_secret.pem' },
    }), 'read_file');
    // The record-level guard refuses the name even if a stale row carries it.
    expect(secret.reloadMessage?.toolMemberSummary?.filename).toBeUndefined();

    const badHost = toolMessageOf(productionToolPair('web_fetch', {
      member_summary: { kind: 'web_fetch', state: 'done', host: 'user:pass@evil.example:443/a?token=1' },
    }), 'web_fetch');
    expect(badHost.reloadMessage?.toolMemberSummary?.host).toBeUndefined();
  });

  it('only a clean zero exit is done (X3c r2: null, non-number, timeout are not done)', () => {
    for (const execution of [
      { exit_code: null, timed_out: false },
      { exit_code: '1', timed_out: false },
      { exit_code: undefined, timed_out: false },
    ]) {
      const { reloaded } = toolMessageOf(productionToolPair('execute_code', {}, {
        code_execution_result: execution,
      }), 'execute_code');
      const timeline = buildRunTimelineFromMessages(reloaded.messages);
      expect(timeline?.steps.at(-1)?.status).toBe('failed');
    }
  });

  it('a command with a non-zero exit is failed; a zero exit stays done (run 19 row 16)', () => {
    const failed = toolMessageOf(productionToolPair('execute_code', {}, {
      code_execution_result: { exit_code: 1, timed_out: false },
    }), 'execute_code');
    const failedTimeline = buildRunTimelineFromMessages(failed.reloaded.messages);
    expect(failedTimeline?.steps.at(-1)?.status).toBe('failed');

    const timedOut = toolMessageOf(productionToolPair('execute_code', {}, {
      code_execution_result: { exit_code: null, timed_out: true },
    }), 'execute_code');
    expect(buildRunTimelineFromMessages(timedOut.reloaded.messages)?.steps.at(-1)?.status).toBe('failed');

    const clean = toolMessageOf(productionToolPair('execute_code', {}, {
      code_execution_result: { exit_code: 0, timed_out: false },
    }), 'execute_code');
    expect(buildRunTimelineFromMessages(clean.reloaded.messages)?.steps.at(-1)?.status).toBe('done');

    // No typed execution result at all: the status stays with the outcome.
    const unknown = toolMessageOf(productionToolPair('execute_code', {}), 'execute_code');
    expect(buildRunTimelineFromMessages(unknown.reloaded.messages)?.steps.at(-1)?.status).toBe('done');
  });
});
