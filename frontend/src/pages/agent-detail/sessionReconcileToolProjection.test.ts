import { describe, expect, it, vi } from 'vitest';

import type { AgentChatMessage } from './chatRuntime';
import { isDedicatedToolCardMessage } from './chatDisclosureReducer';
import {
  applyCanonicalSessionSnapshot,
  consumeSessionEnvelope,
  mergeCanonicalTerminalMessages,
} from './sessionEventConsumer';
import { projectSessionSocketEvent, type SessionSocketProjectionDependencies } from './sessionSocketEventProjector';
import type { SessionSocketMessageContext } from './useSessionTransportController';
import type { SessionEventStore, SessionEventV2 } from '../session-workbench/sessionEventStore';

const ROUND_SCOPE = {
  level: 'round',
  session_id: 'session-1',
  thread_id: 'session-1',
  turn_id: 'turn-1',
  run_id: 'run-1',
  round_id: 'round-1',
} as const;

function canonicalEvent(overrides: {
  sequence: number;
  event_id: string;
  item_id: string;
  item_kind: string;
  lifecycle: string;
  actor: SessionEventV2['actor'];
  payload: Record<string, unknown>;
  scope?: SessionEventV2['scope'];
  invocation_id?: string;
  provider_tool_use_id?: string;
}): SessionEventV2 {
  return {
    schema: 'hive.session_event',
    schema_version: 2,
    kind: `${overrides.item_kind}.${overrides.lifecycle}`,
    payload_schema: `hive.session.payload.${overrides.item_kind}.${overrides.lifecycle}.v2`,
    tenant_id: 'tenant-1',
    visibility: { audience: 'direct_user' },
    occurred_at: `2026-08-27T00:00:0${overrides.sequence}Z`,
    persisted_at: `2026-08-27T00:00:0${overrides.sequence}Z`,
    scope: overrides.scope ?? ROUND_SCOPE,
    ...overrides,
  } as SessionEventV2;
}

/**
 * Exact production shapes for the DAY1-LIVE-TAIL-001 durable tail.
 *
 * The canonical payloads mirror the current backend writers byte-for-shape:
 * `tool_call.started` carries `tool_name` + `args_hash` (never the raw
 * arguments); `tool_result.completed` carries the tool output as the `content`
 * string with NO `tool_name`/`result` field and no `parent_item_id` — the
 * frontend pairs call and result through `invocation_id`. The preview payload
 * mirrors `hr_creation_draft_payload` over `_build_blueprint_preview_payload`.
 */
const PREVIEW_RESULT = JSON.stringify({
  status: 'preview',
  blueprint_id: '0f0e8d2a-6f6d-4f6e-9c2d-111111111111',
  blueprint_version: 2,
  blueprint_hash: 'blueprint-hash-1',
  draft_status: 'awaiting_confirmation',
  hr_agent_id: 'hr-agent-1',
  session_id: 'session-1',
  blueprint: {
    name: '数据分析师',
    role_description: '为运营团队产出每周经营分析',
    primary_users: ['运营团队'],
    core_outputs: ['每周经营分析报告'],
    boundaries: '不对外发送任何数据',
    permission_scope: 'standard',
    source_attributions: [],
    ready_now: ['builtin tools'],
    deferred_capabilities: [],
  },
  summary: {
    mission: '为运营团队产出每周经营分析',
    first_mission: '完成本周经营分析初稿',
    primary_users: ['运营团队'],
    core_outputs: ['每周经营分析报告'],
  },
  risk_class: 'standard',
  missing_gates: [],
  ready_now: ['builtin tools'],
  will_install: [],
  manual_steps: [],
  warnings: [],
  knowledge_debt: {},
  confirmation_requirements: {},
});

const LIVE_INPUT_ACCEPTED: SessionEventV2 = canonicalEvent({
  sequence: 1,
  event_id: 'event-input-1',
  item_id: 'input-1',
  item_kind: 'human_input',
  lifecycle: 'accepted',
  actor: { type: 'user', id: 'user-1' },
  scope: { level: 'session', session_id: 'session-1', thread_id: 'session-1' },
  payload: { content_parts: [{ type: 'text', text: '帮我创建一个数据分析师' }], intent: 'start_turn' },
});

