import {
  TERMINAL_RUNTIME_PHASES,
  appendToolCallMessage,
  applyRuntimeDoneEvent,
  applyStreamingChunkEvent,
  extractArtifactParts,
  getRuntimeEventMessage,
  getTerminalRunIdFromTranscriptEvent,
  getTransportNotice,
  isRuntimePhase,
  isSameSessionRunId,
  isTerminalRealtimeChatEvent,
  isTerminalRunAcceptedForActiveRun,
  reduceRuntimePhase,
  terminalRuntimePhaseForSessionEvent,
  type AgentChatMessage,
  type ChatTranscriptEventPayload,
  type RuntimePhase,
  type SessionRunState,
} from './chatRuntime';
import { normalizeToolCallResult } from './toolResultEnvelope';
import { sessionRunStateFromPayload } from './runtimeBudgetState';
import { sessionPayloadContent, type SessionCompatibilityEvent, type SessionEventV2 } from '../session-workbench/sessionEventStore';
import {
  compatibilityProjectionEvent,
  isLegacyAssistantTerminalItem,
  type SessionTranscriptApplication,
} from './sessionEventConsumer';
import type { SessionSocketMessageContext } from './useSessionTransportController';

type MessageUpdater = (
  sessionId: string,
  updater: (messages: AgentChatMessage[]) => AgentChatMessage[],
) => void;

const TASK_LEDGER_MUTATION_TOOLS = new Set(['task_create', 'task_update', 'task_stop', 'track_todo']);
const RUNTIME_QUERY_EVENT_KINDS = new Set([
  'run',
  'subagent',
  'a2a_delegation',
  'a2a_receipt',
  'workflow_run',
  'workflow_step',
  'workflow_gate',
]);

function scopeRunId(scope: unknown): string | null {
  if (!scope || typeof scope !== 'object') return null;
  const runId = (scope as { run_id?: unknown }).run_id;
  return typeof runId === 'string' && runId.trim() ? runId : null;
}

/** Run identity carried by a terminal transcript witness — top-level run id
 * for raw/native frames, mapped legacy run id for compatibility envelopes. */
function terminalWitnessRunId(event: ChatTranscriptEventPayload): string | null {
  const direct = getTerminalRunIdFromTranscriptEvent(event);
  if (direct) return direct;
  const envelope = event as unknown as Record<string, unknown>;
  const payload = envelope.payload && typeof envelope.payload === 'object'
    ? envelope.payload as Record<string, unknown>
    : {};
  const legacyRunId = payload.legacy_run_id;
  if (typeof legacyRunId === 'string' && legacyRunId.trim()) return legacyRunId.trim();
  const runId = envelope.run_id;
  return typeof runId === 'string' && runId.trim() ? runId.trim() : null;
}

/** One terminal-effect step in total ascending event sequence, across planes. */
type TerminalEffectStep =
  | { sequence: number; canonical: SessionEventV2 }
  | { sequence: number; compatibility: SessionCompatibilityEvent };

function ascendingEffectSteps(
  canonicalEvents: SessionEventV2[],
  compatibilityEvents: SessionCompatibilityEvent[],
): TerminalEffectStep[] {
  const steps: TerminalEffectStep[] = [
    ...canonicalEvents.map((applied): TerminalEffectStep => ({
      sequence: Number(applied.sequence ?? 0),
      canonical: applied,
    })),
    ...compatibilityEvents.map((drained): TerminalEffectStep => ({
      sequence: Number(drained.sequence ?? 0),
      compatibility: drained,
    })),
  ];
  return steps.sort((left, right) => left.sequence - right.sequence);
}

