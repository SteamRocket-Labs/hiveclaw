import React, { useState, useEffect, useRef, lazy, Suspense } from 'react';
import { useParams, useLocation, useNavigate } from 'react-router-dom';
import { useQuery, useQueryClient } from '@tanstack/react-query';
import { useTranslation } from 'react-i18next';
import ConfirmModal from '../components/ConfirmModal';
import AgentChatSection, {
    type SessionCommandControlState,
    type SessionPermissionMode,
} from './agent-detail/AgentChatSection';
import type { BranchLineageItem } from './agent-detail/SessionLineageSurface';
import AgentStatusSection from './agent-detail/AgentStatusSection';
import {
    ACTIVE_RUN_ABSENCE_GRACE_MS,
    activeRunPollInterval,
    applySessionActiveRunObservedState,
    applySessionActiveRunState,
    buildRuntimeSummary,
    buildSessionTranscriptLoadFailureMessage,
    createEmptyTranscriptReplayState,
    isDraftHumanChatSession,
    markActiveRunTerminalInRegistry, normalizeSessionRunId,
    mergePendingUserMessages,
    filterSessionsForAgent,
    hasLocalSessionRuntimeState,
    normalizeRuntimeEventMessage,
    normalizeStoredChatMessage,
    phaseUi,
    replayTranscriptEvents,
    resolvePendingSessionLookup,
    uiForPhase,
    sessionBelongsToAgent,
    shouldPreserveActiveSessionForRequestedId,
    shouldUseWritableSessionSurface,
    shouldIgnoreObservedActiveRun,
    shouldClearStaleRuntimeState,
    reconcileSessionTranscriptSafely,
    shouldReconcileTranscriptOnActiveRunAbsence,
    shouldReuseSessionTranscriptLoad,
    type AgentChatMessage,
    type ChatTranscriptEventPayload,
    type ChatRuntimeSummary,
    type PendingUserMessage,
    type RuntimePhase,
    type SessionTranscriptLoadDescriptor,
    type SessionPermissionRequest,
    type SessionRunState,
    type SessionUiState,
    type TranscriptReplayState,
} from './agent-detail/chatRuntime';
import { buildPlanModeScopeKey, nextPlanModeRequestedForScope } from './agent-detail/planModeComposer';
import { latestTranscriptSequence, retrySessionRead } from './agent-detail/chatTransportRecovery';
import {
    createSessionMessageStore,
    useSessionMessages,
    type ChatMessagesUpdater,
} from './agent-detail/sessionMessageStore';
import { normalizeToolCallResult } from './agent-detail/toolResultEnvelope';
import { sessionRunStateFromPayload } from './agent-detail/runtimeBudgetState';
import { agentApi, type AgentCapabilityInstall, type AgentChannelCapability } from '../api/domains/agents';
import { activityApi } from '../api/domains/activity';
import { enterpriseApi } from '../api/domains/enterprise';
import { triggerApi } from '../api/domains/triggers';
import { autonomyApi } from '../api/domains/autonomy';
import { chatApi, type ChatSession, type ConversationBranchMode, type SessionRun, type StartSessionRunInput } from '../api/domains/chat';
import { ccParityApi } from '../api/domains/ccParity';
import { ApiError } from '../api/core';
import { uploadFileWithProgress } from '../api/core/upload-progress';
import { useAuthStore } from '../stores';
import { parseSlashCommandInput } from './agent-detail/slashCommand';
import {
    commandResultRecord,
    formatSlashCommandResult,
    getSessionCommandUiAction,
} from './agent-detail/sessionCommandResult';
import {
    useSessionTransportController,
    type SessionTransportCallbacks,
} from './agent-detail/useSessionTransportController';
import { projectSessionSocketEvent } from './agent-detail/sessionSocketEventProjector';
import { applyTranscriptToSessionRuntime } from './agent-detail/sessionTranscriptApplier';
import { buildSessionCommandStatusControl } from './agent-detail/sessionCommandPanelPresentation';
import { AgentDetailErrorBoundary, SessionAccessErrorSurface, SessionResolvingSurface } from './agent-detail/AgentDetailErrorSurfaces';
import { liveSubscriptionWatermark, loadCanonicalSessionTranscript, nextSessionBackfillNotice, projectCanonicalTranscriptSnapshot, realtimeSubscriptionCursor } from './agent-detail/sessionTranscriptHydration';
import type { CompatibilityMessageTimeline, SessionVisibilityBoundary } from './agent-detail/sessionEventConsumer';
import { buildSessionVisibilityBoundary, createCompatibilityMessageTimeline, installRewindVisibilityBoundary, installRewindVisibilityBoundaryFromStore, seedCompatibilityTimelineIdentities } from './agent-detail/sessionEventConsumer';
import { createSessionEventStore, type SessionEventStore } from './session-workbench/sessionEventStore';
import {
    buildAssignmentHandoff,
    buildAssignmentSessionTitle,
    readAssignmentHandoff,
} from './assignmentHandoff';
import {
    createDraftChatSession,
    readNewSessionDraftRequest,
} from './agent-detail/newSessionNavigation';
import {
    AGENT_DETAIL_TABS,
    AGENT_TAB_LABELS,
    AGENT_WORKBENCH_AREAS,
    DEFAULT_SESSION_PERMISSION_MODE,
    applySessionActiveProjection,
    branchDraftContent,
    buildAgentDetailTabNavigation,
    buildSessionWorkbenchNavigation,
    checkpointEventIdFromPayload,
    defaultSessionPermissionModeFromAgent,
    draftContentFromCheckpointPayload,
    getAgentDetailHashTab,
    getVisibleAgentDetailTabs,
    isAgentDetailTabVisible,
    isLocalAgentRuntimeType,
    isSessionWorkbenchRoute,
    normalizeSessionCommandCheckpoints,
    objectValue,
    rewindMarkerMessage,
    sessionPermissionModeFromSession,
    stringValueFromUnknown,
    trimMessagesBeforeTranscriptEvent,
    withRewindActiveProjection,
    withSessionPermissionMode,
    type AgentDetailTab,
} from './agent-detail/agentDetailPolicy';
import './AgentDetail.css';

const AgentApprovalsSection = lazy(() => import('./agent-detail/AgentApprovalsSection'));
const AgentWorkflowsSection = lazy(() => import('./agent-detail/AgentWorkflowsSection'));
const AgentActivityLogSection = lazy(() => import('./agent-detail/AgentActivityLogSection'));
const AgentAwareSection = lazy(() => import('./agent-detail/AgentAwareSection'));
const AgentEvolutionSection = lazy(() => import('./agent-detail/AgentEvolutionSection'));
const AgentExtensionsSection = lazy(() => import('./agent-detail/AgentExtensionsSection'));
const AgentKnowledgeSection = lazy(() => import('./agent-detail/AgentKnowledgeSection'));
const AgentBusinessTasksSection = lazy(() => import('./agent-detail/AgentBusinessTasksSection'));
const AgentSettingsSection = lazy(() => import('./agent-detail/AgentSettingsSection'));
const AgentWorkspaceSection = lazy(() => import('./agent-detail/AgentWorkspaceSection'));
const AgentA2ASection = lazy(() => import('./agent-detail/AgentA2ASection'));
const LocalAgents = lazy(() => import('./LocalAgents'));
const LocalAgentChatSection = lazy(() => import('./agent-detail/LocalAgentChatSection'));

export {
    AGENT_DETAIL_TABS,
    applySessionActiveProjection,
    buildAgentDetailTabNavigation,
    buildSessionWorkbenchNavigation,
    defaultSessionPermissionModeFromAgent,
    getAgentDetailHashTab,
    getVisibleAgentDetailTabs,
    isLocalAgentRuntimeType,
    isSessionWorkbenchRoute,
    sessionPermissionModeFromSession,
    trimMessagesBeforeTranscriptEvent,
} from './agent-detail/agentDetailPolicy';

function AgentDetailSectionFallback() {
    return (
        <div className="agent-detail-section-fallback" role="status" aria-live="polite">
            <span className="agent-detail-section-fallback-dot" aria-hidden="true" />
            Loading workspace…
        </div>
    );
}

