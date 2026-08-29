import React from 'react';
import { renderToStaticMarkup } from 'react-dom/server';
import { beforeEach, describe, expect, it, vi } from 'vitest';

import { withRewindActiveProjection } from './agent-detail/agentDetailPolicy';

const queryCalls: Array<{ key: unknown[]; enabled: unknown }> = [];

async function readSource(relativePath: string): Promise<string> {
  const fsModuleId = 'node:fs';
  const { readFileSync } = (await import(/* @vite-ignore */ fsModuleId)) as {
    readFileSync: (path: URL, encoding: string) => string;
  };
  return readFileSync(new URL(relativePath, import.meta.url), 'utf8');
}

vi.mock('react-i18next', () => ({
  useTranslation: () => ({
    t: (key: string, fallback?: string) => fallback ?? key,
    i18n: { language: 'en' },
  }),
}));

vi.mock('@tanstack/react-query', () => ({
  useQuery: (options: { queryKey: unknown[]; enabled?: unknown }) => {
    queryCalls.push({ key: options.queryKey, enabled: options.enabled });
    const key = String(options.queryKey[0]);
    if (key === 'agent') {
      return {
        data: undefined,
        isLoading: false,
        isError: true,
        error: { status: 403, message: 'Access denied' },
      };
    }
    return {
      data: [],
      isLoading: false,
      isError: false,
      error: null,
      refetch: vi.fn(),
    };
  },
  useQueryClient: () => ({
    invalidateQueries: vi.fn(),
  }),
}));

vi.mock('react-router-dom', () => ({
  useParams: () => ({ id: 'agent-403' }),
  useLocation: () => ({ hash: '', search: '', pathname: '/agents/agent-403' }),
  useNavigate: () => vi.fn(),
}));

vi.mock('../stores', () => {
  const state = {
    token: null,
    user: null,
  };
  const useAuthStore = ((selector: (input: typeof state) => unknown) => selector(state)) as unknown as typeof import('../stores').useAuthStore;
  Object.assign(useAuthStore, {
    getState: () => state,
  });
  return { useAuthStore };
});

vi.mock('../components/ConfirmModal', () => ({ default: () => null }));
vi.mock('../components/FileBrowser', () => ({ default: () => null }));
vi.mock('../components/PromptModal', () => ({ default: () => null }));
vi.mock('./agent-detail/AgentApprovalsSection', () => ({ default: () => null }));
vi.mock('./agent-detail/AgentActivityLogSection', () => ({ default: () => null }));
vi.mock('./agent-detail/AgentAwareSection', () => ({ default: () => null }));
vi.mock('./agent-detail/AgentChatSection', () => ({ default: () => null }));
vi.mock('./agent-detail/AgentMindSection', () => ({ default: () => null }));
vi.mock('./agent-detail/AgentSettingsSection', () => ({ default: () => null }));
vi.mock('./agent-detail/AgentExtensionsSection', () => ({ default: () => null }));
vi.mock('./agent-detail/AgentStatusSection', () => ({ default: () => null }));
vi.mock('./agent-detail/AgentWorkspaceSection', () => ({ default: () => null }));
vi.mock('./agent-detail/AgentA2ASection', () => ({ default: () => null }));

import AgentDetail, {
  defaultSessionPermissionModeFromAgent,
  sessionPermissionModeFromSession,
} from './AgentDetail';