export interface SessionSocketProjectionDependencies {
  // Returns this transition's application facts: the canonical events actually
  // applied to the contiguous item projection, in sequence order, plus whether
  // a compatibility carrier itself advanced the contiguous cursor.  The
  // boolean preserves the legacy contract ("carrier newly applied") for raw
  // legacy transcript frames.  Null/false means nothing was newly applied
  // (duplicate, rejected, buffered-only, or consistency conflict) and MUST
  // perform zero side effects — the transport is at-least-once and gaps hold
  // effects back until the missing sequence closes them.
  applyTranscriptToSession: (
    agentId: string,
    sessionId: string,
    event: ChatTranscriptEventPayload,
    isActiveRuntime: boolean,
  ) => SessionTranscriptApplication | boolean | null;
  // The currently active run id for the session key, or null when no run is
  // live.  Captured before envelope consumption so a matching terminal can
  // still be honored after the consumption callback cleared that active run.
  activeRunIdOf: (key: string) => string | null;
  selectSession: (session: any) => void | Promise<unknown>;
  fetchMySessions: (silent: boolean, agentId: string) => void | Promise<unknown>;
  setSessionPhase: (key: string, phase: RuntimePhase) => void;
  sessionPhaseOf: (key: string) => RuntimePhase;
  syncActivePhase: (phase: RuntimePhase) => void;
  setActiveRunState: (key: string, run: SessionRunState | null) => void;
  /** Identity-safe terminal bookkeeping. Returns whether the terminal was
   * accepted for the currently active run; terminal effects may run only on
   * acceptance (a stale old-run terminal is recorded but performs none). */
  markActiveRunTerminal: (key: string, runId?: string | null) => boolean;
  invalidateSessionRuntimeQueries: (agentId: string, sessionId: string, includeActiveRun?: boolean) => void;
  reconcileSessionTranscript: (agentId: string, sessionId: string) => void | Promise<unknown>;
  shouldInvalidateToolCall: (key: string) => boolean;
  isTerminalTranscriptToolMessage: (message: AgentChatMessage | undefined) => boolean;
  normalizeToolCallMessage: (message: AgentChatMessage) => AgentChatMessage;
  parseChatMsg: (message: AgentChatMessage) => AgentChatMessage;
  setChatMessagesSessionId: (sessionId: string) => void;
  setTransportNotice: (message: string) => void;
  enqueueChatMessagesUpdate: MessageUpdater;
  setChatMessagesAfterQueued: MessageUpdater;
  setCreatedAgentId: (agentId: string) => void;
  setAgentExpired: (expired: boolean) => void;
  invalidateQuery: (queryKey: unknown[]) => void;
}

function isCanonicalSessionEvent(value: any): boolean {
  return Boolean(
    value
    && value.schema === 'hive.session_event'
    && value.schema_version === 2
    && typeof value.event_id === 'string'
    && typeof value.sequence === 'number'
    && typeof value.kind === 'string',
  );
}

function isCompatibilitySessionEvent(value: any): boolean {
  return Boolean(
    value
      && value.schema === 'hive.session_event_compatibility'
      && value.schema_version === 1
      && typeof value.event_id === 'string'
      && typeof value.sequence === 'number'
      && typeof value.legacy_event_type === 'string',
  );
}

/**
 * Canonical side effects for exactly one applied canonical session event —
 * the single owner of the tool/run/failure consumption ladder.  Runs once per
 * applied event, in sequence order, for canonical carriers and for canonical
 * events drained by a gap-filling carrier alike.
 */