function AgentDetailInner() {
    const { t, i18n } = useTranslation();
    const { id, sessionId: routeSessionId } = useParams<{ id: string; sessionId?: string }>();
    const navigate = useNavigate();
    const queryClient = useQueryClient();
    const location = useLocation();
    const validTabs = AGENT_DETAIL_TABS as readonly string[];
    const [activeTab, setActiveTabRaw] = useState<string>(() =>
        routeSessionId ? 'chat' : (getAgentDetailHashTab(location.hash, validTabs) ?? 'status'),
    );
    const querySessionId = new URLSearchParams(location.search).get('session_id');
    const requestedSessionId = routeSessionId ?? querySessionId;
    const isManageMode = new URLSearchParams(location.search).has('manage');
    const consumedNewSessionDraftRequestRef = useRef<string | null>(null);
    const newSessionDraftRequest = readNewSessionDraftRequest(location.state, id);
    const newSessionDraftTransitionPending = Boolean(
        newSessionDraftRequest
        && consumedNewSessionDraftRequestRef.current !== newSessionDraftRequest.requestId,
    );

    // Legacy disguise → canonical session route (§8.4 Legacy Session URL row).
    useEffect(() => {
        if (routeSessionId || !id || !querySessionId || isManageMode) return;
        if (!location.hash.startsWith('#chat') && location.hash !== '') return;
        navigate(buildSessionWorkbenchNavigation(`/agents/${id}`, location.search, querySessionId), { replace: true });
        // eslint-disable-next-line react-hooks/exhaustive-deps
    }, [routeSessionId, id, querySessionId, isManageMode]);

    // Sync URL hash when tab changes
    const setActiveTab = (tab: string, options?: { detailChat?: boolean }) => {
        setActiveTabRaw(tab);
        navigate(buildAgentDetailTabNavigation(location.pathname, location.search, tab, options), { replace: true });
    };
    const selectDetailTab = (tab: string) => setActiveTab(tab, { detailChat: tab === 'chat' });
    useEffect(() => {
        const nextHashTab = getAgentDetailHashTab(location.hash, validTabs);
        if (nextHashTab) setActiveTabRaw((current) => (current === nextHashTab ? current : nextHashTab));
    }, [location.hash]);
    const token = useAuthStore((s) => s.token);
    const currentUser = useAuthStore((s) => s.user);
    const isAdmin = currentUser?.role === 'platform_admin' || currentUser?.role === 'org_admin';

    const { data: agent, isLoading, error: agentError } = useQuery({
        queryKey: ['agent', id],
        queryFn: () => agentApi.getById(id!),
        enabled: !!id,
        retry: false,
        refetchOnWindowFocus: true, // page stays mounted across tabs; global focus-refetch is off, this query opts in
    });
    const canLoadAgentScopedData = !!id && !!agent;
    const canManage = !!agent && ((agent as any).access_level === 'manage' || isAdmin);

    // ── Aware tab data: triggers ──
    const { data: awareTriggers = [], refetch: refetchTriggers } = useQuery({
        queryKey: ['triggers', id],
        queryFn: () => triggerApi.list(id!),
        enabled: canLoadAgentScopedData && activeTab === 'aware',
        refetchInterval: activeTab === 'aware' ? 5000 : false,
    });

    const { data: autonomyOverview, refetch: refetchAutonomy } = useQuery({
        queryKey: ['autonomy-overview', id],
        queryFn: () => autonomyApi.getOverview(id!, { lookbackHours: 24 }),
        enabled: canLoadAgentScopedData && activeTab === 'aware',
        refetchInterval: activeTab === 'aware' ? 5000 : false,
    });

    // ── Aware tab data: reflection sessions (trigger monologues) ──
    const { data: reflectionSessions = [] } = useQuery({
        queryKey: ['reflection-sessions', id],
        queryFn: async () => {
            const all = await chatApi.listSessions(id!, 'all', {
                operatorView: true,
                operatorReason: 'Agent reflection monitoring',
            }).catch(() => [] as any[]);
            return all.filter((s: any) => s.source_channel === 'trigger');
        },
        enabled: canLoadAgentScopedData && canManage && activeTab === 'aware',
        refetchInterval: activeTab === 'aware' ? 10000 : false,
    });

    // ── Aware tab state ──
    const [expandedReflection, setExpandedReflection] = useState<string | null>(null);
    const [reflectionMessages, setReflectionMessages] = useState<Record<string, any[]>>({});
    const [showAllTriggers, setShowAllTriggers] = useState(false);
    const [reflectionPage, setReflectionPage] = useState(0);

    const [activityOperatorView, setActivityOperatorView] = useState(false);
    useEffect(() => setActivityOperatorView(false), [id]);

    const { data: activityLogs = [] } = useQuery({
        queryKey: ['activity', id, activityOperatorView ? 'operator' : 'owner'],
        queryFn: () => activityApi.list(
            id!,
            100,
            activityOperatorView ? { operatorView: true, reason: 'Agent activity administration' } : undefined,
        ),
        enabled: canLoadAgentScopedData && (activeTab === 'activityLog' || activeTab === 'status'),
        refetchInterval: activeTab === 'activityLog' ? 10000 : false,
    });

    const { data: capabilityInstalls = [] } = useQuery<AgentCapabilityInstall[]>({
        queryKey: ['agent-capability-installs', id],
        queryFn: () => agentApi.getCapabilityInstalls(id!),
        enabled: canLoadAgentScopedData && activeTab === 'status',
        staleTime: 30_000,
    });

    const { data: channelCapabilities = [] } = useQuery<AgentChannelCapability[]>({
        queryKey: ['agent-channel-capabilities', id],
        queryFn: () => agentApi.getChannelCapabilities(id!),
        enabled: canLoadAgentScopedData && activeTab === 'status',
        staleTime: 30_000,
    });

    const { data: toolFailureSummary } = useQuery({
        queryKey: ['activity', 'tool-failures', id, activityOperatorView ? 'operator' : 'owner'],
        queryFn: () => activityApi.getToolFailureSummary(
            id!,
            24,
            200,
            activityOperatorView ? { operatorView: true, reason: 'Agent activity administration' } : undefined,
        ),
        enabled: canLoadAgentScopedData && activeTab === 'activityLog',
        refetchInterval: activeTab === 'activityLog' ? 10000 : false,
    });

    // Chat history
    // ── Session state (replaces old conversations query) ──────────────────
    const [sessions, setSessions] = useState<any[]>([]);
    const [allSessions, setAllSessions] = useState<any[]>([]);
    const [activeSession, setActiveSession] = useState<any | null>(null);
    const [branchLineage, setBranchLineage] = useState<BranchLineageItem[]>([]);
    const [branchLineageLoading, setBranchLineageLoading] = useState(false);
    const [chatScope, setChatScope] = useState<'mine' | 'all'>('mine');
    const [allUserFilter, setAllUserFilter] = useState<string>('');  // filter by username in All Users
    const [historyMsgs, setHistoryMsgs] = useState<AgentChatMessage[]>([]);
    const historyMessagesRef = useRef<AgentChatMessage[]>([]);
    const updateHistoryMsgs = (next: AgentChatMessage[] | ((previous: AgentChatMessage[]) => AgentChatMessage[])) => {
        const resolved = typeof next === 'function' ? next(historyMessagesRef.current) : next;
        historyMessagesRef.current = resolved;
        setHistoryMsgs(resolved);
    };
    const [historyMessagesSessionId, setHistoryMessagesSessionId] = useState<string | null>(null);
    const [sessionsLoading, setSessionsLoading] = useState(false);
    const [allSessionsLoading, setAllSessionsLoading] = useState(false);
    const [agentExpired, setAgentExpired] = useState(false);
    const [sessionAccessError, setSessionAccessError] = useState<{ sessionId: string; status: 403 | 404 } | null>(null);
    // Websocket chat state (for 'me' conversation)
    type SessionRuntimeKey = string;
    const sessionUiStateRef = useRef<Record<SessionRuntimeKey, SessionUiState>>({});
    const transcriptReplayStateRef = useRef<Record<SessionRuntimeKey, TranscriptReplayState>>({});
    const sessionCompatibilityTimelinesRef = useRef<Record<SessionRuntimeKey, CompatibilityMessageTimeline>>({});
    const sessionVisibilityBoundariesRef = useRef<Record<SessionRuntimeKey, SessionVisibilityBoundary>>({});
    const transcriptEventsRef = useRef<Record<SessionRuntimeKey, ChatTranscriptEventPayload[]>>({});
    const sessionEventStoresRef = useRef<Record<SessionRuntimeKey, SessionEventStore>>({});
    const sessionEventFullHydrationKeysRef = useRef<Set<SessionRuntimeKey>>(new Set());
    const transcriptBackfillInFlightRef = useRef<Record<SessionRuntimeKey, Promise<number | void> | undefined>>({});
    const toolCallInvalidateAtRef = useRef<Record<SessionRuntimeKey, number>>({});
    const activeRunStateRef = useRef<Record<SessionRuntimeKey, SessionRunState>>({});
    const pendingUserMessagesRef = useRef<Record<SessionRuntimeKey, PendingUserMessage[]>>({});
    const runtimeActivityAtRef = useRef<Record<SessionRuntimeKey, number>>({});
    const activeSessionIdRef = useRef<string | null>(null);
    const currentAgentIdRef = useRef<string | undefined>(id);
    const sessionMsgAbortRef = useRef<AbortController | null>(null);
    const sessionLoadSeqRef = useRef(0);
    const sessionTranscriptLoadRef = useRef<SessionTranscriptLoadDescriptor | null>(null);
    const locallyTerminalRunIdsRef = useRef<Set<string>>(new Set());
    const locallyTerminalSessionKeysRef = useRef<Set<string>>(new Set());
    const [activeRunStateBySession, setActiveRunStateBySession] = useState<Record<SessionRuntimeKey, SessionRunState>>({});
    const transportCallbacksRef = useRef<SessionTransportCallbacks>({
        onBackfill: () => undefined,
        onLiveTailReady: () => undefined,
        onDisconnected: () => undefined,
        onMessage: () => undefined,
    });
    const {
        wsConnected,
        transportPhase,
        transportReconnectAttempt,
        closeSessionSocket,
        getSessionSocket,
        reconnectActiveTransport,
        resetActiveTransportState,
        syncActiveSocketState,
    } = useSessionTransportController({
        enabled: canLoadAgentScopedData && activeTab === 'chat' && Boolean(id && activeSession?.id)
            && activeSession?.is_pending_session_lookup !== true && !sessionAccessError,
        agentId: id,
        token,
        activeSession,
        writableSession: Boolean(
            activeSession
            && shouldUseWritableSessionSurface(activeSession, currentUser?.id)
            && !isDraftHumanChatSession(activeSession),
        ),
        isRunActive: (key) => Boolean(activeRunStateRef.current[key]),
        shouldKeepalive: (key) => {
            const uiState = sessionUiStateRef.current[key];
            return Boolean(activeRunStateRef.current[key] || uiState?.isWaiting || uiState?.isStreaming);
        },
        getHighestContiguousSequence: (key) => (
            sessionEventStoresRef.current[key]?.highestContiguousSequence
            ?? latestTranscriptSequence(transcriptEventsRef.current[key] || [])
        ),
        getLiveSubscriptionCursor: (key) => realtimeSubscriptionCursor(sessionEventStoresRef.current[key], latestTranscriptSequence(transcriptEventsRef.current[key] || []), sessionEventFullHydrationKeysRef.current.has(key)),
        getProjectionPhase: (key) => sessionEventStoresRef.current[key]?.projection.phase,
        needsProjectionRecovery: (key) => {
            const store = sessionEventStoresRef.current[key];
            return store?.recoveryRequired === 'full_hydration' || store?.projection.phase === 'gap_detected';
        },
        onAgentExpired: () => setAgentExpired(true),
        onSocketDisposed: (key) => {
            delete sessionUiStateRef.current[key];
            delete pendingUserMessagesRef.current[key];
            delete runtimeActivityAtRef.current[key];
        },
        callbacks: {
            onBackfill: (session, agentId) => transportCallbacksRef.current.onBackfill(session, agentId),
            onLiveTailReady: (input) => transportCallbacksRef.current.onLiveTailReady(input),
            onDisconnected: (input) => transportCallbacksRef.current.onDisconnected(input),
            onMessage: (context) => transportCallbacksRef.current.onMessage(context),
        },
    });
    const buildSessionRuntimeKey = (agentId: string, sessionId: string) => `${agentId}:${sessionId}`;
    const isLiveRun = (run?: SessionRun | null) => !!run && ['pending', 'running'].includes(String(run.status || '').toLowerCase());

    const invalidateSessionRuntimeQueries = (agentId: string, sessionId: string, includeActiveRun = true) => {
        if (includeActiveRun) void queryClient.invalidateQueries({ queryKey: ['chat-active-run', agentId, sessionId] });
        void queryClient.invalidateQueries({ queryKey: ['chat-runtime-summary', agentId, sessionId] });
        void queryClient.invalidateQueries({ queryKey: ['chat-session-work-ledger', agentId, sessionId] });
        void queryClient.invalidateQueries({ queryKey: ['chat-session-workbench', agentId, sessionId] });
    };

    const setActiveRunState = (key: SessionRuntimeKey, run: SessionRunState | null) => {
        if (run) {
            runtimeActivityAtRef.current[key] = Date.now();
            locallyTerminalSessionKeysRef.current.delete(key);
            if (run.runId) locallyTerminalRunIdsRef.current.delete(normalizeSessionRunId(run.runId));
        }
        const next = applySessionActiveRunState(activeRunStateRef.current, sessionUiStateRef.current, key, run);
        activeRunStateRef.current = next.activeRuns;
        sessionUiStateRef.current = next.uiStates;
        setActiveRunStateBySession(next.activeRuns);
    };

    const observeActiveRunState = (key: SessionRuntimeKey, run: SessionRunState) => {
        runtimeActivityAtRef.current[key] = Date.now();
        const next = applySessionActiveRunObservedState(activeRunStateRef.current, sessionUiStateRef.current, key, run);
        activeRunStateRef.current = next.activeRuns;
        sessionUiStateRef.current = next.uiStates;
        setActiveRunStateBySession(next.activeRuns);
        return next.uiStates[key];
    };

    // Identity-safe terminal bookkeeping lives in chatRuntime (policy owner).
    const markActiveRunTerminal = (key: SessionRuntimeKey, terminalRunId?: string | null): boolean =>
        markActiveRunTerminalInRegistry(locallyTerminalSessionKeysRef.current, locallyTerminalRunIdsRef.current, key, activeRunStateRef.current[key]?.runId ?? null, terminalRunId ?? null, () => setActiveRunState(key, null));

    // Turn lifecycle state machine (§3 seam 1): the RuntimePhase is the single
    // source of truth per session; waiting/streaming booleans are derived views.
    const setSessionPhase = (key: SessionRuntimeKey, phase: RuntimePhase) => {
        sessionUiStateRef.current[key] = uiForPhase(phase);
        const derived = phaseUi(phase);
        if (derived.isWaiting || derived.isStreaming) runtimeActivityAtRef.current[key] = Date.now();
    };

    const sessionPhaseOf = (key: SessionRuntimeKey): RuntimePhase =>
        sessionUiStateRef.current[key]?.phase ?? 'idle';

    const mergePendingForSession = (key: SessionRuntimeKey, messages: AgentChatMessage[]): AgentChatMessage[] => {
        const merged = mergePendingUserMessages(messages, pendingUserMessagesRef.current[key] || []);
        if (merged.pending.length > 0) {
            pendingUserMessagesRef.current[key] = merged.pending;
        } else {
            delete pendingUserMessagesRef.current[key];
        }
        return merged.messages;
    };

    const appendOptimisticUserMessage = (
        key: SessionRuntimeKey, sessionId: string, messageInput: AgentChatMessage,
    ) => {
        const message = parseChatMsg(messageInput);
        setChatMessagesAfterQueuedForSession(sessionId, prev => {
            pendingUserMessagesRef.current[key] = [
                ...(pendingUserMessagesRef.current[key] || []),
                { message, anchorMessageCount: prev.length },
            ];
            return [...prev, message];
        });
    };

    const isTerminalTranscriptToolMessage = (message: AgentChatMessage | undefined) => {
        if (!message || message.role !== 'tool_call') return false;
        const kind = message.toolMeta?.kind;
        const toolMeta = message.toolMeta as (typeof message.toolMeta & { blocking?: boolean }) | null | undefined;
        return Boolean(
            toolMeta?.blocking
            || kind === 'user_clarification'
            || kind === 'plan_proposal'
            || kind === 'dynamic_workflow_proposal'
            || kind === 'plan_mode_request'
            || kind === 'create_employee_success'
            || kind === 'hr_preview',
        );
    };
    const applyTranscriptToSession = (
        agentId: string,
        sessionId: string,
        event: ChatTranscriptEventPayload,
        isActiveRuntime: boolean,
    ) => applyTranscriptToSessionRuntime({
        refs: {
            transcriptEvents: transcriptEventsRef.current,
            eventStores: sessionEventStoresRef.current,
            fullHydrationKeys: sessionEventFullHydrationKeysRef.current,
            replayStates: transcriptReplayStateRef.current,
            compatibilityTimelines: sessionCompatibilityTimelinesRef.current,
            visibilityBoundaries: sessionVisibilityBoundariesRef.current,
            uiStates: sessionUiStateRef.current,
            runtimeActivityAt: runtimeActivityAtRef.current,
            pendingUserMessages: pendingUserMessagesRef.current,
        },
        markActiveRunTerminal,
        isTerminalTranscriptToolMessage,
        mergePendingMessages: mergePendingForSession,
        setChatMessagesSessionId,
        enqueueChatMessagesUpdate: enqueueChatMessagesUpdateForSession,
        setChatMessagesAfterQueued: setChatMessagesAfterQueuedForSession,
        setActivePhase,
        setIsWaiting,
        setIsStreaming,
        parseChatMsg,
    }, agentId, sessionId, event, isActiveRuntime);

    const backfillSessionTranscript = async (sess: any, agentId: string) => {
        const sessionId = String(sess?.id || '');
        if (!sessionId) return;
        const key = buildSessionRuntimeKey(agentId, sessionId);
        if (!transcriptReplayStateRef.current[key]) return;
        const inFlight = transcriptBackfillInFlightRef.current[key];
        if (inFlight) return liveSubscriptionWatermark(sessionEventStoresRef.current[key]) ?? inFlight;

        const task = (async () => {
            if (sessionEventStoresRef.current[key]?.recoveryRequired === 'full_hydration') {
                delete sessionEventStoresRef.current[key];
                sessionEventFullHydrationKeysRef.current.add(key);
            }
            let afterSequence = sessionEventFullHydrationKeysRef.current.has(key)
                ? 0
                : sessionEventStoresRef.current[key]?.highestContiguousSequence
                    ?? latestTranscriptSequence(transcriptEventsRef.current[key] || []);
            let pageSize = 0;
            do {
                const events = await chatApi.getSessionTranscript(agentId, sessionId, {
                    afterSequence,
                    limit: 1000,
                    schemaVersion: 2,
                    ...(sess?.operator_view
                        ? { operatorView: true, operatorReason: 'Agent session administration' }
                        : undefined),
                }) as ChatTranscriptEventPayload[];
                pageSize = events.length;
                if (pageSize === 0) break;
                const isActiveRuntime = currentAgentIdRef.current === agentId
                    && activeSessionIdRef.current === sessionId;
                events.forEach((event) => applyTranscriptToSession(agentId, sessionId, event, isActiveRuntime));
                const nextSequence = latestTranscriptSequence(events);
                if (nextSequence <= afterSequence) break;
                afterSequence = nextSequence;
            } while (pageSize === 1000);
            invalidateSessionRuntimeQueries(agentId, sessionId);
            return sessionEventStoresRef.current[key]?.highestContiguousSequence ?? afterSequence;
        })().catch((error) => {
            console.warn(`[WS] Durable transcript backfill failed for ${key}:`, error);
            throw error;
        }).finally(() => {
            sessionEventFullHydrationKeysRef.current.delete(key);
            delete transcriptBackfillInFlightRef.current[key];
        });
        transcriptBackfillInFlightRef.current[key] = task;
        return task;
    };

    const isWritableSession = (sess: any) => {
        if (!sess) return false;
        return shouldUseWritableSessionSurface(sess, currentUser?.id);
    };

    const fetchMySessions = async (silent = false, agentId: string | undefined = id) => {
        if (!agentId) return [];
        if (!silent && currentAgentIdRef.current === agentId) setSessionsLoading(true);
        try {
            const data = filterSessionsForAgent(await chatApi.listSessions(agentId, 'mine'), agentId);
            if (currentAgentIdRef.current === agentId) {
                setSessions((prev: any[]) => {
                    const drafts = prev.filter((session: any) => isDraftHumanChatSession(session));
                    return [...drafts, ...data];
                });
            }
            if (!silent && currentAgentIdRef.current === agentId) setSessionsLoading(false);
            return data;
        } catch { }
        if (!silent && currentAgentIdRef.current === agentId) setSessionsLoading(false);
        return [];
    };

    const fetchAllSessions = async () => {
        if (!id || !canManage) {
            if (currentAgentIdRef.current === id) setAllSessions([]);
            return;
        }
        setAllSessionsLoading(true);
        try {
            const all = filterSessionsForAgent(await chatApi.listSessions(id, 'all', {
                operatorView: true,
                operatorReason: 'Agent session administration',
            }), id);
            if (currentAgentIdRef.current === id) {
                setAllSessions(all.filter((s: any) => s.source_channel !== 'trigger'));
            }
        } catch { }
        setAllSessionsLoading(false);
    };

    const selectSession = async (sess: any) => {
        const targetAgentId = id;
        if (!targetAgentId) return;
        if (!sessionBelongsToAgent(sess, targetAgentId)) return;
        const sessionId = String(sess.id);
        const runtimeKey = buildSessionRuntimeKey(targetAgentId, sessionId);
        const runtimeState = sessionUiStateRef.current[runtimeKey] || uiForPhase('idle');
        const activeRunState = activeRunStateRef.current[runtimeKey];
        const writableSession = isWritableSession(sess);
        const draftSession = isDraftHumanChatSession(sess);
        const operatorOptions = sess?.operator_view
            ? { operatorView: true, operatorReason: 'Agent session administration' }
            : undefined;
        const transcriptSurface = writableSession ? 'chat' : 'history';
        const nextTranscriptLoad: SessionTranscriptLoadDescriptor = {
            key: runtimeKey,
            surface: transcriptSurface,
        };
        setSessionAccessError(null);
        activeSessionIdRef.current = sessionId;
        setCreatedAgentId(null);
        if (writableSession) {
            setHistoryMessagesSessionId(null);
            setChatMessagesSessionId((current) => (current === sessionId ? current : null));
        } else {
            setChatMessagesSessionId(null);
            setHistoryMessagesSessionId((current) => (current === sessionId ? current : null));
        }
        setTransportNotice(null);
        setSessionCommandControl(null);
        syncActivePhase(
            runtimeState.phase !== 'idle'
                ? runtimeState.phase
                : (activeRunState ? 'resuming' : 'idle'),
        );
        setSessionPermissionMode(sessionPermissionModeFromSession(sess));
        setActiveSession(sess);
        setAgentExpired(false);
        syncActiveSocketState(sess, targetAgentId, writableSession && !draftSession);

        if (draftSession) {
            sessionMsgAbortRef.current?.abort();
            setChatMessagesAfterQueued(() => []);
            setChatMessagesSessionId(sessionId);
            updateHistoryMsgs([]);
            setHistoryMessagesSessionId(null);
            setBranchLineage([]);
            setIsWaiting(false);
            setIsStreaming(false);
            return;
        }

        // Abort any pending message load and increment sequence
        if (shouldReuseSessionTranscriptLoad(sessionTranscriptLoadRef.current, nextTranscriptLoad)) {
            return;
        }
        sessionMsgAbortRef.current?.abort();
        const controller = new AbortController();
        sessionMsgAbortRef.current = controller;
        const loadSeq = ++sessionLoadSeqRef.current;
        sessionTranscriptLoadRef.current = {
            ...nextTranscriptLoad,
            loadSeq,
        };
        let canonicalHydrationInFlight: Promise<number | void> | undefined;
        let publishedCanonicalSnapshot = false;
        try {
            const publishCanonicalSnapshot = (snapshot: ChatTranscriptEventPayload[]) => {
                if (snapshot.length === 0) return;
                if (controller.signal.aborted || loadSeq !== sessionLoadSeqRef.current) return;
                if (currentAgentIdRef.current !== targetAgentId) return;
                if (activeSessionIdRef.current !== sessionId) return;
                const projected = projectCanonicalTranscriptSnapshot({
                    existing: transcriptEventsRef.current[runtimeKey] || [],
                    snapshot,
                    session: sess,
                    parseMessage: parseChatMsg,
                });
                transcriptEventsRef.current[runtimeKey] = projected.events;
                if (projected.store) sessionEventStoresRef.current[runtimeKey] = projected.store;
                transcriptReplayStateRef.current[runtimeKey] = projected.replay;
                sessionCompatibilityTimelinesRef.current[runtimeKey] = projected.compatibilityTimeline;
                sessionVisibilityBoundariesRef.current[runtimeKey] = projected.visibilityBoundary;
                sessionUiStateRef.current[runtimeKey] = projected.ui;
                syncActivePhase(projected.ui.phase);
                const { messages: preParsed, activeProjection } = projected;
                if (activeProjection.checkpointEventId) {
                    if (activeProjection.draftContent) setChatInput(activeProjection.draftContent);
                    if (activeProjection.shouldScrollToProjectionTail && !publishedCanonicalSnapshot) {
                        setProjectionTailScrollNonce(prev => prev + 1);
                    }
                }
                publishedCanonicalSnapshot = true;
                if (writableSession) {
                    setChatMessagesSessionId(sessionId);
                    setChatMessagesAfterQueuedForSession(sessionId, () => mergePendingForSession(runtimeKey, preParsed));
                } else {
                    setHistoryMessagesSessionId(sessionId);
                    updateHistoryMsgs(preParsed);
                }
                return liveSubscriptionWatermark(projected.store);
            };
            const canonicalHydration = loadCanonicalSessionTranscript(
                (page) => retrySessionRead(() => chatApi.getSessionTranscript(targetAgentId, sessionId, {
                    ...page,
                    signal: controller.signal,
                    ...operatorOptions,
                }) as Promise<ChatTranscriptEventPayload[]>, { signal: controller.signal }),
                publishCanonicalSnapshot,
                { session: sess },
            );
            canonicalHydrationInFlight = canonicalHydration.liveReady;
            transcriptBackfillInFlightRef.current[runtimeKey] = canonicalHydrationInFlight;
            const transcriptEvents = await canonicalHydration;
            if (controller.signal.aborted || loadSeq !== sessionLoadSeqRef.current) return;
            if (currentAgentIdRef.current !== targetAgentId) return;
            if (activeSessionIdRef.current !== sessionId) return;
            if (transcriptEvents.length === 0) {
                const msgs = await retrySessionRead(() => chatApi.getSessionMessages(targetAgentId, sessionId, {
                    signal: controller.signal,
                    ...operatorOptions,
                }), { signal: controller.signal });
                if (controller.signal.aborted || loadSeq !== sessionLoadSeqRef.current) return;
                const storedParsed = msgs.map((m: any) => parseChatMsg(normalizeStoredChatMessage(m)));
                let preParsed = storedParsed;
                transcriptReplayStateRef.current[runtimeKey] = {
                    ...createEmptyTranscriptReplayState(),
                    messages: preParsed,
                };
                const activeProjection = applySessionActiveProjection(sess, preParsed);
                preParsed = activeProjection.messages;
                if (activeProjection.checkpointEventId) {
                    const replay = transcriptReplayStateRef.current[runtimeKey];
                    if (replay) {
                        transcriptReplayStateRef.current[runtimeKey] = {
                            ...replay,
                            messages: activeProjection.messages,
                        };
                    }
                    if (activeProjection.draftContent) setChatInput(activeProjection.draftContent);
                    if (activeProjection.shouldScrollToProjectionTail) {
                        setProjectionTailScrollNonce(prev => prev + 1);
                    }
                }
                // Stored fallback messages own no event sequences: seed deterministic pre-live timeline identities and carry the same rewind boundary as the canonical path.
                const fallbackTimeline = createCompatibilityMessageTimeline();
                seedCompatibilityTimelineIdentities(
                    fallbackTimeline,
                    transcriptReplayStateRef.current[runtimeKey]?.messages || [],
                );
                sessionCompatibilityTimelinesRef.current[runtimeKey] = fallbackTimeline;
                sessionVisibilityBoundariesRef.current[runtimeKey] = activeProjection.checkpointEventId
                    ? buildSessionVisibilityBoundary(activeProjection.checkpointEventId, storedParsed, activeProjection.messages)
                    : null;
                if (writableSession) {
                    setChatMessagesSessionId(sessionId);
                    setChatMessagesAfterQueuedForSession(sessionId, () => mergePendingForSession(runtimeKey, preParsed));
                } else {
                    setHistoryMessagesSessionId(sessionId);
                    updateHistoryMsgs(preParsed);
                }
            }
            sessionEventFullHydrationKeysRef.current.delete(runtimeKey);
            setActiveSession((current: any | null) => resolvePendingSessionLookup(current, sessionId));
            setTransportNotice((current) => nextSessionBackfillNotice(current, t('agent.chat.sessionBackfillIncomplete', 'Latest activity is visible, but older session evidence is still recovering.'), false));
        } catch (err: any) {
            if (err?.name === 'AbortError') return;
            if (loadSeq !== sessionLoadSeqRef.current) return;
            if (currentAgentIdRef.current !== targetAgentId) return;
            if (activeSessionIdRef.current !== sessionId) return;
            if (err instanceof ApiError && (err.status === 403 || err.status === 404)) {
                setSessionAccessError({ sessionId, status: err.status });
                setTransportNotice(null);
                setChatMessagesAfterQueuedForSession(sessionId, () => []);
                setChatMessagesSessionId(null);
                setHistoryMessagesSessionId(null);
                updateHistoryMsgs([]);
                setBranchLineage([]);
                transcriptEventsRef.current[runtimeKey] = [];
                delete transcriptReplayStateRef.current[runtimeKey];
                delete sessionCompatibilityTimelinesRef.current[runtimeKey];
                delete sessionVisibilityBoundariesRef.current[runtimeKey];
                delete sessionEventStoresRef.current[runtimeKey];
                delete pendingUserMessagesRef.current[runtimeKey];
                setActiveRunState(runtimeKey, null);
                setIsWaiting(false);
                setIsStreaming(false);
                syncActivePhase('idle');
                return;
            }
            if (publishedCanonicalSnapshot) {
                console.error('Failed to load session messages:', err);
                sessionEventFullHydrationKeysRef.current.add(runtimeKey);
                setTransportNotice(
                    t(
                        'agent.chat.sessionBackfillIncomplete',
                        'Latest activity is visible, but older session evidence is still recovering.',
                    ),
                );
                return;
            }
            console.error('Failed to load session messages:', err);
            const failureMessage = buildSessionTranscriptLoadFailureMessage(
                t(
                    'agent.chat.sessionLoadFailed',
                    'Conversation failed to load. Please retry this session or refresh the page.',
                ),
            );
            if (writableSession) {
                setChatMessagesSessionId(sessionId);
                setChatMessagesAfterQueuedForSession(sessionId, () => mergePendingForSession(runtimeKey, [failureMessage]));
            } else {
                setHistoryMessagesSessionId(sessionId);
                updateHistoryMsgs([failureMessage]);
            }
            setActiveSession((current: any | null) => resolvePendingSessionLookup(current, sessionId));
        } finally {
            if (transcriptBackfillInFlightRef.current[runtimeKey] === canonicalHydrationInFlight) {
                delete transcriptBackfillInFlightRef.current[runtimeKey];
            }
            const currentLoad = sessionTranscriptLoadRef.current;
            if (
                currentLoad?.key === runtimeKey
                && currentLoad.surface === transcriptSurface
                && currentLoad.loadSeq === loadSeq
            ) {
                sessionTranscriptLoadRef.current = null;
            }
        }
    };

    const fetchBranchLineage = async (agentId: string | undefined = id, sessionId: string | undefined = activeSession?.id ? String(activeSession.id) : undefined) => {
        if (!agentId || !sessionId) {
            setBranchLineage([]);
            return;
        }
        setBranchLineageLoading(true);
        try {
            const lineage = await chatApi.getSessionLineage(
                agentId,
                sessionId,
                activeSession?.operator_view
                    ? { operatorView: true, operatorReason: 'Agent session administration' }
                    : undefined,
            );
            setBranchLineage((lineage || []).map((item: any) => ({
                id: String(item.id),
                parent_session_id: item.parent_session_id ?? null,
                root_session_id: item.root_session_id ?? null,
                title: item.title ?? null,
                branch: item.branch ?? null,
                created_at: item.created_at ?? null,
            })));
        } catch (err) {
            console.warn('Failed to load branch lineage:', err);
            setBranchLineage([]);
        } finally {
            setBranchLineageLoading(false);
        }
    };

    const selectBranchSession = async (sessionId: string) => {
        const known = [...sessions, ...allSessions].find((session: any) => String(session.id) === String(sessionId));
        if (known) {
            await selectSession(known);
            ensureSessionWorkbenchRoute(sessionId);
            return;
        }
        const branch = branchLineage.find((item) => String(item.id) === String(sessionId));
        await selectSession({
            id: sessionId,
            agent_id: id,
            user_id: currentUser?.id ? String(currentUser.id) : undefined,
            title: branch?.title || 'Branch',
            created_at: branch?.created_at,
            parent_session_id: branch?.parent_session_id ?? null,
            root_session_id: branch?.root_session_id ?? (branch?.branch?.root_session_id ? String(branch.branch.root_session_id) : null),
            transcript_metadata_json: branch?.branch ?? null,
            source_channel: 'web',
            listed_surface: 'chat',
        });
        ensureSessionWorkbenchRoute(sessionId);
    };

    const ensureSessionWorkbenchRoute = (sessionId: string) => {
        setActiveTabRaw('chat');
        navigate(buildSessionWorkbenchNavigation(location.pathname, location.search, sessionId), { replace: true });
    };

    const openSessionCommandControl = (control: SessionCommandControlState) => {
        if (activeSession?.id) {
            ensureSessionWorkbenchRoute(String(activeSession.id));
        } else {
            setActiveTab('chat');
        }
        setSessionCommandControl(control);
    };

    const handleSessionCommandUiAction = async (response: Awaited<ReturnType<typeof ccParityApi.executeCommand>>) => {
        const uiAction = getSessionCommandUiAction(response);
        if (!uiAction || !id || !activeSession?.id) return false;
        const actionResult = commandResultRecord(response);
        const currentSessionId = String(activeSession.id);
        const message = typeof uiAction.message === 'string' && uiAction.message.trim()
            ? uiAction.message.trim()
            : formatSlashCommandResult(response);

        if (uiAction.type === 'switch_session') {
            const targetSession = actionResult?.session && typeof actionResult.session === 'object'
                ? actionResult.session as any
                : null;
            const draftContent = branchDraftContent({ branch: objectValue(actionResult?.branch) }, '');
            const targetSessionId = String(uiAction.session_id || targetSession?.id || '');
            if (targetSessionId) {
                if (targetSession) {
                    setSessions(prev => {
                        if (prev.some((session: any) => String(session.id) === targetSessionId)) return prev;
                        return [targetSession, ...prev];
                    });
                    setAllSessions(prev => {
                        if (prev.some((session: any) => String(session.id) === targetSessionId)) return prev;
                        return [targetSession, ...prev];
                    });
                    await selectSession(targetSession);
                } else {
                    await selectBranchSession(targetSessionId);
                }
                await fetchMySessions(true, id);
                await fetchBranchLineage(id, targetSessionId);
            }
            showToast(message, 'success');
            invalidateSessionRuntimeQueries(id, currentSessionId);
            if (targetSessionId && targetSessionId !== currentSessionId) {
                invalidateSessionRuntimeQueries(id, targetSessionId);
            }
            if (targetSessionId) {
                ensureSessionWorkbenchRoute(targetSessionId);
            }
            if (draftContent) setChatInput(draftContent);
            return true;
        }

        if (uiAction.type === 'open_checkpoint_selector') {
            const checkpoints = normalizeSessionCommandCheckpoints(uiAction.checkpoints || actionResult?.checkpoints);
            openSessionCommandControl({
                type: 'checkpoint_selector',
                title: t('sessionWorkbench.commandPanel.selectCheckpointTitle', 'Choose where to go back'),
                message: checkpoints.length > 0
                    ? t('sessionWorkbench.commandPanel.selectCheckpointMessage', 'Choose an earlier request. The full history stays preserved.')
                    : t('sessionWorkbench.commandPanel.noCheckpoints', 'This session has no earlier request to return to.'),
                command: response.command,
                checkpoints,
                payload: actionResult,
            });
            invalidateSessionRuntimeQueries(id, currentSessionId);
            showToast(
                t('sessionWorkbench.commandPanel.selectCheckpointTitle', 'Choose where to go back'),
                uiAction.level === 'error' ? 'error' : 'success',
            );
            return true;
        }

        if (uiAction.type === 'open_resume_picker') {
            const control = buildSessionCommandStatusControl(t, uiAction.type, {
                command: response.command,
                level: uiAction.level === 'error' ? 'error' : 'info',
                payload: actionResult,
                interrupted: actionResult?.interrupted === true,
                resumeState: typeof actionResult?.resume_state === 'string' ? actionResult.resume_state : undefined,
            });
            openSessionCommandControl(control);
            invalidateSessionRuntimeQueries(id, currentSessionId);
            showToast(control.title, uiAction.level === 'error' ? 'error' : 'success');
            return true;
        }

        if (uiAction.type === 'copy_to_clipboard') {
            const content = typeof uiAction.content === 'string' ? uiAction.content : '';
            if (content && navigator.clipboard?.writeText) {
                await navigator.clipboard.writeText(content);
            }
            showToast(message, 'success');
            return true;
        }

        if (uiAction.type === 'open_export_panel') {
            openSessionCommandControl({
                type: 'export_panel',
                title: message || 'Session export',
                message: 'Session export is ready.',
                command: response.command,
                payload: actionResult,
            });
            showToast(message, 'success');
            invalidateSessionRuntimeQueries(id, currentSessionId);
            return true;
        }

        if (uiAction.type === 'open_permissions_menu') {
            openSessionCommandControl({
                type: 'permissions_panel',
                title: message || 'Session permissions',
                message: 'Use the permission selector in the composer to change this session mode.',
                command: response.command,
                payload: actionResult,
            });
            showToast(message, 'success');
            return true;
        }

        if (uiAction.type === 'confirm_workspace_restore') {
            openSessionCommandControl(buildSessionCommandStatusControl(t, uiAction.type, {
                command: response.command,
                payload: actionResult,
            }));
            invalidateSessionRuntimeQueries(id, currentSessionId);
            return true;
        }

        if (
            uiAction.type === 'install_compacted_context'
            || uiAction.type === 'install_active_projection'
            || uiAction.type === 'install_workspace_snapshot'
            || uiAction.type === 'install_active_projection_with_workspace'
        ) {
            const workspaceRestore = uiAction.type === 'install_workspace_snapshot' || uiAction.type === 'install_active_projection_with_workspace';
            const isRewindProjection = uiAction.type === 'install_active_projection' || uiAction.type === 'install_active_projection_with_workspace';
            if (isRewindProjection) {
                const checkpointEventId = checkpointEventIdFromPayload(actionResult, uiAction);
                const draftContent = draftContentFromCheckpointPayload(actionResult, uiAction);
                const checkpointCreatedAt = stringValueFromUnknown(objectValue(actionResult?.checkpoint).created_at);
                if (draftContent) setChatInput(draftContent);
                if (checkpointEventId) {
                    const activeKey = buildSessionRuntimeKey(id, currentSessionId);
                    // Boundary and trim ALWAYS derive from ONE current list: the chat surface via the message store's synchronously flushed updater list (queued live updates included), the history surface via the tracked mirror ref.
                    if (chatMessagesSessionId === currentSessionId) {
                        sessionVisibilityBoundariesRef.current[activeKey] = installRewindVisibilityBoundaryFromStore({
                            store: sessionMessageStore,
                            sessionId: currentSessionId,
                            previous: sessionVisibilityBoundariesRef.current[activeKey],
                            checkpointEventId,
                            marker: rewindMarkerMessage(checkpointEventId),
                            ...(checkpointCreatedAt ? { checkpointCreatedAt } : {}),
                        });
                    } else {
                        const install = installRewindVisibilityBoundary({
                            previous: sessionVisibilityBoundariesRef.current[activeKey],
                            checkpointEventId,
                            visibleMessages: historyMessagesSessionId === currentSessionId ? historyMessagesRef.current : [],
                            ...(checkpointCreatedAt ? { checkpointCreatedAt } : {}),
                        });
                        sessionVisibilityBoundariesRef.current[activeKey] = install.boundary;
                        if (historyMessagesSessionId === currentSessionId) updateHistoryMsgs(install.trimmedVisibleMessages);
                    }
                    // The legacy replay baseline is trimmed separately, for its own state only.
                    const replay = transcriptReplayStateRef.current[activeKey];
                    if (replay) {
                        transcriptReplayStateRef.current[activeKey] = {
                            ...replay,
                            messages: trimMessagesBeforeTranscriptEvent(replay.messages, checkpointEventId, checkpointCreatedAt),
                        };
                    }
                    setProjectionTailScrollNonce(prev => prev + 1);
                    // The pre-command transcript load captured the stale session value: abort it and clear its generation (existing controller.signal/loadSeq guards make every old publish callback inert), update local session copies, then rehydrate from the accepted rewind projection. The immediate boundary/UI above stays visible meanwhile; selectSession catches its own load errors.
                    const rewoundSession = withRewindActiveProjection(activeSession, actionResult, checkpointEventId, draftContent);
                    setActiveSession(rewoundSession);
                    setSessions(prev => prev.map((entry: any) => (String(entry.id) === currentSessionId ? rewoundSession : entry)));
                    setAllSessions(prev => prev.map((entry: any) => (String(entry.id) === currentSessionId ? rewoundSession : entry)));
                    sessionMsgAbortRef.current?.abort();
                    sessionTranscriptLoadRef.current = null;
                    void selectSession(rewoundSession);
                }
            }
            const control = buildSessionCommandStatusControl(t, uiAction.type, {
                command: response.command,
                level: uiAction.level === 'error' ? 'error' : 'success',
                payload: actionResult,
            });
            openSessionCommandControl(control);
            invalidateSessionRuntimeQueries(id, currentSessionId);
            showToast(control.title, uiAction.level === 'error' ? 'error' : 'success');
            return true;
        }

        if (uiAction.type === 'open_context_panel' || uiAction.type === 'open_usage_panel' || uiAction.type === 'open_side_question') {
            openSessionCommandControl({
                type: uiAction.type === 'open_context_panel'
                    ? 'context_panel'
                    : uiAction.type === 'open_usage_panel'
                        ? 'usage_panel'
                        : 'side_question',
                title: message || (uiAction.type === 'open_context_panel' ? 'Session context' : uiAction.type === 'open_usage_panel' ? 'Session usage' : 'Side question'),
                message,
                command: response.command,
                payload: actionResult,
            });
            invalidateSessionRuntimeQueries(id, currentSessionId);
            showToast(message, uiAction.level === 'error' ? 'error' : 'success');
            return true;
        }

        if (uiAction.type === 'toast') {
            invalidateSessionRuntimeQueries(id, currentSessionId);
            showToast(message, uiAction.level === 'error' ? 'error' : 'success');
            return true;
        }

        showToast(message, 'success');
        return true;
    };

    const handleRunSessionCommandFromUi = async (command: string, args: Record<string, unknown> = {}) => {
        if (!id || !activeSession?.id) return;
        const currentSessionId = String(activeSession.id);
        try {
            const response = await ccParityApi.executeCommand(id, command, {
                arguments: args,
                session_id: currentSessionId,
            });
            const handled = await handleSessionCommandUiAction(response);
            if (!handled) {
                showToast(formatSlashCommandResult(response), 'success');
                invalidateSessionRuntimeQueries(id, currentSessionId);
            }
        } catch (err: any) {
            showToast(err?.message || t('agent.chat.commands.failed', 'Failed to run command'), 'error');
        }
    };

    const createNewSession = async () => {
        if (!id) return;
        const draft = createDraftChatSession({
            agentId: id,
            userId: currentUser?.id ? String(currentUser.id) : undefined,
            permissionMode: defaultSessionPermissionModeFromAgent(agent),
        });
        const params = new URLSearchParams(location.search);
        params.delete('session_id');
        params.delete('manage');
        navigate(
            {
                pathname: location.pathname,
                search: params.toString() ? `?${params.toString()}` : '',
                hash: '#chat',
            },
            { replace: true, state: null },
        );
        setSessions(prev => [draft, ...prev.filter((session: any) => !isDraftHumanChatSession(session))]);
        setIsStreaming(false);
        setIsWaiting(false);
        await selectSession(draft);
    };

    const replaceDraftActiveSession = (draftId: string, created: ChatSession) => {
        setSessions(prev => [created, ...prev.filter((session: any) => String(session.id) !== draftId)]);
        activeSessionIdRef.current = String(created.id);
        setActiveSession(created);
        setChatMessagesSessionId(String(created.id));
        setHistoryMessagesSessionId(null);
        setSessionPermissionMode(sessionPermissionModeFromSession(created));
        navigate(buildSessionWorkbenchNavigation(location.pathname, location.search, String(created.id)), { replace: true });
        return created;
    };

    const ensureDurableActiveSession = async () => {
        if (!id || !activeSession?.id) return null;
        if (!isDraftHumanChatSession(activeSession)) return activeSession;
        const draftId = String(activeSession.id);
        const created = await chatApi.createSession(id, activeSession.title || undefined);
        return replaceDraftActiveSession(draftId, created);
    };

    const startRunForActiveSession = async (input: StartSessionRunInput): Promise<{ session: ChatSession; run: SessionRun } | null> => {
        if (!id || !activeSession?.id) return null;
        if (!isDraftHumanChatSession(activeSession)) {
            const run = await chatApi.startSessionRun(id, String(activeSession.id), input);
            return { session: activeSession as ChatSession, run };
        }
        const draftId = String(activeSession.id);
        const response = await chatApi.createSessionRun(id, {
            title: activeSession.title || undefined,
            ...input,
        });
        const created = replaceDraftActiveSession(draftId, response.session);
        return { session: created, run: response.run };
    };

    const deleteSession = async (sessionId: string) => {
        const target = [...sessions, ...allSessions].find((session: any) => String(session.id) === String(sessionId));
        setDeleteSessionConfirm({ sessionId, title: target?.title || t('agent.chat.session', 'Session') });
    };

    const confirmDeleteSession = async () => {
        if (!deleteSessionConfirm) return;
        const sessionId = deleteSessionConfirm.sessionId;
        setDeleteSessionConfirm(null);
        try {
            await chatApi.deleteSession(id!, sessionId);
            if (id) closeSessionSocket(buildSessionRuntimeKey(id, sessionId), true);
            // If deleted the active session, clear it
            if (activeSession?.id === sessionId) {
                activeSessionIdRef.current = null;
                setActiveSession(null);
                setChatMessagesAfterQueued(() => []);
                setChatMessagesSessionId(null);
                updateHistoryMsgs([]);
                setHistoryMessagesSessionId(null);
                setTransportNotice(null);
                resetActiveTransportState();
                setIsStreaming(false);
                setIsWaiting(false);
            }
            await fetchMySessions(false, id);
            await fetchAllSessions();
        } catch (e: any) {
            showToast(e.message || 'Delete failed', 'error');
        }
    };

    // Expiry editor modal state
    const [showExpiryModal, setShowExpiryModal] = useState(false);
    const [expiryValue, setExpiryValue] = useState('');       // datetime-local string or ''
    const [expirySaving, setExpirySaving] = useState(false);

    const openExpiryModal = () => {
        const cur = (agent as any)?.expires_at;
        // Convert ISO to datetime-local format (YYYY-MM-DDTHH:MM)
        setExpiryValue(cur ? new Date(cur).toISOString().slice(0, 16) : '');
        setShowExpiryModal(true);
    };

    const addHours = (h: number) => {
        const base = (agent as any)?.expires_at ? new Date((agent as any).expires_at) : new Date();
        const next = new Date(base.getTime() + h * 3600_000);
        setExpiryValue(next.toISOString().slice(0, 16));
    };

    const saveExpiry = async (permanent = false) => {
        setExpirySaving(true);
        try {
            const body = permanent ? { expires_at: null } : { expires_at: expiryValue ? new Date(expiryValue).toISOString() : null };
            await agentApi.update(id!, body as any);
            queryClient.invalidateQueries({ queryKey: ['agent', id] });
            setShowExpiryModal(false);
        } catch (e) { showToast(`Failed: ${e}`, 'error'); }
        setExpirySaving(false);
    };
    const [chatMessagesSessionId, setChatMessagesSessionId] = useState<string | null>(null);
    const sessionMessageStoreRef = useRef<ReturnType<typeof createSessionMessageStore> | null>(null);
    if (!sessionMessageStoreRef.current) {
        sessionMessageStoreRef.current = createSessionMessageStore();
    }
    const sessionMessageStore = sessionMessageStoreRef.current;
    const chatMessages = useSessionMessages(sessionMessageStore, chatMessagesSessionId);
    const enqueueChatMessagesUpdateForSession = React.useCallback((sessionId: string | null | undefined, update: ChatMessagesUpdater) => sessionMessageStore.enqueueUpdate(sessionId, update), [sessionMessageStore]);
    const setChatMessagesAfterQueuedForSession = React.useCallback((sessionId: string | null | undefined, update: ChatMessagesUpdater) => sessionMessageStore.updateAfterQueued(sessionId, update), [sessionMessageStore]);
    const resolveCurrentChatMessagesSessionId = React.useCallback(() => (
        activeSessionIdRef.current
        || chatMessagesSessionId
        || (activeSession?.id ? String(activeSession.id) : null)
    ), [activeSession?.id, chatMessagesSessionId]);
    const enqueueChatMessagesUpdate = React.useCallback((update: ChatMessagesUpdater) => {
        enqueueChatMessagesUpdateForSession(resolveCurrentChatMessagesSessionId(), update);
    }, [enqueueChatMessagesUpdateForSession, resolveCurrentChatMessagesSessionId]);
    const setChatMessagesAfterQueued = React.useCallback((update: ChatMessagesUpdater) => {
        setChatMessagesAfterQueuedForSession(resolveCurrentChatMessagesSessionId(), update);
    }, [resolveCurrentChatMessagesSessionId, setChatMessagesAfterQueuedForSession]);
    useEffect(() => () => {
        sessionMessageStore.clearAll();
    }, [sessionMessageStore]);
    const [chatInput, setChatInput] = useState('');
    const [planModeRequested, setPlanModeRequested] = useState(false);
    const [goalModeRequested, setGoalModeRequested] = useState(false);
    const goalStartRequestRef = useRef<{ fingerprint: string; requestId: string } | null>(null);
    const [sessionPermissionMode, setSessionPermissionMode] = useState<SessionPermissionMode>(DEFAULT_SESSION_PERMISSION_MODE);
    const [fullAccessPromptOpen, setFullAccessPromptOpen] = useState(false);
    const [sessionCommandControl, setSessionCommandControl] = useState<SessionCommandControlState | null>(null);
    const [projectionTailScrollNonce, setProjectionTailScrollNonce] = useState(0);

    const applySessionPermissionUpdate = (
        sessionId: string,
        mode: SessionPermissionMode,
    ) => {
        setSessionPermissionMode(mode);
        setActiveSession((current: any | null) =>
            current && String(current.id) === sessionId
                ? withSessionPermissionMode(current, mode)
                : current,
        );
        setSessions((current) =>
            current.map((session) =>
                String(session.id) === sessionId
                    ? withSessionPermissionMode(session, mode)
                    : session,
            ),
        );
        setAllSessions((current) =>
            current.map((session) =>
                String(session.id) === sessionId
                    ? withSessionPermissionMode(session, mode)
                    : session,
            ),
        );
    };

    const handleSetSessionPermissionMode = async (mode: SessionPermissionMode) => {
        if (mode === 'bypassPermissions') {
            setFullAccessPromptOpen(true);
            return;
        }
        await persistSessionPermissionMode(mode);
    };

    const persistSessionPermissionMode = async (mode: SessionPermissionMode) => {
        const previous = sessionPermissionMode;
        if (!id || !activeSession?.id) return;
        if (isDraftHumanChatSession(activeSession) && mode !== 'bypassPermissions') {
            applySessionPermissionUpdate(String(activeSession.id), mode);
            return;
        }
        let targetSession = activeSession;
        try {
            if (isDraftHumanChatSession(targetSession)) {
                targetSession = await ensureDurableActiveSession();
            }
            if (!targetSession?.id) return;
            const sessionId = String(targetSession.id);
            applySessionPermissionUpdate(sessionId, mode);
            const result: any = await chatApi.updateSessionPermissionProfile(id, sessionId, { permission_mode: mode });
            applySessionPermissionUpdate(sessionId, sessionPermissionModeFromSession(result));
            invalidateSessionRuntimeQueries(id, sessionId);
        } catch (err: any) {
            applySessionPermissionUpdate(String(targetSession?.id || activeSession.id), previous);
            const msg = err?.message || t('agent.chat.permission.updateFailed', 'Failed to update session permission mode');
            showToast(msg, 'error');
        }
    };

    const handleConfirmFullAccess = () => {
        setFullAccessPromptOpen(false);
        void persistSessionPermissionMode('bypassPermissions');
    };

    const handleCancelFullAccess = () => {
        setFullAccessPromptOpen(false);
    };
    const planModeScopeKey = buildPlanModeScopeKey(id, activeSession?.id);
    const planModeScopeKeyRef = useRef(planModeScopeKey);
    useEffect(() => {
        const previousScopeKey = planModeScopeKeyRef.current;
        planModeScopeKeyRef.current = planModeScopeKey;
        setPlanModeRequested((currentRequested) =>
            nextPlanModeRequestedForScope({
                currentRequested,
                previousScopeKey,
                nextScopeKey: planModeScopeKey,
            }),
        );
        setGoalModeRequested(false);
    }, [planModeScopeKey]);
    const consumedAssignmentHandoffRef = useRef<string | null>(null);
    useEffect(() => {
        const handoff = readAssignmentHandoff(location.state);
        if (!handoff || !requestedSessionId || String(activeSession?.id || '') !== String(requestedSessionId)) return;
        const handoffKey = `${requestedSessionId}:${handoff.intent}:${handoff.content}`;
        if (consumedAssignmentHandoffRef.current === handoffKey) return;
        consumedAssignmentHandoffRef.current = handoffKey;
        setChatInput(handoff.content);
        setPlanModeRequested(handoff.intent === 'plan');
        setGoalModeRequested(handoff.intent === 'goal');
        navigate(
            { pathname: location.pathname, search: location.search, hash: location.hash },
            { replace: true, state: null },
        );
    }, [activeSession?.id, location.hash, location.pathname, location.search, location.state, navigate, requestedSessionId]);
    useEffect(() => {
        setSessionPermissionMode(sessionPermissionModeFromSession(activeSession));
    }, [
        activeSession?.id,
        activeSession?.permission_mode,
        activeSession?.permission_profile?.mode,
        activeSession?.transcript_metadata_json?.permission_mode,
    ]);
    const [uploading, setUploading] = useState(false);
    const [isWaiting, setIsWaiting] = useState(false);
    const [isStreaming, setIsStreaming] = useState(false);
    // RuntimePhase for the ACTIVE session (§3 seam 1). isWaiting/isStreaming are
    // derived views kept in sync through syncActivePhase — never set directly
    // on lifecycle paths anymore.
    const [activePhase, setActivePhase] = useState<RuntimePhase>('idle');
    const syncActivePhase = (phase: RuntimePhase) => {
        setActivePhase(phase);
        const derived = phaseUi(phase);
        setIsWaiting(derived.isWaiting);
        setIsStreaming(derived.isStreaming);
    };
    const [transportNotice, setTransportNotice] = useState<string | null>(null);

    const [uploadProgress, setUploadProgress] = useState(-1);
    const uploadAbortRef = useRef<(() => void) | null>(null);
    const [attachedFiles, setAttachedFiles] = useState<{
        name: string;
        text: string;
        path?: string;
        imageUrl?: string;
        conversion?: {
            status?: string;
            markdownPath?: string;
            metadataPath?: string;
            engine?: string;
        };
    }[]>([]);
    const [createdAgentId, setCreatedAgentId] = useState<string | null>(null);
    const chatEndRef = useRef<HTMLDivElement>(null);
    const chatContainerRef = useRef<HTMLDivElement>(null);
    const chatInputRef = useRef<HTMLTextAreaElement>(null);
    const fileInputRef = useRef<HTMLInputElement>(null);

    // Settings form local state
    const [settingsForm, setSettingsForm] = useState({
        primary_model_id: '',
        fallback_model_id: '',
        max_triggers: 20,
        min_poll_interval_min: 5,
        webhook_rate_limit: 5,
        default_session_permission_mode: 'default' as SessionPermissionMode,
        smart_model_routing_enabled: false,
        execution_mode: 'standard' as 'standard' | 'coordinator' | 'coordinator_strict',
        security_zone: 'standard',
    });
    const [settingsSaving, setSettingsSaving] = useState(false);
    const [settingsSaved, setSettingsSaved] = useState(false);
    const [settingsError, setSettingsError] = useState('');
    const settingsInitRef = useRef(false);

    // Sync settings form from server data on load
    useEffect(() => {
        if (agent && !settingsInitRef.current) {
            setSettingsForm({
                primary_model_id: agent.primary_model_id || '',
                fallback_model_id: agent.fallback_model_id || '',
                max_triggers: (agent as any).max_triggers ?? 20,
                min_poll_interval_min: (agent as any).min_poll_interval_min ?? 5,
                webhook_rate_limit: (agent as any).webhook_rate_limit ?? 5,
                default_session_permission_mode: defaultSessionPermissionModeFromAgent(agent),
                smart_model_routing_enabled: !!(agent as any).smart_model_routing?.enabled,
                execution_mode: ((agent as any).execution_mode || 'standard') as 'standard' | 'coordinator' | 'coordinator_strict',
                security_zone: (agent as any).security_zone || 'standard',
            });
            settingsInitRef.current = true;
        }
    }, [agent]);

    // Welcome message editor state (must be at top level -- not inside IIFE)
    const [wmDraft, setWmDraft] = useState('');
    const [wmSaved, setWmSaved] = useState(false);
    useEffect(() => { setWmDraft((agent as any)?.welcome_message || ''); }, [(agent as any)?.welcome_message]);

    // Reset cached state when switching to a different agent
    const prevIdRef = useRef(id);
    useEffect(() => {
        if (id && id !== prevIdRef.current) {
            prevIdRef.current = id;
            settingsInitRef.current = false;
            setSettingsSaved(false);
            setSettingsError('');
            setWmDraft('');
            setWmSaved(false);
            setCreatedAgentId(null);
            // Invalidate all queries for the old agent to force fresh data
            queryClient.invalidateQueries({ queryKey: ['agent', id] });
            // Re-apply hash so refresh preserves the current tab
            navigate(buildAgentDetailTabNavigation(location.pathname, location.search, activeTab), { replace: true });
        }
    }, [id]);

    // Load chat history + connect websocket when chat tab is active
    const IMAGE_EXTS = ['png', 'jpg', 'jpeg', 'gif', 'webp', 'bmp'];
    const normalizeToolCallMessage = (msg: AgentChatMessage): AgentChatMessage => {
        if (msg.role !== 'tool_call' || !msg.toolName) {
            return msg;
        }
        const rawToolResult = msg.toolRawResult ?? msg.toolResult;
        if (!rawToolResult) {
            return msg;
        }
        const normalized = normalizeToolCallResult(msg.toolName, rawToolResult);
        return {
            ...msg,
            toolResult: normalized.displayResult,
            toolRawResult: normalized.raw,
            toolMeta: normalized.toolMeta ?? msg.toolMeta ?? null,
        };
    };
    const parseChatMsg = (msg: AgentChatMessage): AgentChatMessage => {
        const runtimeEvent = normalizeRuntimeEventMessage(msg);
        if (runtimeEvent) return runtimeEvent;
        if (msg.role === 'tool_call') return normalizeToolCallMessage(msg);
        if (msg.role !== 'user') return msg;
        let parsed = { ...msg };
        // Standard web chat format: [file:name.pdf]\ncontent
        const newFmt = msg.content.match(/^\[file:([^\]]+)\]\n?/);
        if (newFmt) { parsed = { ...msg, fileName: newFmt[1], content: msg.content.slice(newFmt[0].length).trim() }; }
        // Feishu/Slack channel format: [文件已上传: workspace/uploads/name]
        const chanFmt = !newFmt && msg.content.match(/^\[\u6587\u4ef6\u5df2\u4e0a\u4f20: (?:workspace\/uploads\/)?([^\]\n]+)\]/);
        if (chanFmt) {
            const raw = chanFmt[1]; const fileName = raw.split('/').pop() || raw;
            parsed = { ...msg, fileName, content: msg.content.slice(chanFmt[0].length).trim() };
        }
        // Old format: [File: name.pdf]\nFile location:...\nQuestion: user_msg
        const oldFmt = !newFmt && !chanFmt && msg.content.match(/^\[File: ([^\]]+)\]/);
        if (oldFmt) {
            const fileName = oldFmt[1];
            const qMatch = msg.content.match(/\nQuestion: ([\s\S]+)$/);
            parsed = { ...msg, fileName, content: qMatch ? qMatch[1].trim() : '' };
        }
        // If file is an image and no imageUrl yet, build download URL for preview
        if (parsed.fileName && !parsed.imageUrl && id) {
            const ext = parsed.fileName.split('.').pop()?.toLowerCase() || '';
            if (IMAGE_EXTS.includes(ext)) {
                parsed.imageUrl = `/api/agents/${id}/files/download?path=workspace/uploads/${encodeURIComponent(parsed.fileName)}`;
            }
        }
        return parsed;
    };


    useEffect(() => {
        currentAgentIdRef.current = id;
    }, [id]);

    // Reset visible state whenever the viewed agent changes.
    // Existing background sockets keep running and will be cleaned up on unmount.
    useEffect(() => {
        sessionMsgAbortRef.current?.abort();
        sessionTranscriptLoadRef.current = null;
        activeSessionIdRef.current = null;
        setActiveSession(null);
        setChatMessagesAfterQueued(() => []);
        setChatMessagesSessionId(null);
        updateHistoryMsgs([]);
        setHistoryMessagesSessionId(null);
        setTransportNotice(null);
        setIsStreaming(false);
        setIsWaiting(false);
        resetActiveTransportState();
        activeRunStateRef.current = {};
        pendingUserMessagesRef.current = {};
        runtimeActivityAtRef.current = {};
        setActiveRunStateBySession({});
        setChatScope('mine');
        setAgentExpired(false);
        setSessionAccessError(null);
        settingsInitRef.current = false;
    }, [id]);

    useEffect(() => {
        if (!newSessionDraftRequest || !id) return;
        if (consumedNewSessionDraftRequestRef.current === newSessionDraftRequest.requestId) return;
        consumedNewSessionDraftRequestRef.current = newSessionDraftRequest.requestId;
        void createNewSession();
        // The request id is the exact-once navigation authority. createNewSession
        // deliberately consumes the current route and clears its state.
        // This effect must run after the Agent reset above and before the
        // default Session selection below so cross-Agent drafts remain active.
        // eslint-disable-next-line react-hooks/exhaustive-deps
    }, [id, newSessionDraftRequest?.requestId]);

    useEffect(() => {
        if (!canLoadAgentScopedData || !token || activeTab !== 'chat') return;
        if (newSessionDraftTransitionPending || isDraftHumanChatSession(activeSession)) return;
        fetchMySessions(false, id).then((data: any) => {
            if (currentAgentIdRef.current !== id) return;
            setSessionsLoading(false);
            if (requestedSessionId) {
                const requested = data?.find((session: any) => String(session.id) === String(requestedSessionId));
                if (requested) {
                    selectSession(requested);
                    return;
                }
                if (activeSessionIdRef.current && String(activeSessionIdRef.current) === String(requestedSessionId)) {
                    return;
                }
                selectSession({
                    id: requestedSessionId,
                    agent_id: id,
                    title: t('agent.chat.session', 'Session'),
                    source_channel: 'unknown',
                    listed_surface: 'chat',
                    read_only: true,
                    is_pending_session_lookup: true,
                });
                return;
            }
            if (data && data.length > 0) selectSession(data[0]);
        });
    }, [canLoadAgentScopedData, id, token, activeTab, requestedSessionId]);

    useEffect(() => {
        if (!canLoadAgentScopedData || activeTab !== 'chat' || !isManageMode || !canManage) return;
        fetchAllSessions();
    }, [canLoadAgentScopedData, activeTab, isManageMode, canManage, id]);

    useEffect(() => {
        if (!canLoadAgentScopedData || activeTab !== 'chat' || !requestedSessionId || !id) return;
        if (shouldPreserveActiveSessionForRequestedId(activeSession, requestedSessionId)) return;
        const known = [...sessions, ...allSessions].find((session: any) => String(session.id) === String(requestedSessionId));
        if (known) {
            selectSession(known);
            return;
        }
        if (activeSessionIdRef.current && String(activeSessionIdRef.current) === String(requestedSessionId)) return;
        selectSession({
            id: requestedSessionId,
            agent_id: id,
            title: t('agent.chat.session', 'Session'),
            source_channel: 'unknown',
            listed_surface: 'chat',
            read_only: true,
            is_pending_session_lookup: true,
        });
    }, [canLoadAgentScopedData, activeTab, requestedSessionId, id, activeSession, sessions, allSessions]);

    transportCallbacksRef.current = {
        onBackfill: backfillSessionTranscript,
        onLiveTailReady: ({ key, sessionId, acceptedAfterSequence }) => {
            sessionEventStoresRef.current[key] ??= createSessionEventStore(acceptedAfterSequence);
            transcriptReplayStateRef.current[key] ??= createEmptyTranscriptReplayState();
            if (activeSessionIdRef.current !== sessionId) return;
            setTransportNotice((current) => nextSessionBackfillNotice(current, t('agent.chat.sessionBackfillIncomplete', 'Latest activity is visible, but older session evidence is still recovering.'), Boolean(transcriptBackfillInFlightRef.current[key]) || sessionEventFullHydrationKeysRef.current.has(key)));
        },
        onDisconnected: ({ key, phase, isActiveRuntime }) => {
            setSessionPhase(key, phase);
            if (isActiveRuntime) syncActivePhase(phase);
        },
        onMessage: (context) => projectSessionSocketEvent(context, {
            applyTranscriptToSession,
            selectSession,
            fetchMySessions,
            setSessionPhase,
            sessionPhaseOf,
            syncActivePhase,
            setActiveRunState,
            markActiveRunTerminal,
            activeRunIdOf: (key: SessionRuntimeKey) => activeRunStateRef.current[key]?.runId ? String(activeRunStateRef.current[key].runId) : null,
            invalidateSessionRuntimeQueries,
            reconcileSessionTranscript: (agentId, sessionId) => {
                const sess = String(activeSession?.id || '') === sessionId ? activeSession : { id: sessionId };
                reconcileSessionTranscriptSafely(() => backfillSessionTranscript(sess, agentId), () => undefined);
            },
            shouldInvalidateToolCall: (key) => {
                const now = Date.now();
                if (now - (toolCallInvalidateAtRef.current[key] || 0) < 2000) return false;
                toolCallInvalidateAtRef.current[key] = now;
                return true;
            },
            isTerminalTranscriptToolMessage,
            normalizeToolCallMessage,
            parseChatMsg,
            setChatMessagesSessionId,
            setTransportNotice,
            enqueueChatMessagesUpdate: enqueueChatMessagesUpdateForSession,
            setChatMessagesAfterQueued: setChatMessagesAfterQueuedForSession,
            setCreatedAgentId,
            setAgentExpired,
            invalidateQuery: (queryKey) => {
                void queryClient.invalidateQueries({ queryKey });
            },
        }),
    };

    useEffect(() => {
        if (!canLoadAgentScopedData || activeTab !== 'chat' || !id || !activeSession?.id || activeSession?.is_pending_session_lookup === true || sessionAccessError || isDraftHumanChatSession(activeSession)) {
            setBranchLineage([]);
            return;
        }
        fetchBranchLineage(id, String(activeSession.id));
    }, [canLoadAgentScopedData, activeTab, id, activeSession?.id, activeSession?.is_pending_session_lookup, sessionAccessError]);

    useEffect(() => {
        return () => {
            sessionMsgAbortRef.current?.abort();
        };
    }, []);

    // Smart scroll: only auto-scroll if user is at the bottom
    const isNearBottom = useRef(true);
    const isFirstLoad = useRef(true);
    const [showScrollBtn, setShowScrollBtn] = useState(false);
    // Read-only history scroll-to-bottom
    const historyContainerRef = useRef<HTMLDivElement>(null);
    const [showHistoryScrollBtn, setShowHistoryScrollBtn] = useState(false);
    const handleHistoryScroll = () => {
        const el = historyContainerRef.current;
        if (!el) return;
        const distFromBottom = el.scrollHeight - el.scrollTop - el.clientHeight;
        setShowHistoryScrollBtn(distFromBottom > 200);
    };
    const scrollHistoryToBottom = () => {
        const el = historyContainerRef.current;
        if (el) el.scrollTop = el.scrollHeight;
        setShowHistoryScrollBtn(false);
    };
    // Auto-show button when history messages overflow the container
    useEffect(() => {
        const el = historyContainerRef.current;
        if (!el) return;
        // Use a small timeout to let the DOM render the messages first
        const timer = setTimeout(() => {
            const distFromBottom = el.scrollHeight - el.scrollTop - el.clientHeight;
            setShowHistoryScrollBtn(distFromBottom > 200);
        }, 100);
        return () => clearTimeout(timer);
    }, [historyMsgs, activeSession?.id]);

    const handleChatScroll = () => {
        const el = chatContainerRef.current;
        if (!el) return;
        const distFromBottom = el.scrollHeight - el.scrollTop - el.clientHeight;
        isNearBottom.current = distFromBottom < 5;
        setShowScrollBtn(distFromBottom > 200);
    };
    const scrollToBottom = () => {
        chatEndRef.current?.scrollIntoView({ behavior: 'instant' as ScrollBehavior });
        setShowScrollBtn(false);
    };
    useEffect(() => {
        if (!projectionTailScrollNonce) return;
        const frame = window.requestAnimationFrame(() => {
            chatEndRef.current?.scrollIntoView({ behavior: 'instant' as ScrollBehavior, block: 'end' });
            isNearBottom.current = true;
            setShowScrollBtn(false);
            window.setTimeout(() => chatInputRef.current?.focus(), 0);
        });
        return () => window.cancelAnimationFrame(frame);
    }, [projectionTailScrollNonce]);
    useEffect(() => {
        if (!chatEndRef.current) return;
        if (isFirstLoad.current && chatMessages.length > 0) {
            // First load: instant jump to bottom, no animation
            chatEndRef.current.scrollIntoView({ behavior: 'instant' as ScrollBehavior });
            isFirstLoad.current = false;
            // Auto-focus the input
            setTimeout(() => chatInputRef.current?.focus(), 100);
            return;
        }
        if (isNearBottom.current) {
            chatEndRef.current.scrollIntoView({ behavior: 'instant' as ScrollBehavior });
        }
    }, [chatMessages]);

    // Auto-focus input when switching sessions
    useEffect(() => {
        if (activeSession && activeTab === 'chat') {
            setTimeout(() => chatInputRef.current?.focus(), 150);
        }
    }, [activeSession?.id, activeTab]);

    const sendChatMsg = async () => {
        if (!id || !activeSession?.id) return;
        if (!chatInput.trim() && attachedFiles.length === 0) return;
        let userMsg = chatInput.trim();
        const goalObjective = userMsg;
        let contentForLLM = userMsg;
        let displayFiles = '';
        const attachmentPayload = attachedFiles.map((file) => ({
            name: file.name,
            path: file.path,
            is_image: Boolean(file.imageUrl),
            has_image_data: Boolean(file.imageUrl),
            conversion: file.conversion || null,
        }));

        if (attachedFiles.length === 0 && !goalModeRequested) {
            let parsedSlashCommand: ReturnType<typeof parseSlashCommandInput> = null;
            try {
                parsedSlashCommand = parseSlashCommandInput(userMsg);
            } catch (err) {
                setChatMessagesSessionId(String(activeSession.id));
                setChatMessagesAfterQueued(prev => [
                    ...prev,
                    parseChatMsg({ role: 'user', content: userMsg, timestamp: new Date().toISOString() }),
                    parseChatMsg({
                        role: 'event',
                        content: err instanceof Error ? err.message : t('agent.chat.commands.invalid', 'Invalid slash command.'),
                        eventType: 'runtime_action_failed', eventStatus: 'command_invalid',
                    }),
                ]);
                setChatInput('');
                return;
            }

            if (parsedSlashCommand) {
                let commandSession: any;
                try {
                    commandSession = await ensureDurableActiveSession();
                } catch (err: any) {
                    const msg = err?.message || t('agent.chat.sessionCreateFailed', 'Failed to create session');
                    setChatMessagesSessionId(String(activeSession.id));
                    setChatMessagesAfterQueued(prev => [...prev, parseChatMsg({ role: 'event', content: msg, eventType: 'runtime_action_failed', eventStatus: 'session_create_failed' })]);
                    return;
                }
                if (!commandSession?.id) return;
                const commandSessionId = String(commandSession.id);
                const activeRuntimeKey = buildSessionRuntimeKey(id, commandSessionId);
                syncActivePhase('queued');
                setTransportNotice(null);
                setSessionPhase(activeRuntimeKey, 'queued');
                setChatMessagesSessionId(commandSessionId);
                const commandMessageId = `session-command:${globalThis.crypto.randomUUID()}`;
                setChatMessagesAfterQueued(prev => [...prev, parseChatMsg({
                    role: 'user',
                    id: commandMessageId,
                    content: userMsg,
                    timestamp: new Date().toISOString(),
                })]);
                setChatInput('');

                let commandStartedRun = false;
                try {
                    const response = await ccParityApi.executeCommand(id, parsedSlashCommand.name, {
                        arguments: parsedSlashCommand.args,
                        session_id: commandSessionId,
                    });
                    const actionResult = commandResultRecord(response);
                    if (getSessionCommandUiAction(response)) {
                        await handleSessionCommandUiAction(response);
                        setChatMessagesAfterQueuedForSession(
                            commandSessionId,
                            prev => prev.filter(message => message.id !== commandMessageId),
                        );
                        return;
                    }
                    if (actionResult?.action === 'chat_prompt') {
                        const content = typeof actionResult.content === 'string' ? actionResult.content : userMsg;
                        const displayContent = typeof actionResult.display_content === 'string' ? actionResult.display_content : userMsg;
                        const run = await chatApi.startSessionRun(id, commandSessionId, {
                            content,
                            display_content: displayContent,
                            plan_mode_requested: actionResult.plan_mode_requested === true,
                            permission_mode: sessionPermissionMode,
                        });
                        commandStartedRun = true;
                        setPlanModeRequested(false);
                        setActiveRunState(activeRuntimeKey, sessionRunStateFromPayload(run));
                        invalidateSessionRuntimeQueries(id, commandSessionId);
                        return;
                    }
                    if (actionResult?.action === 'open_tab') {
                        const tab = typeof actionResult.tab === 'string' ? actionResult.tab : '';
                        if (tab) selectDetailTab(tab);
                        setChatMessagesAfterQueued(prev => [...prev, parseChatMsg({
                            role: 'assistant',
                            content: typeof actionResult.message === 'string' ? actionResult.message : formatSlashCommandResult(response),
                        })]);
                        invalidateSessionRuntimeQueries(id, commandSessionId);
                        return;
                    }
                    setChatMessagesAfterQueued(prev => [...prev, parseChatMsg({
                        role: 'assistant',
                        content: formatSlashCommandResult(response),
                    })]);
                    invalidateSessionRuntimeQueries(id, commandSessionId);
                } catch (err: any) {
                    const msg = err?.message || t('agent.chat.commands.failed', 'Failed to run command');
                    setChatMessagesAfterQueued(prev => [...prev, parseChatMsg({ role: 'event', content: msg, eventType: 'runtime_action_failed', eventStatus: 'command_failed' })]);
                } finally {
                    if (!commandStartedRun) {
                        syncActivePhase('idle');
                        setSessionPhase(activeRuntimeKey, 'idle');
                    }
                }
                return;
            }
        }

        if (attachedFiles.length > 0) {
            let filesPrompt = '';
            let filesDisplay = '';
            
            attachedFiles.forEach(file => {
                filesDisplay += `[📎 ${file.name}] `;
                if (file.imageUrl && supportsVision) {
                    filesPrompt += `[image_data:${file.imageUrl}]\n`;
                } else if (file.imageUrl) {
                    filesPrompt += `[图片文件已上传: ${file.name}，保存在 ${file.path || ''}]\n`;
                } else {
                    const wsPath = file.path || '';
                    const codePath = wsPath.replace(/^workspace\//, '');
                    const fileLoc = wsPath ? `\nFile location: ${wsPath} (for read_file/read_document tools)\nIn execute_code, use relative path: "${codePath}" (working directory is workspace/)\n` : '';
                    const conversionLoc = file.conversion?.markdownPath
                        ? `Markdown artifact (preferred): ${file.conversion.markdownPath}\nConversion metadata: ${file.conversion.metadataPath || ''}\n`
                        : '';
                    filesPrompt += `[File: ${file.name}]${fileLoc}${conversionLoc}\n${file.text}\n\n`;
                }
            });

            if (supportsVision && attachedFiles.some(f => f.imageUrl)) {
                contentForLLM = userMsg ? `${filesPrompt}\n${userMsg}` : `${filesPrompt}\n请分析这些文件`;
            } else {
                contentForLLM = userMsg ? `${filesPrompt}\nQuestion: ${userMsg}` : `Please analyze these files:\n\n${filesPrompt}`;
            }
            
            displayFiles = filesDisplay.trim();
            userMsg = userMsg ? `${displayFiles}\n${userMsg}` : displayFiles;
        }

        const fileName = attachedFiles.map(f => f.name).join(', ');
        let completedGoalRequestId: string | null = null;
        let acceptedInputId: string = globalThis.crypto.randomUUID();
        try {
            let started: { session: ChatSession; run: SessionRun } | null;
            if (goalModeRequested) {
                const goalSession = await ensureDurableActiveSession();
                if (!goalSession?.id) return;
                const goalFingerprint = JSON.stringify([
                    String(goalSession.id),
                    contentForLLM,
                    userMsg,
                    attachmentPayload.map((attachment) => [attachment.name, attachment.path]),
                ]);
                const previousGoalRequest = goalStartRequestRef.current;
                const goalRequestId = previousGoalRequest?.fingerprint === goalFingerprint
                    ? previousGoalRequest.requestId
                    : globalThis.crypto.randomUUID();
                goalStartRequestRef.current = { fingerprint: goalFingerprint, requestId: goalRequestId };
                const goal = await ccParityApi.startGoal(id, String(goalSession.id), {
                    request_id: goalRequestId,
                    objective: goalObjective || (fileName ? `Analyze ${fileName}` : contentForLLM.slice(0, 160)),
                    content: contentForLLM,
                    display_content: userMsg,
                    file_name: fileName,
                    attachments: attachmentPayload,
                    start_immediately: true,
                });
                const run = goal.run as SessionRun | null | undefined;
                if (!run?.run_id) throw new Error(t('agent.chat.goalStartFailed', 'The goal was created but its first turn did not start.'));
                completedGoalRequestId = goalRequestId;
                acceptedInputId = goalRequestId;
                started = { session: goalSession as ChatSession, run };
            } else {
                started = await startRunForActiveSession({
                    content: contentForLLM,
                    display_content: userMsg,
                    file_name: fileName,
                    attachments: attachmentPayload,
                    plan_mode_requested: planModeRequested,
                    permission_mode: sessionPermissionMode,
                    input_id: acceptedInputId,
                    idempotency_key: `session-input:${acceptedInputId}`,
                });
            }
            if (!started?.session?.id) return;
            const runSessionId = String(started.session.id);
            const activeRuntimeKey = buildSessionRuntimeKey(id, runSessionId);
            syncActivePhase('queued');
            setTransportNotice(null);
            setSessionPhase(activeRuntimeKey, 'queued');
            setChatMessagesSessionId(runSessionId);
            appendOptimisticUserMessage(activeRuntimeKey, runSessionId, {
                role: 'user',
                id: acceptedInputId,
                content: userMsg,
                fileName,
                imageUrl: attachedFiles.length === 1 ? attachedFiles[0].imageUrl : undefined,
                timestamp: new Date().toISOString(),
            });
            setChatInput('');
            setAttachedFiles([]);
            setPlanModeRequested(false);
            setGoalModeRequested(false);
            setActiveRunState(activeRuntimeKey, sessionRunStateFromPayload(started.run));
            invalidateSessionRuntimeQueries(id, runSessionId);
            if (completedGoalRequestId && goalStartRequestRef.current?.requestId === completedGoalRequestId) {
                goalStartRequestRef.current = null;
            }
        } catch (err: any) {
            setIsWaiting(false);
            setIsStreaming(false);
            const msg = err?.message || t('agent.chat.runStartFailed', 'Failed to start run');
            setChatMessagesSessionId(String(activeSession.id));
            setChatMessagesAfterQueued(prev => [...prev, parseChatMsg({ role: 'event', content: msg, eventType: 'runtime_action_failed', eventStatus: 'run_start_failed' })]);
        }
    };

    // Sends an explicit text message (not from the composer). Used by inline
    // chat cards (e.g. the ask_user_question clarification card) to post the
    // user's answer so the agent's blocked turn resumes. Reuses the same
    // startSessionRun path as sendChatMsg — if a run is active, the backend
    // queues it as a mid-run user message. When `planMode` is true the message carries
    // plan_mode_requested=true so the existing Plan Mode entry path activates
    // Plan Mode (used by the plan-mode-request approval card).
    const sendChatMessageText = async (text: string, options?: { planMode?: boolean }) => {
        const userMsg = text.trim();
        if (!id || !activeSession?.id || !userMsg) return;
        const acceptedInputId = globalThis.crypto.randomUUID();
        try {
            const started = await startRunForActiveSession({
                content: userMsg,
                display_content: userMsg,
                ...(options?.planMode ? { plan_mode_requested: true } : {}),
                permission_mode: sessionPermissionMode,
                input_id: acceptedInputId,
                idempotency_key: `session-input:${acceptedInputId}`,
            });
            if (!started?.session?.id) return;
            const runSessionId = String(started.session.id);
            const activeRuntimeKey = buildSessionRuntimeKey(id, runSessionId);
            syncActivePhase('queued');
            setTransportNotice(null);
            setSessionPhase(activeRuntimeKey, 'queued');
            setChatMessagesSessionId(runSessionId);
            appendOptimisticUserMessage(activeRuntimeKey, runSessionId, {
                role: 'user',
                id: acceptedInputId,
                content: userMsg,
                timestamp: new Date().toISOString(),
            });
            setActiveRunState(activeRuntimeKey, sessionRunStateFromPayload(started.run));
            invalidateSessionRuntimeQueries(id, runSessionId);
        } catch (err: any) {
            setIsWaiting(false);
            setIsStreaming(false);
            const msg = err?.message || t('agent.chat.runStartFailed', 'Failed to start run');
            setChatMessagesSessionId(String(activeSession.id));
            setChatMessagesAfterQueued(prev => [...prev, parseChatMsg({ role: 'event', content: msg, eventType: 'runtime_action_failed', eventStatus: 'run_start_failed' })]);
            throw err;
        }
    };

    const resolveSessionPermission = async (
        request: SessionPermissionRequest,
        action: 'allow_once' | 'allow_session' | 'deny',
    ) => {
        if (!id || !activeSession?.id || !request.permission_request_id) return;
        const sessionId = String(activeSession.id);
        const activeRuntimeKey = buildSessionRuntimeKey(id, sessionId);
        try {
            const response: any = await chatApi.resolveSessionPermission(id, sessionId, request.permission_request_id, {
                action,
            });
            if (response?.run?.run_id) {
                setActiveRunState(activeRuntimeKey, sessionRunStateFromPayload(response.run));
            }
            invalidateSessionRuntimeQueries(id, sessionId);
            await selectSession(activeSession);
        } catch (err: any) {
            const msg = err?.message || t('agent.chat.permission.resolveFailed', 'Failed to resolve permission request');
            showToast(msg, 'error');
        }
    };

    const handleBranchMessage = async (message: AgentChatMessage, mode: ConversationBranchMode, branchContent = '') => {
        const anchorEventId = message.transcriptEventId;
        if (!id || !activeSession?.id || !anchorEventId) return;
        const sessionId = String(activeSession.id);
        const content = branchContent.trim();
        const displayContent = content;
        if ((mode === 'edit' || mode === 'insert_before' || mode === 'insert_after' || mode === 'reply') && !content) return;

        try {
            const response = await chatApi.branchSession(id, sessionId, {
                mode,
                anchor_event_id: String(anchorEventId),
                content,
                display_content: displayContent,
                start_run: !['fork', 'branch'].includes(mode),
                permission_mode: sessionPermissionMode,
            });
            const branchSession = response.session;
            setSessions(prev => {
                if (prev.some((session: any) => String(session.id) === String(branchSession.id))) return prev;
                return [branchSession, ...prev];
            });
            setAllSessions(prev => {
                if (prev.some((session: any) => String(session.id) === String(branchSession.id))) return prev;
                return [branchSession, ...prev];
            });
            await selectSession(branchSession);
            ensureSessionWorkbenchRoute(String(branchSession.id));
            const draftContent = branchDraftContent(response, message.content || '');
            if (draftContent) setChatInput(draftContent);
            if (response.run?.run_id) {
                const branchKey = buildSessionRuntimeKey(id, String(branchSession.id));
                setActiveRunState(branchKey, sessionRunStateFromPayload(response.run));
                invalidateSessionRuntimeQueries(id, String(branchSession.id));
            }
            await fetchBranchLineage(id, String(branchSession.id));
        } catch (err: any) {
            const msg = err?.message || t('agent.chat.branch.failed', 'Failed to create branch');
            showToast(msg, 'error');
        }
    };

    const handleChatFile = async (e: React.ChangeEvent<HTMLInputElement>) => {
        const files = Array.from(e.target.files || []);
        if (!files.length) return;
        const allowedFiles = files.slice(0, 10 - attachedFiles.length);
        if (!allowedFiles.length) {
            showToast(t('agent.upload.limit', 'Limit of 10 attached files reached.'), 'error');
            return;
        }
        
        setUploading(true); setUploadProgress(0);
        try {
            const uploadPromises = allowedFiles.map(file => {
                const { promise } = uploadFileWithProgress(
                    `/chat/upload`,
                    file,
                    () => {}, // Avoid updating progress per file to prevent flickering, could implement total progress
                    id ? { agent_id: id } : undefined,
                );
                return promise;
            });
            const results = await Promise.all(uploadPromises);
            const newAttached = results.map(data => ({
                name: data.filename,
                text: data.extracted_text,
                path: data.workspace_path,
                imageUrl: data.image_data_url || undefined,
                conversion: data.conversion ? {
                    status: data.conversion.status,
                    markdownPath: data.conversion.markdown_path,
                    metadataPath: data.conversion.metadata_path,
                    engine: data.conversion.engine,
                } : undefined,
            }));
            setAttachedFiles(prev => [...prev, ...newAttached].slice(0, 10));
        } catch (err: any) {
            if (err?.message !== 'Upload cancelled') showToast(t('agent.upload.failed'), 'error');
        } finally { 
            setUploading(false); setUploadProgress(-1); uploadAbortRef.current = null; 
            if (fileInputRef.current) fileInputRef.current.value = ''; 
        }
    };

    // Clipboard paste handler — auto-upload pasted images
    const handlePaste = async (e: React.ClipboardEvent<HTMLTextAreaElement>) => {
        const items = e.clipboardData?.items;
        if (!items) return;
        
        const filesToUpload: File[] = [];
        for (let i = 0; i < items.length; i++) {
            if (items[i].type.startsWith('image/')) {
                const blob = items[i].getAsFile();
                if (blob) {
                    const ext = blob.type.split('/')[1] || 'png';
                    const fileName = `paste-${Date.now()}-${i}.${ext}`;
                    filesToUpload.push(new File([blob], fileName, { type: blob.type }));
                }
            }
        }
        
        if (!filesToUpload.length) return;
        e.preventDefault();
        const allowedFiles = filesToUpload.slice(0, 10 - attachedFiles.length);
        if (!allowedFiles.length) {
            showToast(t('agent.upload.limit', 'Limit of 10 attached files reached.'), 'error');
            return;
        }

        setUploading(true); setUploadProgress(0);
        try {
            const uploadPromises = allowedFiles.map(file => {
                const { promise } = uploadFileWithProgress(
                    `/chat/upload`,
                    file,
                    () => {},
                    id ? { agent_id: id } : undefined,
                );
                return promise;
            });
            const results = await Promise.all(uploadPromises);
            const newAttached = results.map(data => ({
                name: data.filename,
                text: data.extracted_text,
                path: data.workspace_path,
                imageUrl: data.image_data_url || undefined,
                conversion: data.conversion ? {
                    status: data.conversion.status,
                    markdownPath: data.conversion.markdown_path,
                    metadataPath: data.conversion.metadata_path,
                    engine: data.conversion.engine,
                } : undefined,
            }));
            setAttachedFiles(prev => [...prev, ...newAttached].slice(0, 10));
        } catch (err: any) {
            if (err?.message !== 'Upload cancelled') showToast(t('agent.upload.failed'), 'error');
        } finally { setUploading(false); setUploadProgress(-1); uploadAbortRef.current = null; }
    };

    // Expandable activity log
    const [expandedLogId, setExpandedLogId] = useState<string | null>(null);
    const [logFilter, setLogFilter] = useState<string>('user'); // 'user' | 'backend' | 'heartbeat' | 'schedule' | 'messages'

    const { data: metrics } = useQuery({
        queryKey: ['metrics', id],
        queryFn: () => agentApi.getMetrics(id!).catch(() => null),
        enabled: canLoadAgentScopedData && activeTab === 'status',
        retry: false,
    });

    const { data: llmModels = [] } = useQuery({
        queryKey: ['llm-models'],
        queryFn: () => enterpriseApi.llmModels(),
        enabled: activeTab === 'settings' || activeTab === 'status' || activeTab === 'chat',
    });

    const { data: persistedRuntimeSummary } = useQuery({
        queryKey: ['chat-runtime-summary', id, activeSession?.id],
        queryFn: () => chatApi.getRuntimeSummary(
            String(activeSession!.id),
            activeSession?.operator_view
                ? { operatorView: true, operatorReason: 'Agent session administration' }
                : undefined,
        ),
        enabled: canLoadAgentScopedData && activeTab === 'chat' && !!activeSession?.id && activeSession?.is_pending_session_lookup !== true && !sessionAccessError && !isDraftHumanChatSession(activeSession),
        refetchInterval: activeTab === 'chat' && activeSession?.id && activeSession?.is_pending_session_lookup !== true && !sessionAccessError && !isDraftHumanChatSession(activeSession) ? 10000 : false,
    });

    const { data: activeSessionRun, dataUpdatedAt: activeSessionRunObservedAt } = useQuery({
        queryKey: ['chat-active-run', id, activeSession?.id],
        queryFn: () => chatApi.getActiveSessionRun(
            id!,
            String(activeSession!.id),
            activeSession?.operator_view
                ? { operatorView: true, operatorReason: 'Agent session administration' }
                : undefined,
        ),
        enabled: canLoadAgentScopedData && activeTab === 'chat' && !!activeSession?.id && !!activeSession && isWritableSession(activeSession) && !isDraftHumanChatSession(activeSession),
        refetchInterval: (query) => activeRunPollInterval(
            query.state.data as SessionRun | null | undefined,
            hasLocalSessionRuntimeState(activeRunStateRef.current, sessionUiStateRef.current, id && activeSession?.id ? buildSessionRuntimeKey(id, String(activeSession.id)) : ''),
        ),
    });

    useEffect(() => {
        if (!id || !activeSession?.id || !isWritableSession(activeSession)) return;
        const key = buildSessionRuntimeKey(id, String(activeSession.id));
        if (activeSessionRun && isLiveRun(activeSessionRun)) {
            if (shouldIgnoreObservedActiveRun({
                key,
                run: sessionRunStateFromPayload(activeSessionRun),
                terminalRunIds: locallyTerminalRunIdsRef.current,
                terminalSessionKeys: locallyTerminalSessionKeysRef.current,
            })) {
                invalidateSessionRuntimeQueries(id, String(activeSession.id), false);
                return;
            }
            const observedUiState = observeActiveRunState(key, sessionRunStateFromPayload(activeSessionRun));
            setActivePhase(observedUiState.phase);
            setIsWaiting(observedUiState.isWaiting);
            setIsStreaming(observedUiState.isStreaming);
            return;
        }
        const staleRuntimeState = hasLocalSessionRuntimeState(activeRunStateRef.current, sessionUiStateRef.current, key);
        if (shouldReconcileTranscriptOnActiveRunAbsence({
            observedActiveRun: activeSessionRun,
            hasLocalActiveRuntime: staleRuntimeState,
        })) {
            // The authoritative active-run read observed the turn's terminal state
            // while live frames may have been lost; the UI grace below must not
            // defer projection truth. Reconcile the durable transcript now.
            reconcileSessionTranscriptSafely(() => backfillSessionTranscript(activeSession, id), () => undefined);
        }
        if (shouldClearStaleRuntimeState({
            hasStaleRuntimeState: staleRuntimeState,
            lastRuntimeActivityAt: runtimeActivityAtRef.current[key],
            now: Date.now(),
            graceMs: ACTIVE_RUN_ABSENCE_GRACE_MS,
        })) {
            setActiveRunState(key, null);
            syncActivePhase('idle');
            invalidateSessionRuntimeQueries(id, String(activeSession.id), false);
            selectSession(activeSession);
            fetchMySessions(true, id);
        }
    }, [activeSessionRun, activeSessionRunObservedAt, activeSession?.id, id]);

    const supportsVision = !!agent?.primary_model_id && llmModels.some(
        (m: any) => m.id === agent.primary_model_id && m.supports_vision
    );

    const activeTimelineMessages = activeSession && isWritableSession(activeSession) ? chatMessages : historyMsgs;
    const currentActiveRunState = id && activeSession?.id
        ? activeRunStateBySession[buildSessionRuntimeKey(id, String(activeSession.id))] || null
        : null;

    const runtimeSummary: ChatRuntimeSummary | null = React.useMemo(() => {
        if (!activeSession) return null;
        const activeModel = llmModels.find((model: any) => model.id === agent?.primary_model_id);
        return buildRuntimeSummary({
            persistedSummary: persistedRuntimeSummary,
            activeModel,
            agentPrimaryModelId: agent?.primary_model_id,
            agentContextWindowSize: agent?.context_window_size,
            messages: activeTimelineMessages,
            connected: isWritableSession(activeSession) ? wsConnected : false,
        });
    }, [activeSession, activeTimelineMessages, agent?.context_window_size, agent?.primary_model_id, llmModels, persistedRuntimeSummary, wsConnected]);

    const { data: permData } = useQuery({
        queryKey: ['agent-permissions', id],
        queryFn: () => agentApi.getPermissions(id!),
        enabled: canLoadAgentScopedData && activeTab === 'chat',
    });
    const handleTogglePlanMode = React.useCallback(() => {
        setPlanModeRequested((value) => !value);
        setGoalModeRequested(false);
    }, []);
    const handleToggleGoalMode = React.useCallback(() => {
        setGoalModeRequested((value) => !value);
        setPlanModeRequested(false);
    }, []);
    const handleDismissSessionCommandControl = React.useCallback(() => {
        setSessionCommandControl(null);
    }, []);
    const handleRemoveAttachedFile = React.useCallback((index: number) => {
        setAttachedFiles((prev) => prev.filter((_, i) => i !== index));
    }, []);
    const handleAbortGeneration = React.useCallback(() => {
        if (!id || !activeSession?.id) return;
        const activeRuntimeKey = buildSessionRuntimeKey(id, String(activeSession.id));
        const activeRun = activeRunStateRef.current[activeRuntimeKey];
        if (activeRun?.runId) {
            chatApi.cancelSessionRun(id, String(activeSession.id), activeRun.runId).catch((err) => {
                console.warn('Failed to cancel chat run:', err);
            });
            markActiveRunTerminal(activeRuntimeKey);
            syncActivePhase('cancelled');
            setSessionPhase(activeRuntimeKey, 'cancelled');
            return;
        }
        const activeSocket = getSessionSocket(id, String(activeSession.id));
        if (activeSocket?.readyState === WebSocket.OPEN) {
            activeSocket.send(JSON.stringify({ type: 'abort' }));
        }
    }, [activeSession?.id, id]);

    const [deleteSessionConfirm, setDeleteSessionConfirm] = useState<{ sessionId: string; title: string } | null>(null);
    const [uploadToast, setUploadToast] = useState<{ message: string; type: 'success' | 'error' } | null>(null);
    const [editingRole, setEditingRole] = useState(false);
    const [roleInput, setRoleInput] = useState('');
    const [editingName, setEditingName] = useState(false);
    const [nameInput, setNameInput] = useState('');
    const showToast = (message: string, type: 'success' | 'error' = 'success') => {
        setUploadToast({ message, type });
        setTimeout(() => setUploadToast(null), 3000);
    };
    // Redirect to /agents/new when tenant switches while viewing HR system agent
    useEffect(() => {
        if ((agent as any)?.agent_class !== 'internal_system') return;
        const handler = (e: StorageEvent) => {
            if (e.key === 'current_tenant_id') navigate('/agents/new', { replace: true });
        };
        window.addEventListener('storage', handler);
        return () => window.removeEventListener('storage', handler);
    }, [(agent as any)?.agent_class, navigate]);

    if (isLoading) {
        return <div className="agent-detail-placeholder">{t('common.loading')}</div>;
    }

    if (!agent) {
        const status = (agentError as { status?: number } | null | undefined)?.status;
        const message = status === 403
            ? t('agent.detail.accessDenied', 'You do not have access to this employee.')
            : t('agent.detail.loadFailed', 'Unable to load this employee right now.');
        return <div className="agent-detail-placeholder">{message}</div>;
    }

    // Compute display status
    const computeStatusKey = () => {
        if (agent.status === 'error') return 'error';
        if (agent.status === 'creating') return 'creating';
        if (agent.status === 'stopped') return 'stopped';
        return agent.status === 'running' ? 'running' : 'idle';
    };
    const statusKey = computeStatusKey();
    const isSystemHrRaw = (agent as any).agent_class === 'internal_system';
    const isSystemHr = isSystemHrRaw && !isManageMode;
    const sessionWorkbenchMode = Boolean(routeSessionId) || isSessionWorkbenchRoute(activeTab, location.search);
    const pendingSessionLookup = activeSession?.is_pending_session_lookup === true;

    // HR system agent: force chat-only mode
    if (isSystemHr && activeTab !== 'chat') {
        setActiveTab('chat');
    }

    const visibleAgentTabs = getVisibleAgentDetailTabs(agent);
    if (!isSystemHr && visibleAgentTabs.length > 0 && !visibleAgentTabs.includes(activeTab as AgentDetailTab)) {
        setActiveTab(visibleAgentTabs[0]);
    }

    const visibleWorkbenchAreas = AGENT_WORKBENCH_AREAS
        .map((area) => ({
            ...area,
            tabs: area.tabs.filter((tab) => isAgentDetailTabVisible(agent, tab)),
        }))
        .filter((area) => area.tabs.length > 0);
    const activeWorkbenchArea =
        visibleWorkbenchAreas.find((area) => area.tabs.includes(activeTab as AgentDetailTab)) ||
        visibleWorkbenchAreas[0];

    return (
        <>
            <div className={sessionWorkbenchMode ? 'agent-detail-session-only' : undefined}>
                {/* Header */}
                {!sessionWorkbenchMode && (isSystemHr ? (
                    <div className="page-header">
                        <div className="agent-detail-hr-header">
                            <div className="agent-detail-hr-avatar">&#x1F464;</div>
                            <div>
                                <h1 className="page-title agent-detail-title-flush">{t('nav.newAgent', 'Create Digital Employee')}</h1>
                                <p className="page-subtitle agent-detail-subtitle-tight">{t('hrChat.subtitle', 'Tell the HR agent what kind of digital employee you need')}</p>
                            </div>
                        </div>
                    </div>
                ) : (
                <div className="page-header">
                    <div className="agent-detail-header-main">
                        <div className="agent-detail-avatar">{(Array.from(agent.name || 'A')[0] as string || 'A').toUpperCase()}</div>
                        <div className="agent-detail-header-text">
                            {canManage && editingName ? (
                                <input
                                    className="page-title agent-detail-name-input"
                                    autoFocus
                                    value={nameInput}
                                    onChange={e => setNameInput(e.target.value)}
                                    onBlur={async () => {
                                        setEditingName(false);
                                        if (nameInput.trim() && nameInput !== agent.name) {
                                            await agentApi.update(id!, { name: nameInput.trim() } as any);
                                            queryClient.invalidateQueries({ queryKey: ['agent', id] });
                                        } else {
                                            setNameInput(agent.name);
                                        }
                                    }}
                                    onKeyDown={async e => {
                                        if (e.key === 'Enter') (e.target as HTMLInputElement).blur();
                                        if (e.key === 'Escape') { setEditingName(false); setNameInput(agent.name); }
                                    }}
                                />
                            ) : (
                                <h1 className={`page-title agent-detail-name${canManage ? ' is-editable' : ''}`}
                                    title={canManage ? "Click to edit name" : undefined}
                                    onClick={() => { if (canManage) { setNameInput(agent.name); setEditingName(true); } }}
                                >
                                    {agent.name}
                                </h1>
                            )}
                            <p className="page-subtitle agent-detail-status-line">
                                <span className={`status-dot ${statusKey}`} />
                                {t(`agent.status.${statusKey}`)}
                                {canManage && editingRole ? (
                                    <textarea
                                        autoFocus
                                        value={roleInput}
                                        onChange={e => setRoleInput(e.target.value)}
                                        onBlur={async () => {
                                            setEditingRole(false);
                                            if (roleInput !== agent.role_description) {
                                                await agentApi.update(id!, { role_description: roleInput } as any);
                                                queryClient.invalidateQueries({ queryKey: ['agent', id] });
                                            }
                                        }}
                                        onKeyDown={async e => {
                                            if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); (e.target as HTMLTextAreaElement).blur(); }
                                            if (e.key === 'Escape') { setEditingRole(false); setRoleInput(agent.role_description || ''); }
                                        }}
                                        rows={2}
                                        className="agent-detail-role-input"
                                    />
                                ) : (
                                    <span
                                        title={canManage ? (agent.role_description || 'Click to edit') : (agent.role_description || '')}
                                        onClick={() => { if (canManage) { setRoleInput(agent.role_description || ''); setEditingRole(true); } }}
                                        className={`agent-detail-role${canManage ? ' is-editable' : ''}`}
                                    >
                                        {agent.role_description ? `· ${agent.role_description}` : (canManage ? <span className="agent-detail-role-placeholder">· {t('agent.fields.role', 'Click to add a description...')}</span> : null)}
                                    </span>
                                )}
                                {(agent as any).is_expired && (
                                    <span className="agent-detail-expired-badge">{t('agent.settings.expiry.expired', 'Expired')}</span>
                                )}
                                {(agent as any).agent_type === 'local_agent' && (
                                    <span className="agent-detail-local-badge">{t('nav.localBadge', 'Local')}</span>
                                )}
                                {!(agent as any).is_expired && (agent as any).expires_at && (
                                    <span className="agent-detail-expires-text">
                                        {t('agent.settings.expiry.expiresAt', 'Expires: {{time}}', { time: new Date((agent as any).expires_at).toLocaleString() })}
                                    </span>
                                )}                                {isAdmin && (
                                    <button
                                        onClick={openExpiryModal}
                                        title={t('agent.settings.expiry.editTitle', 'Edit expiry time')}
                                        className="agent-detail-expiry-edit"
                                    >✏️ {t((agent as any).expires_at || (agent as any).is_expired ? 'agent.settings.expiry.renew' : 'agent.settings.expiry.setExpiry')}</button>
                                )}
                            </p>
                        </div>
                    </div>
                    <div className="agent-detail-header-actions">
                        <button className="btn btn-primary" onClick={() => selectDetailTab('chat')}>{t('agent.actions.chat')}</button>
                        {!isLocalAgentRuntimeType(agent) && (
                            <>
                                {agent.status === 'stopped' ? (
                                    <button className="btn btn-secondary" onClick={async () => { await agentApi.start(id!); queryClient.invalidateQueries({ queryKey: ['agent', id] }); }}>{t('agent.actions.start')}</button>
                                ) : agent.status === 'running' ? (
                                    <button className="btn btn-secondary" onClick={async () => { await agentApi.stop(id!); queryClient.invalidateQueries({ queryKey: ['agent', id] }); }}>{t('agent.actions.stop')}</button>
                                ) : null}
                            </>
                        )}
	                    </div>
	                </div>
	                ))}

                {/* Workbench navigation — hidden for HR system agent */}
                {!isSystemHr && !sessionWorkbenchMode && (
                    <div className="agent-workbench-nav" data-testid="agent-workbench-nav">
                        <div className="agent-workbench-areas" role="tablist" aria-label={t('agent.workbench.title', 'Agent workbench')}>
                            {visibleWorkbenchAreas.map((area) => (
                                <button
                                    key={area.id}
                                    type="button"
                                    className={`agent-workbench-area ${activeWorkbenchArea?.id === area.id ? 'active' : ''}`}
                                    onClick={() => selectDetailTab(area.tabs.includes(area.primaryTab) ? area.primaryTab : area.tabs[0])}
                                >
                                    {t(area.labelKey, area.fallback)}
                                </button>
                            ))}
                        </div>
                        {activeWorkbenchArea && (
                            <div className="agent-workbench-subnav" aria-label={t('agent.workbench.sectionTabs', 'Section tabs')}>
                                {activeWorkbenchArea.tabs.map((tab) => (
                                    <button
                                        key={tab}
                                        type="button"
                                        className={`agent-workbench-subtab ${activeTab === tab ? 'active' : ''}`}
                                        onClick={() => selectDetailTab(tab)}
                                    >
                                        {t(`agent.tabs.${tab}`, AGENT_TAB_LABELS[tab])}
                                    </button>
                                ))}
                            </div>
                        )}
                    </div>
                )}

                <Suspense fallback={<AgentDetailSectionFallback />}>
                {/* ── Enhanced Status Tab ── */}
                {activeTab === 'status' && (
                    <AgentStatusSection
                        agent={agent}
                        llmModels={llmModels}
                        metrics={metrics}
                        activityLogs={activityLogs}
                        capabilityInstalls={capabilityInstalls}
                        channelCapabilities={channelCapabilities}
                        statusKey={statusKey}
                        onSelectTab={selectDetailTab}
                    />
                )}

                {/* ── Aware Tab ── */}
                {activeTab === 'aware' && (
                    <AgentAwareSection
                        agentId={id!}
                        awareTriggers={awareTriggers}
                        reflectionSessions={reflectionSessions}
                        reflectionMessages={reflectionMessages}
                        expandedReflection={expandedReflection}
                        showAllTriggers={showAllTriggers}
                        reflectionPage={reflectionPage}
                        onSetExpandedReflection={setExpandedReflection}
                        onSetReflectionMessages={setReflectionMessages}
                        onSetShowAllTriggers={setShowAllTriggers}
                        onSetReflectionPage={setReflectionPage}
                        onRefetchTriggers={refetchTriggers}
                        autonomyOverview={autonomyOverview}
                        onRefetchAutonomy={refetchAutonomy}
                        onLoadReflectionMessages={(sessionId) => chatApi.getSessionMessages(
                            id!,
                            sessionId,
                            {
                                operatorView: true,
                                operatorReason: 'Agent reflection monitoring',
                            },
                        )}
                    />
                )}


                {/* ── Mind Tab (Soul + Memory + Heartbeat) ── */}
                {activeTab === 'knowledge' && (
                    <AgentKnowledgeSection
                        agentId={id!}
                        canEdit={(agent as any)?.access_level !== 'use'}
                        onNavigateTab={selectDetailTab}
                    />
                )}

                {/* ── Evolution Tab (skill lifecycle + evolution timeline) ── */}
                {activeTab === 'evolution' && (
                    <AgentEvolutionSection
                        agentId={id!}
                        active={canLoadAgentScopedData}
                        canManage={canManage}
                    />
                )}
                {activeTab === 'workflows' && (
                    <AgentWorkflowsSection agentId={id!} canManage={(agent as any)?.access_level !== 'use'} />
                )}

                {/* ── Extensions Tab: MCP/plugins, Skills, and Sub-agents ── */}
                {activeTab === 'extensions' && (
                    <AgentExtensionsSection agentId={id!} canManage={canManage} />
                )}

                {/* ── A2A Tab ── */}
                {
                    activeTab === 'a2a' && <AgentA2ASection agentId={id!} canManage={canManage} />
                }

                {/* ── Workspace Tab ── */}
                {
                    activeTab === 'workspace' && (
                        (agent as any)?.agent_type === 'local_agent' ? (
                            <LocalAgents key="workspace" agentId={id!} agentName={agent.name} embedded initialTab="workspace" />
                        ) : (
                            <AgentWorkspaceSection agentId={id!} canUseOperatorView={canManage} />
                        )
                    )
                }

                {
                    activeTab === 'tasks' && <AgentBusinessTasksSection agentId={id!} />
                }

                {
                    activeTab === 'chat' && (
                        (agent as any)?.agent_type === 'local_agent' ? (
                            <LocalAgentChatSection key="chat" agentId={id!} agent={agent} agentPermissions={permData ?? null} />
                        ) : sessionAccessError?.sessionId === String(activeSession?.id) ? (
                            <SessionAccessErrorSurface status={sessionAccessError.status} onBack={() => {
                                sessionMsgAbortRef.current?.abort(); setActiveSession(null); setSessionAccessError(null);
                                navigate('/agents', { replace: true });
                            }} />
                        ) : pendingSessionLookup ? (
                            <SessionResolvingSurface />
                        ) : (
                        <AgentChatSection
                            agentId={id}
                            agent={agent}
                            currentUser={currentUser}
                            isAdmin={isSystemHr ? false : isAdmin}
                            chatScope={chatScope}
                            onSetChatScope={setChatScope}
                            onLoadAllSessions={fetchAllSessions}
                            onCreateNewSession={createNewSession}
                            sessionsLoading={sessionsLoading}
                            sessions={sessions}
                            activeSession={activeSession}
                            sessionTransitionPending={newSessionDraftTransitionPending}
                            branchLineage={branchLineage}
                            branchLineageLoading={branchLineageLoading}
                            onSelectBranchSession={selectBranchSession}
                            wsConnected={wsConnected}
                            transportPhase={transportPhase}
                            transportReconnectAttempt={transportReconnectAttempt}
                            onReconnectTransport={reconnectActiveTransport}
                            allSessions={allSessions}
                            allSessionsLoading={allSessionsLoading}
                            allUserFilter={allUserFilter}
                            onSetAllUserFilter={setAllUserFilter}
                            onSelectSession={selectSession}
                            onDeleteSession={deleteSession}
                            historyContainerRef={historyContainerRef}
                            onHistoryScroll={handleHistoryScroll}
                            historyMsgs={historyMsgs}
                            historyMessagesSessionId={historyMessagesSessionId}
                            showHistoryScrollBtn={showHistoryScrollBtn}
                            onScrollHistoryToBottom={scrollHistoryToBottom}
                            chatContainerRef={chatContainerRef}
                            onChatScroll={handleChatScroll}
                            chatMessages={chatMessages}
                            chatMessagesSessionId={chatMessagesSessionId}
                            runtimeSummary={runtimeSummary}
                            agentPermissions={permData ?? null}
                            transportNotice={transportNotice}
                            isWaiting={isWaiting}
                            runtimePhase={activePhase}
                            activeRunStatus={currentActiveRunState?.status || null}
                            activeRunId={currentActiveRunState?.runId || null}
                            runtimeBudget={currentActiveRunState?.runtimeBudget || null}
                            planModeRequested={planModeRequested}
                            onTogglePlanMode={handleTogglePlanMode}
                            goalModeRequested={goalModeRequested}
                            onToggleGoalMode={handleToggleGoalMode}
                            sessionPermissionMode={sessionPermissionMode}
                            onSetSessionPermissionMode={handleSetSessionPermissionMode}
                            sessionCommandControl={sessionCommandControl}
                            onDismissSessionCommandControl={handleDismissSessionCommandControl}
                            onRunSessionCommand={handleRunSessionCommandFromUi}
                            onResolveSessionPermission={resolveSessionPermission}

                            chatEndRef={chatEndRef}
                            showScrollBtn={showScrollBtn}
                            onScrollToBottom={scrollToBottom}
                            agentExpired={agentExpired}
                            attachedFiles={attachedFiles}
                            onRemoveAttachedFile={handleRemoveAttachedFile}
                            fileInputRef={fileInputRef}
                            onHandleChatFile={handleChatFile}
                            uploading={uploading}
                            uploadProgress={uploadProgress}
                            uploadAbortRef={uploadAbortRef}
                            chatInputRef={chatInputRef}
                            chatInput={chatInput}
                            onSetChatInput={setChatInput}
	                            onHandlePaste={handlePaste}
	                            onSendChatMsg={sendChatMsg}
	                            onBranchMessage={handleBranchMessage}
	                            onSendMessage={sendChatMessageText}
                            onEnterPlanMode={(reason: string) => sendChatMessageText(reason, { planMode: true })}
                            isStreaming={isStreaming}
                            sessionOnly={sessionWorkbenchMode}
                            onAbortGeneration={handleAbortGeneration}
                        />
                        )
                    )
                }

                {/* Agent creation success banner (HR Agent flow) */}
                {createdAgentId && activeTab === 'chat' && (
                    <div className="agent-detail-created-banner">
                        <span className="agent-detail-created-icon">&#x2705;</span>
                        <div>
                            <div className="agent-detail-created-title">{t('hrChat.created', 'Digital employee created successfully!')}</div>
                            <div className="agent-detail-created-desc">{t('hrChat.createdDesc', 'You can now visit the detail page to further customize or start chatting.')}</div>
                        </div>
                        <button className="btn btn-primary agent-detail-created-action" onClick={() => navigate(`/agents/${createdAgentId}`)}>
                            {t('hrChat.goToAgent', 'Go to Detail Page')}
                        </button>
                        <button className="agent-detail-created-close" onClick={() => setCreatedAgentId(null)}>&#x2715;</button>
                    </div>
                )}

                {
                    activeTab === 'activityLog' && (
                        <AgentActivityLogSection
                            activityLogs={activityLogs}
                            toolFailureSummary={toolFailureSummary}
                            logFilter={logFilter}
                            expandedLogId={expandedLogId}
                            onFilterChange={setLogFilter}
                            onToggleExpandedLog={setExpandedLogId}
                            canUseOperatorView={canManage}
                            operatorView={activityOperatorView}
                            onOperatorViewChange={setActivityOperatorView}
                        />
                    )
                }

                {/* ── Feishu Channel Tab ── */}

                {/* ── Approvals Tab ── */}
                {
                    activeTab === 'approvals' && <AgentApprovalsSection agentId={id!} />}

                {/* ── Settings Tab ── */}
                {
                    activeTab === 'settings' && (
                        <AgentSettingsSection
                            agentId={id!}
                            agent={agent}
                            llmModels={llmModels}
                            canManage={canManage}
                            onReviewPatrolPlan={async (request) => {
                                const handoff = buildAssignmentHandoff(request, 'plan');
                                const session = await chatApi.createSession(id!, buildAssignmentSessionTitle(handoff.content));
                                navigate(`/agents/${id}/sessions/${session.id}`, {
                                    state: { assignmentDraft: handoff },
                                });
                            }}
                            settingsForm={settingsForm}
                            onSettingsFormChange={setSettingsForm}
                            settingsSaving={settingsSaving}
                            settingsSaved={settingsSaved}
                            settingsError={settingsError}
                            onSetSettingsSaving={setSettingsSaving}
                            onSetSettingsSaved={setSettingsSaved}
                            onSetSettingsError={setSettingsError}
                            onResetSettingsInit={() => {
                                settingsInitRef.current = false;
                            }}
                            wmDraft={wmDraft}
                            wmSaved={wmSaved}
                            onSetWmDraft={setWmDraft}
                            onSetWmSaved={setWmSaved}
                        />
                    )
                }
                </Suspense>
            </div >

            <ConfirmModal
                open={!!deleteSessionConfirm}
                title={t('chat.deleteSession', 'Delete session')}
                message={t(
                    'chat.deleteConfirmWithTitle',
                    'Delete "{{title}}" and all its messages? This cannot be undone.',
                    { title: deleteSessionConfirm?.title || t('agent.chat.session', 'Session') },
                )}
                confirmLabel={t('common.delete')}
                danger
                onCancel={() => setDeleteSessionConfirm(null)}
                onConfirm={confirmDeleteSession}
            />

            <ConfirmModal
                open={fullAccessPromptOpen}
                title={t('agent.chat.permission.fullAccessTitle', 'Use Full access for this session?')}
                message={t(
                    'agent.chat.permission.fullAccessMessage',
                    'Full access skips routine session approval prompts. Enterprise access, safety, and destructive-action rules always apply.',
                )}
                confirmLabel={t('agent.chat.permission.fullAccessConfirm', 'Use Full access')}
                onCancel={handleCancelFullAccess}
                onConfirm={handleConfirmFullAccess}
            />

            {
                uploadToast && (
                    <div className={`agent-detail-upload-toast ${uploadToast.type === 'success' ? 'is-success' : 'is-error'}`}>
                        {''}{uploadToast.message}
                    </div>
                )
            }

            {/* ── Expiry Editor Modal (admin only) ── */}
            {
                showExpiryModal && (
                    <div className="ui-modal-overlay"
                        onClick={() => setShowExpiryModal(false)}>
                        <div className="agent-detail-modal"
                            onClick={e => e.stopPropagation()}>
                            <div className="agent-detail-modal-header">
                                <h3 className="agent-detail-modal-title">⏰ {t('agent.settings.expiry.title')}</h3>
                                <button onClick={() => setShowExpiryModal(false)} className="agent-detail-modal-close">×</button>
                            </div>
                            <div className="agent-detail-expiry-status">
                                {(agent as any).is_expired
                                    ? <span className="agent-detail-expiry-status-expired">⏰ {t('agent.settings.expiry.expired')}</span>
                                    : (agent as any).expires_at
                                        ? <>{t('agent.settings.expiry.currentExpiry')} <strong>{new Date((agent as any).expires_at).toLocaleString(i18n.language === 'zh' ? 'zh-CN' : 'en-US')}</strong></>
                                        : <span className="agent-detail-expiry-status-never">{t('agent.settings.expiry.neverExpires')}</span>
                                }
                            </div>
                            <div className="agent-detail-expiry-field">
                                <div className="agent-detail-expiry-hint">{t('agent.settings.expiry.quickRenew')}</div>
                                <div className="agent-detail-expiry-quick">
                                    {([
                                        ['+ 24h', 24],
                                        [`+ ${t('agent.settings.expiry.days', { count: 7 })}`, 168],
                                        [`+ ${t('agent.settings.expiry.days', { count: 30 })}`, 720],
                                        [`+ ${t('agent.settings.expiry.days', { count: 90 })}`, 2160],
                                    ] as [string, number][]).map(([label, h]) => (
                                        <button key={h} onClick={() => addHours(h)}
                                            className="agent-detail-expiry-quick-btn">
                                            {label}
                                        </button>
                                    ))}
                                </div>
                            </div>
                            <div className="agent-detail-expiry-field-lg">
                                <div className="agent-detail-expiry-hint">{t('agent.settings.expiry.customDeadline')}</div>
                                <input type="datetime-local" value={expiryValue} onChange={e => setExpiryValue(e.target.value)}
                                    className="agent-detail-expiry-input" />
                            </div>
                            <div className="agent-detail-expiry-footer">
                                <button onClick={() => saveExpiry(true)} disabled={expirySaving}
                                    className="agent-detail-expiry-btn agent-detail-expiry-btn-reset">
                                    🔓 {t('agent.settings.expiry.neverExpires')}
                                </button>
                                <div className="agent-detail-expiry-actions">
                                    <button onClick={() => setShowExpiryModal(false)} disabled={expirySaving}
                                        className="agent-detail-expiry-btn agent-detail-expiry-btn-cancel">
                                        {t('common.cancel')}
                                    </button>
                                    <button onClick={() => saveExpiry(false)} disabled={expirySaving || !expiryValue}
                                        className="btn btn-primary"
                                        style={{ opacity: !expiryValue ? 0.5 : 1 }}>
                                        {expirySaving ? t('agent.settings.expiry.saving') : t('common.save')}
                                    </button>
                                </div>
                            </div>
                        </div>
                    </div>
                )
            }

        </>
    );
}
export default function AgentDetailWithErrorBoundary() {
    return (
        <AgentDetailErrorBoundary>
            <AgentDetailInner />
        </AgentDetailErrorBoundary>
    );
}