describe('AgentDetail session permission state', () => {
  it('uses the persisted agent preference for new conversations', () => {
    expect(defaultSessionPermissionModeFromAgent({ default_session_permission_mode: 'auto' })).toBe('auto');
    expect(defaultSessionPermissionModeFromAgent(
      { default_session_permission_mode: 'bypassPermissions' },
    )).toBe('bypassPermissions');
    expect(defaultSessionPermissionModeFromAgent({ default_session_permission_mode: 'unknown' })).toBe('default');
  });

  it('restores the composer permission mode from persisted session metadata', () => {
    expect(
      sessionPermissionModeFromSession({
        id: 'session-1',
        permission_mode: 'bypassPermissions',
      }),
    ).toBe('bypassPermissions');

    expect(
      sessionPermissionModeFromSession({
        id: 'session-2',
        permission_profile: { mode: 'default' },
      }),
    ).toBe('default');

    expect(
      sessionPermissionModeFromSession({
        id: 'session-3',
        transcript_metadata_json: { permission_mode: 'auto' },
      }),
    ).toBe('auto');

    expect(
      sessionPermissionModeFromSession({
        id: 'session-4',
      }),
    ).toBe('default');

    expect(
      sessionPermissionModeFromSession({
        id: 'draft:session-6',
        is_draft: true,
        permission_mode: 'bypassPermissions',
      }),
    ).toBe('bypassPermissions');
  });

  it('activates full access as a session mode with a simple risk acknowledgement', async () => {
    const source = await readSource('./AgentDetail.tsx');

    expect(source).toContain("persistSessionPermissionMode('bypassPermissions')");
    expect(source).toContain('Full access skips routine session approval prompts');
    expect(source).toContain('await ensureDurableActiveSession()');
    expect(source).not.toContain('break_glass_');
    expect(source).not.toContain('useSessionPermissionExpiry');
    expect(source).not.toContain('SessionFullAccessPrompt');
  });
});