function runCanonicalEventSideEffects(
  dependencies: SessionSocketProjectionDependencies,
  context: { agentId: string; sessionId: string; key: string; isActiveRuntime: boolean },
  preConsumptionActiveRunId: string | null,
  event: SessionEventV2,
): void {
  const { agentId, sessionId, key, isActiveRuntime } = context;
  const {
    fetchMySessions,
    setSessionPhase,
    syncActivePhase,
    invalidateSessionRuntimeQueries,
  } = dependencies;

  const itemKind = String(event.item_kind || '').toLowerCase();
  const lifecycle = String(event.lifecycle || '').toLowerCase();
  const payload = event.payload && typeof event.payload === 'object' ? event.payload : {};
  const toolName = String(payload.tool_name || payload.name || '').toLowerCase();
  // Run-identity safety for every terminal branch: a stale old-run
  // terminal while a different run is active must not clear the active
  // run, set a terminal phase, or refresh/reconcile as that terminal.
  // Terminal acceptance and the active-run mutation itself are owned by the
  // applier's onTerminal callback (which needs the acceptance boolean for
  // the terminal message merge); this projector owns the observable
  // refresh effects only.
  const staleTerminalForActiveRun = (terminalRunId: string | null): boolean => Boolean(
    preConsumptionActiveRunId && terminalRunId
      && !isSameSessionRunId(preConsumptionActiveRunId, terminalRunId),
  );
  if (
    itemKind === 'tool_call'
    && TASK_LEDGER_MUTATION_TOOLS.has(toolName)
    && dependencies.shouldInvalidateToolCall(key)
  ) {
    invalidateSessionRuntimeQueries(agentId, sessionId, false);
  } else if (itemKind === 'tool_result') {
    // The started tool_call can arrive before the tool mutates the Work
    // Ledger. The committed result is the authoritative refresh boundary.
    invalidateSessionRuntimeQueries(agentId, sessionId, false);
  } else if (RUNTIME_QUERY_EVENT_KINDS.has(itemKind)) {
    invalidateSessionRuntimeQueries(agentId, sessionId);
  } else if (isLegacyAssistantTerminalItem(itemKind, lifecycle, payload) && !staleTerminalForActiveRun(scopeRunId(event.scope))) {
    // Turn-terminal witness of the legacy web-chat path: same contract as
    // the terminal stream frame — refresh the runtime read models the right
    // panel renders from, and reconcile the durable transcript so a lost
    // canonical tail still projects without a reload. The active-run
    // clearing itself already happened in the applier's onTerminal.
    const terminalPhase = terminalRuntimePhaseForSessionEvent(itemKind, lifecycle);
    invalidateSessionRuntimeQueries(agentId, sessionId);
    if (terminalPhase) {
      setSessionPhase(key, terminalPhase);
      if (isActiveRuntime) syncActivePhase(terminalPhase);
    }
    void fetchMySessions(true, agentId);
    if (isActiveRuntime) dependencies.reconcileSessionTranscript(agentId, sessionId);
  }
  if (itemKind === 'run' && ['completed', 'failed', 'cancelled'].includes(lifecycle) && !staleTerminalForActiveRun(scopeRunId(event.scope))) {
    const terminalPhase = terminalRuntimePhaseForSessionEvent(itemKind, lifecycle);
    if (terminalPhase) {
      setSessionPhase(key, terminalPhase);
      if (isActiveRuntime) syncActivePhase(terminalPhase);
    }
    void fetchMySessions(true, agentId);
  }
  // Only a run-scoped runtime_failure with a nonempty authoritative run_id
  // may produce terminal failure effects.  Session/turn/round scopes carry
  // no whole-run terminal authority (a session/turn scope has no run_id and
  // a round scope must not fail the whole run) — no null-id fallback here.
  const failureScopeLevel = event.scope && typeof event.scope === 'object'
    ? String((event.scope as { level?: unknown }).level || '')
    : '';
  const failureRunId = failureScopeLevel === 'run' ? scopeRunId(event.scope) : null;
  if (itemKind === 'runtime_failure' && lifecycle === 'recorded' && failureRunId && !staleTerminalForActiveRun(failureRunId)) {
    // Canonical terminal witness of the web-chat provider-failure path
    // (e.g. typed 402 quota_exhausted/rejected): same no-reload contract as
    // the terminal stream frame — pin the failed phase, refresh the runtime
    // read models, reconcile the durable transcript, and surface the
    // canonical user error card. The typed failure_code travels in the
    // payload; no natural-language scanning decides the quota outcome here,
    // and raw provider prose must not be duplicated through the transport
    // notice lane. The active-run closing already happened in the applier's
    // onTerminal.
    invalidateSessionRuntimeQueries(agentId, sessionId);
    setSessionPhase(key, 'failed');
    if (isActiveRuntime) syncActivePhase('failed');
    void fetchMySessions(true, agentId);
    if (isActiveRuntime) dependencies.reconcileSessionTranscript(agentId, sessionId);
  }
}