const LOST_TAIL: SessionEventV2[] = [
  canonicalEvent({
    sequence: 2,
    event_id: 'event-tool-call-started-1',
    item_id: 'tool-call-1',
    item_kind: 'tool_call',
    lifecycle: 'started',
    actor: { type: 'tool' },
    invocation_id: 'invocation-1',
    provider_tool_use_id: 'toolu-1',
    payload: {
      tool_name: 'preview_agent_blueprint',
      invocation_id: 'invocation-1',
      provider_request_id: 'provider-req-1',
      provider_tool_use_id: 'toolu-1',
      args_hash: 'args-hash-1',
      authority_snapshot_hash: 'authority-hash-1',
      authority_snapshot_ref: 'session-model-result:model-result-1',
      effect_idempotency_key: 'session-tool:invocation-1',
      effect_state: 'prepared_not_started',
    },
  }),
  canonicalEvent({
    sequence: 3,
    event_id: 'event-tool-call-completed-1',
    item_id: 'tool-call-1',
    item_kind: 'tool_call',
    lifecycle: 'completed',
    actor: { type: 'tool' },
    invocation_id: 'invocation-1',
    provider_tool_use_id: 'toolu-1',
    payload: {
      invocation_id: 'invocation-1',
      provider_request_id: 'provider-req-1',
      provider_tool_use_id: 'toolu-1',
      outcome: 'success',
      retryable: false,
      decision_id: 'decision-1',
      execution_fence_ref: 'session-tool-effect:invocation-1:generation:1',
      receipt_ref: 'tool-decision:decision-1',
    },
  }),
  canonicalEvent({
    sequence: 4,
    event_id: 'event-tool-result-1',
    item_id: 'tool-result-1',
    item_kind: 'tool_result',
    lifecycle: 'completed',
    actor: { type: 'tool' },
    invocation_id: 'invocation-1',
    provider_tool_use_id: 'toolu-1',
    payload: {
      invocation_id: 'invocation-1',
      provider_request_id: 'provider-req-1',
      provider_tool_use_id: 'toolu-1',
      outcome: 'success',
      retryable: false,
      content: PREVIEW_RESULT,
      content_hash: 'content-hash-1',
      content_or_error_ref: 'tool-decision:decision-1',
      parts: [],
    },
  }),
  canonicalEvent({
    sequence: 5,
    event_id: 'event-assistant-text-1',
    item_id: 'assistant-1',
    item_kind: 'assistant_text',
    lifecycle: 'completed',
    actor: { type: 'assistant' },
    payload: { phase: 'unknown', content: '蓝图已准备好，请在卡片中确认。' },
  }),
  canonicalEvent({
    sequence: 6,
    event_id: 'event-assistant-final-1',
    item_id: 'assistant-final-1',
    item_kind: 'assistant_final',
    lifecycle: 'completed',
    actor: { type: 'assistant' },
    payload: {
      phase: 'final',
      render_owner_id: 'render-owner-1',
      source_blocks: [{ item_id: 'assistant-1', block_index: 0, content_hash: 'text-hash-1' }],
    },
  }),
  canonicalEvent({
    sequence: 7,
    event_id: 'event-run-completed-1',
    item_id: 'run-1',
    item_kind: 'run',
    lifecycle: 'completed',
    actor: { type: 'runtime' },
    scope: {
      level: 'run',
      session_id: 'session-1',
      thread_id: 'session-1',
      turn_id: 'turn-1',
      run_id: 'run-1',
    },
    payload: {},
  }),
];