describe('AgentDetail realtime refresh contract', () => {
  it('seals terminal websocket events onto the visible live process without rebuilding gapped history', async () => {
    const source = await readSource('./agent-detail/sessionSocketEventProjector.ts');
    const applierSource = await readSource('./agent-detail/sessionTranscriptApplier.ts');

    expect(source).toContain('applyTranscriptToSession(agentId, sessionId, transcriptEvent, isActiveRuntime)');
    expect(source).not.toContain('void selectSession(session)');
    expect(source).not.toContain("parseChatMsg({ role: 'assistant', content: `⚠️ ${message}` })");
    // The consumption contract moved to the extracted production applier; its
    // behavioral proof lives in sessionTranscriptApplier.test.ts.
    expect(applierSource).toContain('mergeTranscriptBackfill(');
    expect(applierSource).toContain('events: appliedCanonicalEvents');
    expect(applierSource).not.toContain('transcriptEvents: nextTranscriptEvents');
    expect(applierSource).toContain('onTerminal: (runId, terminalEvent) => {');
    expect(applierSource).toContain('const accepted = deps.markActiveRunTerminal(key, runId);');
    expect(applierSource).toContain('terminalRuntimePhaseForSessionEvent(');
    expect(applierSource).toContain('deps.setIsStreaming(terminalUi.isStreaming);');
    // Canonical arrivals return this transition's application facts.
    expect(applierSource).toContain('return application ?? false');
    // The visible list commits once per transition through the single
    // mixed-plane composition owner — never a plane-specific whole-list
    // replacement.
    expect(applierSource).toContain('composeMixedPlaneSessionMessages({');
    // Terminal seals bind to the accepted canonical terminal run via the
    // merge; legacy-only accepted terminals keep the unbound legacy seal.
    expect(applierSource).toContain('mergeCanonicalTerminalMessages(previous, composed, canonicalTerminalRunId)');
    // A rewind visibility boundary survives live mixed-plane commits.
    expect(applierSource).toContain('applySessionVisibilityBoundary(');
    // Compatibility carriers legacy-project only after contiguous application.
    expect(applierSource).toContain('consumed.sessionEnvelope && !application');
  });

  it('threads the authoritative active run identity into the Session timeline projection', async () => {
    const source = await readSource('./AgentDetail.tsx');
    const sectionSource = await readSource('./agent-detail/AgentChatSection.tsx');

    expect(source).toContain('activeRunId={currentActiveRunState?.runId || null}');
    expect(sectionSource).toContain('activeRunId?: string | null;');
    expect(sectionSource).toMatch(/buildThreadTimelineCached\(\{[\s\S]*activeRunStatus,\s*activeRunId,\s*runtimePhase,/);
  });

  it('derives every live rewind boundary install from the same current list that is trimmed', async () => {
    const source = await readSource('./AgentDetail.tsx');

    // Chat surface: boundary AND trim derive atomically from the message
    // store's flushed current list (queued live updates included)…
    expect(source).toContain('sessionVisibilityBoundariesRef.current[activeKey] = installRewindVisibilityBoundaryFromStore({');
    // …and no captured render-time visible list split remains.
    expect(source).not.toContain('visibleNow');
    // History surface derives boundary and trim from one tracked current list.
    expect(source).toContain('historyMessagesRef.current');
    expect(source).toContain('sessionVisibilityBoundariesRef.current[activeKey] = install.boundary;');
    // The legacy replay baseline is trimmed separately, for its own state.
    expect(source).toContain('messages: trimMessagesBeforeTranscriptEvent(replay.messages, checkpointEventId, checkpointCreatedAt)');
  });

  it('aborts the stale pre-command transcript load and rehydrates from the accepted rewind projection', async () => {
    const source = await readSource('./AgentDetail.tsx');

    // The accepted rewind facts build the updated local session value.
    expect(source).toContain('withRewindActiveProjection(');
    // Local session copies update so a future selection cannot regress to the
    // stale projection while the server refresh arrives.
    expect(source).toContain('setActiveSession(rewoundSession)');
    // The stale pre-command load is aborted and its generation cleared BEFORE
    // the fresh selectSession starts — the existing controller.signal/loadSeq
    // guards make every old publish callback inert; no second generation
    // system.
    const abortAt = source.indexOf('sessionMsgAbortRef.current?.abort();\n                    sessionTranscriptLoadRef.current = null;');
    expect(abortAt).toBeGreaterThan(-1);
    const rehydrateAt = source.indexOf('void selectSession(rewoundSession);');
    expect(rehydrateAt).toBeGreaterThan(-1);
    expect(rehydrateAt).toBeGreaterThan(abortAt);
  });

  it('keeps branch lineage navigation and the canonical Session URL in one transition', async () => {
    const source = await readSource('./AgentDetail.tsx');
    const selectorStart = source.indexOf('const selectBranchSession = async');
    const selectorEnd = source.indexOf('const ensureSessionWorkbenchRoute', selectorStart);
    const selector = source.slice(selectorStart, selectorEnd);

    expect(selector).toContain('ensureSessionWorkbenchRoute(sessionId)');
    expect(selector.indexOf('ensureSessionWorkbenchRoute(sessionId)')).toBeGreaterThan(selector.indexOf('await selectSession('));
  });

  it('removes non-running slash command prompts after their in-session control surface opens', async () => {
    const source = await readSource('./AgentDetail.tsx');
    const sectionSource = await readSource('./agent-detail/AgentChatSection.tsx');

    expect(source).toContain('const commandMessageId = `session-command:');
    expect(source).toContain('message.id !== commandMessageId');
    expect(source).toMatch(/setChatMessagesAfterQueuedForSession\(\s*commandSessionId/);
    expect(sectionSource).toContain("t('sessionWorkbench.commandPanel.resumeContinuePrompt', resumeQuery)");
  });

  it('keeps polling an absent authoritative run until the local grace state is actually cleared', async () => {
    const source = await readSource('./AgentDetail.tsx');

    expect(source).toContain('dataUpdatedAt: activeSessionRunObservedAt');
    expect(source).toContain('activeRunPollInterval(');
    expect(source).toMatch(/\[activeSessionRun,\s*activeSessionRunObservedAt,/);
  });

  it('hydrates the complete canonical Session V2 transcript without a manual older-message gate', async () => {
    const source = await readSource('./AgentDetail.tsx');
    const sectionSource = await readSource('./agent-detail/AgentChatSection.tsx');

    expect(source).toContain('loadCanonicalSessionTranscript');
    expect(source).not.toContain("direction: 'backward'");
    expect(source).not.toContain('loadOlderMessages');
    expect(sectionSource).not.toContain('load-older-messages');
  });

  it('publishes the authoritative hydrated runtime phase to the visible Session surface', async () => {
    const source = await readSource('./AgentDetail.tsx');
    const publishStart = source.indexOf('const publishCanonicalSnapshot =');
    const publishEnd = source.indexOf('const canonicalHydration =', publishStart);
    const publisher = source.slice(publishStart, publishEnd);

    expect(publisher).toContain('sessionUiStateRef.current[runtimeKey] = projected.ui;');
    expect(publisher).toContain('syncActivePhase(projected.ui.phase);');
  });

  it('opens live transport from the first safe newest suffix while older history keeps recovering', async () => {
    const source = await readSource('./AgentDetail.tsx');

    expect(source).toContain('canonicalHydrationInFlight = canonicalHydration.liveReady');
    expect(source).toContain('return liveSubscriptionWatermark(projected.store)');
    expect(source).not.toContain('canonicalHydrationInFlight = canonicalHydration.then');
  });

  it('does not present an empty conversation after live transport connects but before durable history appears', async () => {
    const source = await readSource('./AgentDetail.tsx');
    const liveTailStart = source.indexOf('onLiveTailReady:');
    const liveTailEnd = source.indexOf('onDisconnected:', liveTailStart);
    const liveTailHandler = source.slice(liveTailStart, liveTailEnd);

    expect(liveTailHandler).not.toContain('setChatMessagesSessionId');
  });

  it('removes the older-history notice once canonical hydration is complete', async () => {
    const source = await readSource('./AgentDetail.tsx');

    expect(source).toContain('nextSessionBackfillNotice(');
    expect(source).toContain('Boolean(transcriptBackfillInFlightRef.current[key])');
    expect(source).toContain('sessionEventFullHydrationKeysRef.current.has(key)');
    expect(source).toContain('sessionEventFullHydrationKeysRef.current.delete(runtimeKey)');
  });

  it('does not make REST transcript hydration a prerequisite for the live Session transport', async () => {
    const pageSource = await readSource('./AgentDetail.tsx');
    const controllerSource = await readSource('./agent-detail/useSessionTransportController.ts');

    expect(pageSource).not.toContain('transportHydratedKeys');
    expect(controllerSource).not.toContain('await optionsRef.current.callbacks.onBackfill(session, agentId)');
    expect(controllerSource).toContain('onLiveTailReady');
    expect(controllerSource).toContain('getLiveSubscriptionCursor');
  });

  it('never gives up transient reconnects and recovers missed durable transcript events', async () => {
    const source = await readSource('./agent-detail/useSessionTransportController.ts');
    const pageSource = await readSource('./AgentDetail.tsx');

    expect(source).not.toContain('attempts >= 20');
    expect(source).not.toContain('Giving up reconnect');
    expect(source).not.toContain('event.code !== 1000');
    expect(source).toContain('const reconnect = shouldReconnectSessionSocket(');
    expect(source).toContain('if (reconnect) scheduleReconnect()');
    expect(source).toContain('reconnectDelayMs(previousAttempts)');
    expect(source).toContain("window.addEventListener('online', wake)");
    expect(source).toContain("window.addEventListener('offline', handleOffline)");
    expect(source).toContain("document.addEventListener('visibilitychange', handleVisibility)");
    expect(source).toContain('transportPollIntervalMs(');
    expect(source).toContain('backfill: () => optionsRef.current.callbacks.onBackfill(activeSession, agentId)');
    expect(pageSource).toContain('onReconnectTransport={reconnectActiveTransport}');
  });
});

describe('AgentDetail access failures', () => {
  beforeEach(() => {
    queryCalls.length = 0;
  });

  it('stops status-side queries and shows a forbidden message when the main agent query returns 403', () => {
    const markup = renderToStaticMarkup(<AgentDetail />);

    expect(markup).toContain('You do not have access to this employee.');

    const getEnabled = (queryKey: unknown[]) =>
      queryCalls.find((entry) => JSON.stringify(entry.key) === JSON.stringify(queryKey))?.enabled;

    expect(getEnabled(['activity', 'agent-403', 'owner'])).toBe(false);
    expect(getEnabled(['agent-capability-installs', 'agent-403'])).toBe(false);
    expect(getEnabled(['agent-channel-capabilities', 'agent-403'])).toBe(false);
    expect(getEnabled(['metrics', 'agent-403'])).toBe(false);
  });
});

describe('rewind session projection builder (Codex REQUEST_CHANGES #4 stale-load race)', () => {
  it('installs the authoritative server projection from control_event.metadata (production _apply_projection_rewind shape)', () => {
    const session = {
      id: 'session-1',
      agent_id: 'agent-1',
      title: 'Ops',
      metadata: { custom: 'kept' },
      transcript_metadata_json: {
        legacy_flag: true,
        active_projection: { projection_reason: 'stale' },
      },
    };
    // The REAL accepted-result shape: checkpoint carries checkpoint facts
    // only, truth_source/rewind_guard also ride top-level, and the full
    // durable projection lives in control_event.metadata next to the
    // envelope-only command/workspace_restore fields. applied_at and mode
    // exist ONLY in that metadata projection.
    const actionResult = {
      truth_source: 'transcript',
      rewind_guard: { mode: 'strict' },
      checkpoint: {
        id: 'event-9',
        created_at: '2026-08-28T01:00:00Z',
        turn_index: 7,
        content: 'draft text',
      },
      control_event: {
        metadata: {
          command: { name: 'rewind' },
          workspace_restore: { restored: 3 },
          projection_reason: 'rewind',
          checkpoint_event_id: 'event-9',
          ledger_event_id: null,
          draft_content: 'draft text',
          turn_index: 7,
          applied_at: '2026-08-28T01:00:01Z',
          truth_source: 'transcript',
          mode: 'context',
          rewind_guard: { mode: 'strict' },
        },
      },
    };

    const updated = withRewindActiveProjection(session, actionResult, 'event-9', 'draft text');
    const metadata = updated.transcript_metadata_json as Record<string, unknown>;

    expect(updated).toMatchObject({
      id: 'session-1',
      agent_id: 'agent-1',
      title: 'Ops',
      metadata: { custom: 'kept' },
    });
    expect(metadata.legacy_flag).toBe(true);
    // Every exact server projection field survives, including the supplied
    // null ledger_event_id; envelope-only fields never enter the projection.
    expect(metadata.active_projection).toEqual({
      projection_reason: 'rewind',
      checkpoint_event_id: 'event-9',
      ledger_event_id: null,
      draft_content: 'draft text',
      turn_index: 7,
      applied_at: '2026-08-28T01:00:01Z',
      truth_source: 'transcript',
      mode: 'context',
      rewind_guard: { mode: 'strict' },
    });
    // The input session object is never mutated.
    expect((session.transcript_metadata_json.active_projection as { projection_reason: string }).projection_reason).toBe('stale');
  });

  it('falls back to checkpoint/top-level facts for older responses without control_event.metadata', () => {
    const updated = withRewindActiveProjection(
      { id: 'session-1' },
      {
        truth_source: 'transcript',
        checkpoint: { id: 'event-3', turn_index: 2, applied_at: '2026-08-28T01:00:01Z' },
      },
      'event-3',
      '',
    );
    expect(updated.id).toBe('session-1');
    expect(updated.transcript_metadata_json).toEqual({
      active_projection: {
        projection_reason: 'rewind',
        checkpoint_event_id: 'event-3',
        draft_content: '',
        applied_at: '2026-08-28T01:00:01Z',
        turn_index: 2,
        truth_source: 'transcript',
      },
    });
  });
});