/** Terminal refresh effects of one applied compatibility envelope — an
 * applied carrier and every drained buffered envelope own them exactly once.
 * Run-identity safety: a stale old-run terminal stays durable evidence but
 * runs zero active terminal effects while a different run is live. */
function runCompatibilityTerminalEffects(
  dependencies: SessionSocketProjectionDependencies,
  context: { agentId: string; sessionId: string; key: string; isActiveRuntime: boolean },
  transcriptEvent: ChatTranscriptEventPayload,
  preConsumptionActiveRunId: string | null,
): void {
  if (!isTerminalRealtimeChatEvent(transcriptEvent)) return;
  if (!isTerminalRunAcceptedForActiveRun(preConsumptionActiveRunId, terminalWitnessRunId(transcriptEvent))) return;
  dependencies.invalidateSessionRuntimeQueries(context.agentId, context.sessionId);
  if (context.isActiveRuntime) void dependencies.fetchMySessions(true, context.agentId);
}

export function projectSessionSocketEvent(
  context: SessionSocketMessageContext,
  dependencies: SessionSocketProjectionDependencies,
): void {
  const { data, agentId, sessionId, key, isActiveRuntime } = context;
  const d = data;
  const {
    applyTranscriptToSession,
    fetchMySessions,
    setSessionPhase,
    sessionPhaseOf,
    syncActivePhase,
    setActiveRunState,
    markActiveRunTerminal,
    invalidateSessionRuntimeQueries,
    isTerminalTranscriptToolMessage,
    normalizeToolCallMessage,
    parseChatMsg,
  } = dependencies;

  if (isCanonicalSessionEvent(d)) {
    const transcriptEvent: ChatTranscriptEventPayload = {
      ...d,
      id: d.event_id,
      event_type: d.kind,
      content: sessionPayloadContent(d.payload),
      metadata: {
        ...(d.payload?.metadata || {}),
        session_event: d,
        item_id: d.item_id,
        item_kind: d.item_kind,
        lifecycle: d.lifecycle,
      },
    };
    // Pre-consumption run identity: consumption itself may clear a matching
    // active run via the terminal callback, so the staleness decision must
    // be made against the active run captured before consumption.
    const preConsumptionActiveRunId = dependencies.activeRunIdOf(key);
    const application = applyTranscriptToSession(agentId, sessionId, transcriptEvent, isActiveRuntime);
    // Contiguous-application contract: side effects run only for canonical
    // events actually applied to the contiguous item projection in this
    // transition — once per event, in sequence order (a gap close surfaces
    // the drained buffered events). Buffered-only, conflicted, ignored, and
    // duplicate arrivals report none and perform zero side effects.
    if (!application) return;
    const appliedCanonicalEvents: SessionEventV2[] = application === true
      ? [d as SessionEventV2]
      : application.canonicalEvents;
    const drainedCompatibility = application === true ? [] : application.compatibilityEvents;
    // Terminal effects run in total ascending event sequence across both
    // planes — never grouped by plane.
    for (const step of ascendingEffectSteps(appliedCanonicalEvents, drainedCompatibility)) {
      if ('canonical' in step) {
        runCanonicalEventSideEffects(dependencies, { agentId, sessionId, key, isActiveRuntime }, preConsumptionActiveRunId, step.canonical);
      } else {
        runCompatibilityTerminalEffects(
          dependencies,
          { agentId, sessionId, key, isActiveRuntime },
          compatibilityProjectionEvent(step.compatibility),
          preConsumptionActiveRunId,
        );
      }
    }
    return;
  }

  if (isCompatibilitySessionEvent(d)) {
    const payload = d.payload && typeof d.payload === 'object' ? d.payload : {};
    const transcriptEvent: ChatTranscriptEventPayload = {
      ...d,
      id: d.event_id,
      event_type: d.legacy_event_type,
      content: sessionPayloadContent(payload),
      metadata: payload.metadata && typeof payload.metadata === 'object'
        ? payload.metadata
        : {},
    };
    // Pre-consumption run identity, same as the canonical branch: drained
    // terminal events must be staleness-checked against the active run that
    // existed before this transition consumed anything.
    const preConsumptionActiveRunId = dependencies.activeRunIdOf(key);
    const application = applyTranscriptToSession(agentId, sessionId, transcriptEvent, isActiveRuntime);
    // A compatibility carrier can fill a sequence gap and drain buffered
    // canonical events in the same transition; those drained events own their
    // canonical side effects now, exactly once each.
    const compatibilityFacts = application && application !== true ? application : null;
    const carrierApplied = application === true || Boolean(compatibilityFacts?.compatibilityApplied);
    // A terminal transcript event settles the turn on the durable plane; the
    // runtime read models (right-panel run rows, runtime summary) must refresh
    // with it — they are only refetched through explicit invalidation. The
    // carrier owns the lowest sequence of its transition, so its terminal
    // handling runs first (total ascending event sequence) and only when the
    // carrier itself was newly applied to the contiguous cursor (never
    // buffered or conflicted).
    if (carrierApplied) {
      runCompatibilityTerminalEffects(dependencies, { agentId, sessionId, key, isActiveRuntime }, transcriptEvent, preConsumptionActiveRunId);
    }
    // Events the carrier drained own their effects exactly once, in total
    // ascending event sequence across both planes.
    if (compatibilityFacts) {
      for (const step of ascendingEffectSteps(compatibilityFacts.canonicalEvents, compatibilityFacts.compatibilityEvents)) {
        if ('canonical' in step) {
          runCanonicalEventSideEffects(dependencies, { agentId, sessionId, key, isActiveRuntime }, preConsumptionActiveRunId, step.canonical);
        } else {
          runCompatibilityTerminalEffects(
            dependencies,
            { agentId, sessionId, key, isActiveRuntime },
            compatibilityProjectionEvent(step.compatibility),
            preConsumptionActiveRunId,
          );
        }
      }
    }
    return;
  }

  // Before canonical SessionEventV2 owned failure delivery, the backend also
  // emitted an identity-free live compatibility card. During a rolling deploy
  // an older instance may still send that adapter shape after the committed
  // run-scoped runtime_failure event. It is not transcript authority and must
  // not create a second user error card beside the canonical terminal.
  if (
    d?.schema === 'hive.thread_item.v1'
    && d?.event_type === 'runtime_failure'
    && d?.sequence === 0
    && typeof d?.id === 'string'
    && d.id.startsWith('live:')
  ) {
    return;
  }

  if (
    typeof d === 'object'
    && d
    && (typeof d.sequence === 'number' || d.transcript_event_id || d.metadata?.transcript_event_id)
    && (d.event_type || d.type)
  ) {
    const transcriptEvent: ChatTranscriptEventPayload = {
      ...d,
      id: d.id || d.transcript_event_id || d.metadata?.transcript_event_id,
      event_type: d.event_type || d.type,
      metadata: d.metadata || d.metadata_json || {},
    };
    // Pre-consumption run identity, same contract as the envelope branches:
    // consumption itself may clear a matching active run.
    const preConsumptionActiveRunId = dependencies.activeRunIdOf(key);
    const applied = applyTranscriptToSession(agentId, sessionId, transcriptEvent, isActiveRuntime);
    // The real application result and the run identity gate every terminal
    // effect: duplicate/rejected frames perform none, and a stale old-run
    // terminal never runs active terminal effects while another run is live.
    const terminal = isTerminalRealtimeChatEvent(transcriptEvent);
    if (terminal && applied && isTerminalRunAcceptedForActiveRun(preConsumptionActiveRunId, terminalWitnessRunId(transcriptEvent))) {
      invalidateSessionRuntimeQueries(agentId, sessionId);
      if (isActiveRuntime) void fetchMySessions(true, agentId);
    }
    return;
  }

  if (d?.type === 'session.error') {
    const error = d.error && typeof d.error === 'object' ? d.error : {};
    const code = typeof error.code === 'string' ? error.code : 'event_store_retryable';
    dependencies.setTransportNotice(
      typeof error.message_key === 'string' ? error.message_key : `session.${code}`,
    );
    if (code === 'auth_failed') context.failAuthentication(key, false);
    return;
  }

  if (d?.type === 'session.control_receipt') {
    return;
  }

  if (d.type === 'phase') {
    if (isRuntimePhase(d.phase)) {
      setSessionPhase(key, d.phase);
      if (TERMINAL_RUNTIME_PHASES.has(d.phase)) {
        markActiveRunTerminal(key, d.run_id ? String(d.run_id) : null);
      }
      if (isActiveRuntime) syncActivePhase(d.phase);
    }
    return;
  }

  if ((d.type === 'run_queued' || d.type === 'run_started') && d.run_id) {
    setActiveRunState(key, sessionRunStateFromPayload(d));
    invalidateSessionRuntimeQueries(agentId, sessionId);
    const phase = d.type === 'run_queued' ? 'queued' : 'starting';
    setSessionPhase(key, phase);
    if (isActiveRuntime) syncActivePhase(phase);
    return;
  }

  if (d.type === 'run_cancelled') {
    const accepted = markActiveRunTerminal(key, d.run_id ? String(d.run_id) : null);
    if (!accepted) return;
    invalidateSessionRuntimeQueries(agentId, sessionId);
    setSessionPhase(key, 'cancelled');
    if (isActiveRuntime) {
      syncActivePhase('cancelled');
    }
    void fetchMySessions(true, agentId);
    return;
  }

  const lifecycleEvent = ['thinking', 'chunk', 'tool_call', 'done', 'error', 'quota_exceeded'].includes(d.type);
  const terminalStreamType = ['done', 'error', 'quota_exceeded'].includes(d.type);
  // Terminal stream frames honor run identity through the same registry: a
  // stale old-run terminal is recorded but rejected, and performs zero
  // active effects — no phase, no refresh/reconcile, no notice, no tail
  // seal, no socket close. Null/absent run identity keeps the historical
  // compatibility behavior (the registry accepts it).
  const terminalStreamAccepted = terminalStreamType
    ? markActiveRunTerminal(key, d.run_id ? String(d.run_id) : null)
    : true;
  const reducedPhase = lifecycleEvent ? reduceRuntimePhase(sessionPhaseOf(key), d) : null;
  if (lifecycleEvent && reducedPhase && (!terminalStreamType || terminalStreamAccepted)) {
    setSessionPhase(key, reducedPhase);
    if (d.type === 'tool_call' && dependencies.shouldInvalidateToolCall(key)) {
      invalidateSessionRuntimeQueries(agentId, sessionId, false);
    }
  }
  if (terminalStreamType && terminalStreamAccepted) {
    invalidateSessionRuntimeQueries(agentId, sessionId);
    // Stream frames and canonical session events ride independent
    // at-least-once channels. A terminal stream frame is the last guaranteed
    // live witness of the turn; reconcile the authoritative transcript so a
    // lost canonical tail (e.g. the final structured tool result) still
    // projects without a reload.
    if (isActiveRuntime) dependencies.reconcileSessionTranscript(agentId, sessionId);
  }

  if (!isActiveRuntime) {
    if (d.type === 'trigger_notification' || (terminalStreamType && terminalStreamAccepted)) {
      void fetchMySessions(true, agentId);
    }
    if (terminalStreamType && terminalStreamAccepted) context.closeSessionSocket(key, true);
    return;
  }

  dependencies.setChatMessagesSessionId(sessionId);
  if (d.type === 'dreaming') return;

  const transportMessage = getTransportNotice(d);
  if (transportMessage) {
    dependencies.setTransportNotice(transportMessage);
    return;
  }
  if (reducedPhase && (!terminalStreamType || terminalStreamAccepted)) syncActivePhase(reducedPhase);

  const runtimeEvent = getRuntimeEventMessage({ ...d, timestamp: new Date().toISOString() });
  if (runtimeEvent) {
    dependencies.enqueueChatMessagesUpdate(sessionId, (messages) => [...messages, parseChatMsg(runtimeEvent)]);
    return;
  }

  if (d.type === 'thinking') {
    dependencies.enqueueChatMessagesUpdate(sessionId, (messages) => {
      const last = messages[messages.length - 1];
      if (last && last.role === 'assistant' && (last as any)._streaming) {
        return [...messages.slice(0, -1), { ...last, thinking: (last.thinking || '') + d.content } as any];
      }
      return [...messages, { role: 'assistant', content: '', thinking: d.content, _streaming: true } as any];
    });
    return;
  }

  if (d.type === 'tool_call') {
    const normalizedResult = normalizeToolCallResult(d.name, d.result);
    const toolMessage = normalizeToolCallMessage({
      role: 'tool_call',
      content: '',
      toolName: d.name,
      toolArgs: d.args,
      toolStatus: d.status,
      toolResult: normalizedResult.displayResult,
      toolRawResult: normalizedResult.raw,
      toolMeta: normalizedResult.toolMeta,
      artifacts: extractArtifactParts(d),
    });
    if (isTerminalTranscriptToolMessage(toolMessage)) {
      markActiveRunTerminal(key, d.run_id ? String(d.run_id) : null);
      setSessionPhase(key, 'awaiting_approval');
      syncActivePhase('awaiting_approval');
    }
    if (normalizedResult.createdAgentId) dependencies.setCreatedAgentId(normalizedResult.createdAgentId);
    dependencies.enqueueChatMessagesUpdate(sessionId, (messages) => appendToolCallMessage(messages, toolMessage));
    return;
  }

  if (d.type === 'chunk') {
    dependencies.enqueueChatMessagesUpdate(sessionId, (messages) => applyStreamingChunkEvent(messages, d));
    return;
  }

  if (d.type === 'done') {
    if (!terminalStreamAccepted) return;
    dependencies.setChatMessagesAfterQueued(
      sessionId,
      (messages) => applyRuntimeDoneEvent(messages, d).map(parseChatMsg),
    );
    void fetchMySessions(true, agentId);
    return;
  }

  if (d.type === 'error' || d.type === 'quota_exceeded') {
    if (!terminalStreamAccepted) return;
    const message = d.content || d.detail || d.message || 'Request denied';
    dependencies.setTransportNotice(String(message));
    return;
  }

  if (d.type === 'trigger_notification') {
    dependencies.enqueueChatMessagesUpdate(sessionId, (messages) => [
      ...messages,
      parseChatMsg({
        role: 'event',
        content: typeof d.content === 'string' ? d.content : '',
        eventType: 'trigger_notification',
        eventStatus: 'trigger_notification',
      }),
    ]);
    void fetchMySessions(true, agentId);
    dependencies.invalidateQuery(['autonomy-overview', agentId]);
    dependencies.invalidateQuery(['triggers', agentId]);
    return;
  }

  if (typeof d.content === 'string' && (d.role === 'assistant' || d.role === 'user')) {
    dependencies.enqueueChatMessagesUpdate(sessionId, (messages) => [
      ...messages,
      parseChatMsg({ role: d.role, content: d.content }),
    ]);
  }
}