describe('DAY1-LIVE-TAIL-001 terminal reconcile vertical projection', () => {
  it('reconciles the lost durable tail into a UI-consumable hr_preview card without reload', () => {
    // 1. The reconcile backfill replays the durable tail through the exact
    //    production consumption path (AgentDetail applyTranscriptToSession →
    //    consumeSessionEnvelope → applyCanonicalSessionSnapshot with the same
    //    onMessages terminal-merge semantics). The store/visible/consume
    //    closure is built BEFORE the projector so the terminal trigger below
    //    can drive this real replay — the two halves are one connected test.
    let store: SessionEventStore | undefined;
    let visible: AgentChatMessage[] = [];
    const consume = (event: SessionEventV2) => {
      const consumed = consumeSessionEnvelope(
        event as unknown as import('./chatRuntime').ChatTranscriptEventPayload,
        store,
        0,
      );
      if (!consumed.canonical || !consumed.store) {
        throw new Error('fixture_expected_canonical_envelope');
      }
      store = consumed.store;
      applyCanonicalSessionSnapshot({
        event: consumed.projectionEvent,
        store,
        active: true,
        onTranscript: () => undefined,
        onActivity: () => undefined,
        onTerminal: () => undefined,
        onMessages: (messages, terminal, runId) => {
          visible = terminal
            ? mergeCanonicalTerminalMessages(visible, messages, runId)
            : messages;
        },
      });
    };

    // The live input is already projected before the terminal boundary; the
    // reconcile must only backfill the lost durable tail after it.
    consume(LIVE_INPUT_ACCEPTED);
    expect(visible).toHaveLength(1);

    // 2. Live terminal done frame (immediate channel): the terminal reconcile
    //    trigger and the runtime/read-model invalidation must still fire. The
    //    reconcile dependency is a vi.fn spy ONLY for call-count/argument
    //    assertion — its implementation is the REAL canonical replay of the
    //    durable tail (LOST_TAIL.forEach(consume)). There is no manual tail
    //    replay after the projector: deleting either the projector's terminal
    //    trigger (reconcile never runs → card assertions fail) or the
    //    canonical consumer (consume never runs → same) fails THIS test.
    const closeSessionSocket = vi.fn();
    const failAuthentication = vi.fn();
    const context: SessionSocketMessageContext = {
      data: { type: 'done', content: '蓝图已准备好，请在卡片中确认。', run_id: 'run-1' },
      session: { id: 'session-1' },
      agentId: 'agent-1',
      sessionId: 'session-1',
      key: 'agent-1:session-1',
      isActiveRuntime: true,
      closeSessionSocket,
      failAuthentication,
    };
    const dependencies = {
      applyTranscriptToSession: vi.fn(),
      selectSession: vi.fn(),
      fetchMySessions: vi.fn(),
      setSessionPhase: vi.fn(),
      sessionPhaseOf: vi.fn(() => 'responding' as const),
      syncActivePhase: vi.fn(),
      setActiveRunState: vi.fn(),
      markActiveRunTerminal: vi.fn(),
      invalidateSessionRuntimeQueries: vi.fn(),
      reconcileSessionTranscript: vi.fn(() => {
        for (const event of LOST_TAIL) {
          consume(event);
        }
      }),
      shouldInvalidateToolCall: vi.fn(() => true),
      isTerminalTranscriptToolMessage: vi.fn(() => false),
      normalizeToolCallMessage: vi.fn((message: AgentChatMessage) => message),
      parseChatMsg: vi.fn((message: AgentChatMessage) => message),
      setChatMessagesSessionId: vi.fn(),
      setTransportNotice: vi.fn(),
      enqueueChatMessagesUpdate: vi.fn(),
      setChatMessagesAfterQueued: vi.fn(),
      setCreatedAgentId: vi.fn(),
      setAgentExpired: vi.fn(),
      invalidateQuery: vi.fn(),
    } as unknown as SessionSocketProjectionDependencies;

    projectSessionSocketEvent(context, dependencies);

    expect(dependencies.reconcileSessionTranscript).toHaveBeenCalledTimes(1);
    expect(dependencies.reconcileSessionTranscript).toHaveBeenCalledWith('agent-1', 'session-1');
    expect(dependencies.invalidateSessionRuntimeQueries).toHaveBeenCalledWith('agent-1', 'session-1');
    expect(dependencies.markActiveRunTerminal).toHaveBeenCalledWith('agent-1:session-1', 'run-1');

    // 3. The structured artifact is consumable from the reconcile result alone:
    //    exactly one preview_agent_blueprint card, hr_preview tool meta with the
    //    blueprint identity preserved, and byte-faithful raw evidence.
    const toolCards = visible.filter((message) => message.role === 'tool_call');
    expect(toolCards).toHaveLength(1);
    const card = toolCards[0];
    expect(card.toolName).toBe('preview_agent_blueprint');
    expect(card.toolStatus).toBe('done');
    expect(card.toolMeta?.kind).toBe('hr_preview');
    expect(card.toolMeta && card.toolMeta.kind === 'hr_preview' ? card.toolMeta : null).toMatchObject({
      blueprintId: '0f0e8d2a-6f6d-4f6e-9c2d-111111111111',
      blueprintVersion: 2,
      status: 'awaiting_confirmation',
      name: '数据分析师',
      mission: '为运营团队产出每周经营分析',
      firstMission: '完成本周经营分析初稿',
      primaryUsers: ['运营团队'],
      coreOutputs: ['每周经营分析报告'],
      riskClass: 'standard',
    });
    expect(JSON.parse(String(card.toolRawResult))).toMatchObject({
      status: 'preview',
      blueprint_id: '0f0e8d2a-6f6d-4f6e-9c2d-111111111111',
      draft_status: 'awaiting_confirmation',
    });

    // The chat surface renders the structured card only for dedicated tool-card
    // meta kinds (chatDisclosureReducer → StructuredToolResultBody →
    // HrBlueprintPreviewCard); without toolMeta the row renders nothing.
    expect(isDedicatedToolCardMessage(card)).toBe(true);

    // The terminal answer is preserved beside the card — reconcile must not
    // reduce the turn to the done text alone.
    expect(visible.at(-1)).toMatchObject({
      role: 'assistant',
      content: '蓝图已准备好，请在卡片中确认。',
    });
    expect(visible[0]).toMatchObject({ role: 'user', content: '帮我创建一个数据分析师' });
  });
});
