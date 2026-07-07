import React, { useState, useEffect, useRef, Component, ErrorInfo } from 'react';
import { useParams, useLocation, useNavigate } from 'react-router-dom';
import { useQuery, useQueryClient } from '@tanstack/react-query';
import { useTranslation } from 'react-i18next';

import ConfirmModal from '../components/ConfirmModal';
import FileBrowser, { buildNewSkillFilePath, type FileBrowserApi } from '../components/FileBrowser';
import PromptModal from '../components/PromptModal';
import AgentApprovalsSection from './agent-detail/AgentApprovalsSection';
import AgentWorkflowsSection from './agent-detail/AgentWorkflowsSection';
import AgentActivityLogSection from './agent-detail/AgentActivityLogSection';
import AgentAwareSection from './agent-detail/AgentAwareSection';
import AgentChatSection, {
    type BranchLineageItem,
    type SessionCommandCheckpoint,
    type SessionCommandControlState,
    type SessionPermissionMode,
} from './agent-detail/AgentChatSection';
import AgentEvolutionSection from './agent-detail/AgentEvolutionSection';
import AgentExtensionsSection from './agent-detail/AgentExtensionsSection';
import AgentKnowledgeSection from './agent-detail/AgentKnowledgeSection';
import OfficeWorkbenchSection from './agent-detail/OfficeWorkbenchSection';
import AgentSettingsSection from './agent-detail/AgentSettingsSection';
import AgentStatusSection from './agent-detail/AgentStatusSection';
import AgentWorkspaceSection from './agent-detail/AgentWorkspaceSection';
import {
    CHAT_SOCKET_KEEPALIVE_INTERVAL_MS,
    ACTIVE_RUN_ABSENCE_GRACE_MS,
    applySessionActiveRunObservedState,
    applySessionActiveRunState,
    applyTranscriptEvent,
    appendToolCallMessage,
    applyRuntimeDoneEvent,
    applyStreamingChunkEvent,
    buildChatSocketKeepaliveMessage,
    buildRuntimeSummary,
    buildSessionTranscriptLoadFailureMessage,
    createEmptyTranscriptReplayState,
    extractArtifactParts,
    getRuntimeEventMessage,
    getTerminalRunIdFromTranscriptEvent,
    getTransportNotice,
    isDraftHumanChatSession,
    isTerminalRealtimeChatEvent,
    mergePendingUserMessages,
    filterSessionsForAgent,
    normalizeRuntimeEventMessage,
    normalizeStoredChatMessage,
    replayTranscriptEvents,
    sessionBelongsToAgent,
    shouldPreserveActiveSessionForRequestedId,
    shouldUseWritableSessionSurface,
    shouldIgnoreObservedActiveRun,
    shouldClearStaleRuntimeState,
    shouldReuseSessionTranscriptLoad,
    type AgentChatMessage,
    type ChatTranscriptEventPayload,
    type ChatRuntimeSummary,
    type PendingUserMessage,
    type SessionTranscriptLoadDescriptor,
    type SessionPermissionRequest,
    type SessionRunState,
    type SessionUiState,
    type TranscriptReplayState,
} from './agent-detail/chatRuntime';
import { buildPlanModeScopeKey, nextPlanModeRequestedForScope } from './agent-detail/planModeComposer';
import {
    createSessionMessageStore,
    useSessionMessages,
    type ChatMessagesUpdater,
} from './agent-detail/sessionMessageStore';
import AgentA2ASection from './agent-detail/AgentA2ASection';
import { normalizeToolCallResult } from './agent-detail/toolResultEnvelope';
import { agentApi, type AgentCapabilityInstall, type AgentChannelCapability } from '../api/domains/agents';
import { activityApi } from '../api/domains/activity';
import { enterpriseApi } from '../api/domains/enterprise';
import { fileApi } from '../api/domains/files';
import { triggerApi } from '../api/domains/triggers';
import { autonomyApi } from '../api/domains/autonomy';
import { chatApi, type ChatSession, type ConversationBranchMode, type SessionRun, type StartSessionRunInput } from '../api/domains/chat';
import { ccParityApi } from '../api/domains/ccParity';
import { uploadFileWithProgress } from '../api/core/upload-progress';
import { useAuthStore } from '../stores';
import { parseSlashCommandInput } from './agent-detail/slashCommand';
import {
    commandResultRecord,
    formatSlashCommandResult,
    getSessionCommandUiAction,
} from './agent-detail/sessionCommandResult';
import LocalAgents from './LocalAgents';
import LocalAgentChatSection from './agent-detail/LocalAgentChatSection';
import './AgentDetail.css';

// P8 IA (docs/agent-memory-md-first-spec.md §10): Knowledge remains the
// primary memory view. External capability governance is exposed through a
// single agent-level Extensions tab, while MCP/tools, Skills, and Sub-agents
// keep their existing runtime modules behind that entry.
export const AGENT_DETAIL_TABS = ['status', 'aware', 'knowledge', 'evolution', 'extensions', 'a2a', 'workspace', 'workflows', 'office', 'chat', 'activityLog', 'approvals', 'settings'] as const;
type AgentDetailTab = typeof AGENT_DETAIL_TABS[number];

export function isSessionWorkbenchRoute(activeTab: string, search: string): boolean {
    const params = new URLSearchParams(search);
    return activeTab === 'chat' && params.has('session_id') && !params.has('manage');
}

export function getAgentDetailHashTab(hash: string | undefined, validTabs: readonly string[]): string | null {
    const rawHashTab = hash?.replace('#', '');
    const legacyHashTabMap: Record<string, string> = {
        mind: 'knowledge',
        tools: 'extensions',
        skills: 'extensions',
        subagents: 'extensions',
    };
    const hashTab = rawHashTab ? (legacyHashTabMap[rawHashTab] ?? rawHashTab) : rawHashTab;
    return hashTab && validTabs.includes(hashTab) ? hashTab : null;
}

function normalizeSessionPermissionModeValue(value: unknown): SessionPermissionMode | null {
    if (value === 'default' || value === 'auto' || value === 'bypassPermissions') {
        return value;
    }
    return null;
}

const DEFAULT_SESSION_PERMISSION_MODE: SessionPermissionMode = 'bypassPermissions';

// B4 transcript windowing: first screen loads a slim newest window; older
// history pages in on demand.
const TRANSCRIPT_INITIAL_WINDOW = 25;
const TRANSCRIPT_OLDER_PAGE = 50;

function objectValue(value: unknown): Record<string, unknown> {
    return value && typeof value === 'object' ? value as Record<string, unknown> : {};
}

function stringValueFromUnknown(value: unknown): string {
    return typeof value === 'string' ? value.trim() : '';
}

function draftContentFromCheckpointPayload(actionResult: Record<string, unknown> | null, uiAction?: Record<string, unknown>): string {
    const checkpoint = objectValue(actionResult?.checkpoint);
    return (
        stringValueFromUnknown(uiAction?.draft_content)
        || stringValueFromUnknown(actionResult?.draft_content)
        || stringValueFromUnknown(checkpoint.content)
    );
}

function checkpointEventIdFromPayload(actionResult: Record<string, unknown> | null, uiAction?: Record<string, unknown>): string {
    const checkpoint = objectValue(actionResult?.checkpoint);
    return (
        stringValueFromUnknown(uiAction?.checkpoint_event_id)
        || stringValueFromUnknown(actionResult?.checkpoint_event_id)
        || stringValueFromUnknown(checkpoint.checkpoint_event_id)
        || stringValueFromUnknown(checkpoint.id)
        || stringValueFromUnknown(checkpoint.event_id)
    );
}

export function trimMessagesBeforeTranscriptEvent(
    messages: AgentChatMessage[],
    checkpointEventId: string,
    checkpointCreatedAt?: string,
): AgentChatMessage[] {
    if (!checkpointEventId) return messages;
    const index = messages.findIndex((message) => message.transcriptEventId === checkpointEventId);
    if (index >= 0) return messages.slice(0, index);
    // Live-appended messages may not carry transcript event ids yet; fall back
    // to the checkpoint timestamp so a rewind never silently does nothing.
    const anchorTime = checkpointCreatedAt ? Date.parse(checkpointCreatedAt) : NaN;
    if (!Number.isNaN(anchorTime)) {
        const timeIndex = messages.findIndex((message) => {
            const stamp = message.timestamp ? Date.parse(message.timestamp) : NaN;
            return !Number.isNaN(stamp) && stamp >= anchorTime;
        });
        if (timeIndex >= 0) return messages.slice(0, timeIndex);
    }
    return messages;
}

function rewindMarkerMessage(checkpointEventId: string): AgentChatMessage {
    return {
        id: `rewind-marker-${checkpointEventId}-${Date.now()}`,
        role: 'event',
        content: '⏪ Rewound — messages after this checkpoint left the active context (history is preserved).',
        timestamp: new Date().toISOString(),
        eventStatus: 'session_rewind_marker',
    } as AgentChatMessage;
}

export function applySessionActiveProjection(
    session: unknown,
    messages: AgentChatMessage[],
): { messages: AgentChatMessage[]; draftContent: string; checkpointEventId: string; shouldScrollToProjectionTail: boolean } {
    const sessionRecord = objectValue(session);
    const transcriptMetadata = objectValue(sessionRecord.transcript_metadata_json);
    const metadata = objectValue(sessionRecord.metadata);
    const activeProjection = objectValue(transcriptMetadata.active_projection || metadata.active_projection);
    if (stringValueFromUnknown(activeProjection.projection_reason) !== 'rewind') {
        return { messages, draftContent: '', checkpointEventId: '', shouldScrollToProjectionTail: false };
    }
    const checkpointEventId = stringValueFromUnknown(activeProjection.checkpoint_event_id);
    return {
        messages: trimMessagesBeforeTranscriptEvent(messages, checkpointEventId),
        draftContent: stringValueFromUnknown(activeProjection.draft_content),
        checkpointEventId,
        shouldScrollToProjectionTail: Boolean(checkpointEventId),
    };
}

function branchDraftContent(response: { branch?: Record<string, unknown> } | null | undefined, fallback: string): string {
    return stringValueFromUnknown(response?.branch?.draft_content) || fallback;
}

export function sessionPermissionModeFromSession(session: unknown): SessionPermissionMode {
    const sessionRecord = objectValue(session);
    const profile = objectValue(sessionRecord.permission_profile);
    const transcriptMetadata = objectValue(sessionRecord.transcript_metadata_json);
    const metadata = objectValue(sessionRecord.metadata);
    return (
        normalizeSessionPermissionModeValue(sessionRecord.permission_mode) ||
        normalizeSessionPermissionModeValue(profile.mode) ||
        normalizeSessionPermissionModeValue(transcriptMetadata.permission_mode) ||
        normalizeSessionPermissionModeValue(metadata.permission_mode) ||
        DEFAULT_SESSION_PERMISSION_MODE
    );
}

function withSessionPermissionMode<T extends Record<string, unknown>>(session: T, mode: SessionPermissionMode): T {
    const existingProfile = objectValue(session.permission_profile);
    const writableRoots = Array.isArray(session.writable_roots)
        ? session.writable_roots
        : Array.isArray(existingProfile.writable_roots)
            ? existingProfile.writable_roots
            : ['workspace/'];
    return {
        ...session,
        permission_mode: mode,
        writable_roots: writableRoots,
        permission_profile: {
            ...existingProfile,
            mode,
            writable_roots: writableRoots,
        },
    };
}

export function buildAgentDetailTabNavigation(
    pathname: string,
    search: string,
    tab: string,
    options?: { detailChat?: boolean },
): { pathname: string; search: string; hash: string } {
    const params = new URLSearchParams(search);
    if (tab === 'chat' && options?.detailChat) {
        params.set('manage', 'true');
    }
    return {
        pathname,
        search: params.toString() ? `?${params.toString()}` : '',
        hash: `#${tab}`,
    };
}

export function buildSessionWorkbenchNavigation(
    pathname: string,
    search: string,
    sessionId: string,
): { pathname: string; search: string; hash: string } {
    // Canonical Active Session Workbench route (§8.4): /agents/:id/sessions/:sessionId.
    // The legacy ?session_id=…#chat disguise redirects here on mount.
    const params = new URLSearchParams(search);
    params.delete('session_id');
    params.delete('manage');
    const agentId = pathname.split('/agents/')[1]?.split('/')[0] ?? '';
    return {
        pathname: agentId ? `/agents/${agentId}/sessions/${sessionId}` : pathname,
        search: params.toString() ? `?${params.toString()}` : '',
        hash: '',
    };
}

/** Visual grouping of tabs for the tab bar — groups are separated by thin dividers */
export const AGENT_DETAIL_TAB_GROUPS: { tabs: AgentDetailTab[]; }[] = [
    { tabs: ['status', 'chat'] },
    { tabs: ['aware', 'knowledge', 'evolution', 'extensions'] },
    { tabs: ['workspace', 'workflows', 'office', 'a2a', 'activityLog', 'approvals'] },
    { tabs: ['settings'] },
];

const AGENT_TAB_LABELS: Record<AgentDetailTab, string> = {
    status: 'Status',
    aware: 'Awareness',
    knowledge: 'Knowledge',
    evolution: 'Evolution',
    extensions: 'Extensions',
    a2a: 'A2A',
    workspace: 'Workspace',
    workflows: 'Workflows',
    office: 'Office',
    chat: 'Chat',
    activityLog: 'Activity',
    approvals: 'Approvals',
    settings: 'Settings',
};

export const AGENT_WORKBENCH_AREAS: Array<{
    id: string;
    labelKey: string;
    fallback: string;
    primaryTab: AgentDetailTab;
    tabs: AgentDetailTab[];
}> = [
    {
        id: 'overview',
        labelKey: 'agent.workbench.overview',
        fallback: 'Overview',
        primaryTab: 'status',
        tabs: ['status', 'activityLog'],
    },
    {
        id: 'conversation',
        labelKey: 'agent.workbench.conversation',
        fallback: 'Conversation & Tasks',
        primaryTab: 'chat',
        tabs: ['chat', 'aware'],
    },
    {
        id: 'capabilities',
        labelKey: 'agent.workbench.capabilities',
        fallback: 'Capabilities',
        primaryTab: 'extensions',
        tabs: ['extensions', 'workflows'],
    },
    {
        id: 'memory',
        labelKey: 'agent.workbench.memory',
        fallback: 'Memory & Knowledge',
        primaryTab: 'knowledge',
        tabs: ['knowledge', 'evolution'],
    },
    {
        id: 'team',
        labelKey: 'agent.workbench.team',
        fallback: 'A2A / Team',
        primaryTab: 'a2a',
        tabs: ['a2a'],
    },
    {
        id: 'documents',
        labelKey: 'agent.workbench.documents',
        fallback: 'Documents & Workspace',
        primaryTab: 'workspace',
        tabs: ['workspace', 'office'],
    },
    {
        id: 'permissions',
        labelKey: 'agent.workbench.permissions',
        fallback: 'Permissions & Settings',
        primaryTab: 'approvals',
        tabs: ['approvals', 'settings'],
    },
];

function isAgentDetailTabVisible(agent: any, tab: AgentDetailTab): boolean {
    if (agent?.access_level === 'use' && (tab === 'settings' || tab === 'approvals')) return false;
    if (agent?.agent_type === 'local_agent') {
        return ['chat', 'workspace', 'settings'].includes(tab);
    }
    return true;
}

export function isLocalAgentRuntimeType(agent: any): boolean {
    const agentType = typeof agent === 'string' ? agent : agent?.agent_type;
    return agentType === 'local_agent';
}

export function getVisibleAgentDetailTabs(agent: any): AgentDetailTab[] {
    if (agent?.agent_type === 'local_agent') {
        return ['chat', 'workspace', 'settings'];
    }
    return AGENT_DETAIL_TABS.filter((tab) => isAgentDetailTabVisible(agent, tab));
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
            const all = await chatApi.listSessions(id!, 'all').catch(() => [] as any[]);
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

    const workspacePath = 'workspace';

    const { data: activityLogs = [] } = useQuery({
        queryKey: ['activity', id],
        queryFn: () => activityApi.list(id!, 100),
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
        queryKey: ['activity', 'tool-failures', id],
        queryFn: () => activityApi.getToolFailureSummary(id!, 24, 200),
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
    const [historyMessagesSessionId, setHistoryMessagesSessionId] = useState<string | null>(null);
    const [sessionsLoading, setSessionsLoading] = useState(false);
    const [allSessionsLoading, setAllSessionsLoading] = useState(false);
    const [agentExpired, setAgentExpired] = useState(false);
    // Websocket chat state (for 'me' conversation)
    type SessionRuntimeKey = string;
    const wsMapRef = useRef<Record<SessionRuntimeKey, WebSocket>>({});
    const reconnectTimerRef = useRef<Record<SessionRuntimeKey, ReturnType<typeof setTimeout> | null>>({});
    const keepaliveTimerRef = useRef<Record<SessionRuntimeKey, ReturnType<typeof setInterval> | null>>({});
    const reconnectDisabledRef = useRef<Record<SessionRuntimeKey, boolean>>({});
    const reconnectAttemptsRef = useRef<Record<SessionRuntimeKey, number>>({});
    const sessionUiStateRef = useRef<Record<SessionRuntimeKey, SessionUiState>>({});
    const transcriptReplayStateRef = useRef<Record<SessionRuntimeKey, TranscriptReplayState>>({});
    const transcriptEventsRef = useRef<Record<SessionRuntimeKey, ChatTranscriptEventPayload[]>>({});
    const toolCallInvalidateAtRef = useRef<Record<SessionRuntimeKey, number>>({});
    const [transcriptHasOlder, setTranscriptHasOlder] = useState<Record<SessionRuntimeKey, boolean>>({});
    const [olderMessagesLoading, setOlderMessagesLoading] = useState(false);
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

    const buildSessionRuntimeKey = (agentId: string, sessionId: string) => `${agentId}:${sessionId}`;
    const isLiveRun = (run?: SessionRun | null) => !!run && ['pending', 'running'].includes(String(run.status || '').toLowerCase());

    const invalidateSessionRuntimeQueries = (agentId: string, sessionId: string, includeActiveRun = true) => {
        if (includeActiveRun) void queryClient.invalidateQueries({ queryKey: ['chat-active-run', agentId, sessionId] });
        void queryClient.invalidateQueries({ queryKey: ['chat-runtime-summary', agentId, sessionId] });
        void queryClient.invalidateQueries({ queryKey: ['chat-session-work-ledger', agentId, sessionId] });
    };

    const setActiveRunState = (key: SessionRuntimeKey, run: SessionRunState | null) => {
        if (run) {
            runtimeActivityAtRef.current[key] = Date.now();
            locallyTerminalSessionKeysRef.current.delete(key);
            if (run.runId) locallyTerminalRunIdsRef.current.delete(String(run.runId));
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

    const markActiveRunTerminal = (key: SessionRuntimeKey, terminalRunId?: string | null) => {
        locallyTerminalSessionKeysRef.current.add(key);
        const runId = terminalRunId || activeRunStateRef.current[key]?.runId;
        if (runId) locallyTerminalRunIdsRef.current.add(String(runId));
        setActiveRunState(key, null);
    };

    const clearReconnectTimer = (key: SessionRuntimeKey) => {
        const timer = reconnectTimerRef.current[key];
        if (timer) {
            clearTimeout(timer);
            reconnectTimerRef.current[key] = null;
        }
    };

    const clearKeepaliveTimer = (key: SessionRuntimeKey) => {
        const timer = keepaliveTimerRef.current[key];
        if (timer) {
            clearInterval(timer);
            keepaliveTimerRef.current[key] = null;
        }
    };

    const startKeepaliveTimer = (key: SessionRuntimeKey, ws: WebSocket) => {
        clearKeepaliveTimer(key);
        keepaliveTimerRef.current[key] = setInterval(() => {
            if (wsMapRef.current[key] !== ws || ws.readyState !== WebSocket.OPEN) {
                clearKeepaliveTimer(key);
                return;
            }
            const uiState = sessionUiStateRef.current[key];
            const hasActiveRun = !!activeRunStateRef.current[key] || !!uiState?.isWaiting || !!uiState?.isStreaming;
            if (!hasActiveRun) return;
            ws.send(JSON.stringify(buildChatSocketKeepaliveMessage()));
        }, CHAT_SOCKET_KEEPALIVE_INTERVAL_MS);
    };

    const closeSessionSocket = (key: SessionRuntimeKey, disableReconnect = true) => {
        if (disableReconnect) reconnectDisabledRef.current[key] = true;
        clearReconnectTimer(key);
        clearKeepaliveTimer(key);
        delete reconnectAttemptsRef.current[key];
        const ws = wsMapRef.current[key];
        if (ws && ws.readyState !== WebSocket.CLOSED) ws.close();
        delete wsMapRef.current[key];
        delete sessionUiStateRef.current[key];
        delete pendingUserMessagesRef.current[key];
        delete runtimeActivityAtRef.current[key];
    };

    const setSessionUiState = (key: SessionRuntimeKey, next: Partial<{ isWaiting: boolean; isStreaming: boolean }>) => {
        const prev = sessionUiStateRef.current[key] || { isWaiting: false, isStreaming: false };
        sessionUiStateRef.current[key] = { ...prev, ...next };
        if (next.isWaiting || next.isStreaming) runtimeActivityAtRef.current[key] = Date.now();
    };

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
        key: SessionRuntimeKey,
        messageInput: AgentChatMessage,
    ) => {
        const message = parseChatMsg(messageInput);
        setChatMessagesAfterQueued(prev => {
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
    ) => {
        const key = buildSessionRuntimeKey(agentId, sessionId);
        const previous = transcriptReplayStateRef.current[key] || createEmptyTranscriptReplayState();
        const next = applyTranscriptEvent(previous, event);
        transcriptReplayStateRef.current[key] = next;
        sessionUiStateRef.current[key] = next.ui;
        runtimeActivityAtRef.current[key] = Date.now();

        const eventType = event.event_type || event.type;
        const lastMessage = next.messages[next.messages.length - 1];
        const terminal =
            eventType === 'assistant_message'
            || eventType === 'run_completed'
            || eventType === 'done'
            || eventType === 'error'
            || eventType === 'quota_exceeded'
            || isTerminalTranscriptToolMessage(lastMessage);
        if (terminal) {
            markActiveRunTerminal(key, getTerminalRunIdFromTranscriptEvent(event));
        }

        if (isActiveRuntime) {
            setChatMessagesSessionId(sessionId);
            const commitChatMessages = terminal ? setChatMessagesAfterQueued : enqueueChatMessagesUpdate;
            commitChatMessages(() => mergePendingForSession(key, next.messages.map(parseChatMsg)));
            setIsWaiting(next.ui.isWaiting);
            setIsStreaming(next.ui.isStreaming);
        }
    };

    const isWritableSession = (sess: any) => {
        if (!sess) return false;
        return shouldUseWritableSessionSurface(sess, currentUser?.id);
    };

    const syncActiveSocketState = (sess: any | null = activeSession, agentId: string | undefined = id) => {
        if (!sess || !agentId || isDraftHumanChatSession(sess)) {
            wsRef.current = null;
            setWsConnected(false);
            return;
        }
        const key = buildSessionRuntimeKey(agentId, sess.id);
        const ws = wsMapRef.current[key];
        wsRef.current = ws ?? null;
        setWsConnected(!!ws && ws.readyState === WebSocket.OPEN);
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
            const all = filterSessionsForAgent(await chatApi.listSessions(id, 'all'), id);
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
        const runtimeState = sessionUiStateRef.current[runtimeKey] || { isWaiting: false, isStreaming: false };
        const activeRunState = activeRunStateRef.current[runtimeKey];
        const writableSession = isWritableSession(sess);
        const draftSession = isDraftHumanChatSession(sess);
        const transcriptSurface = writableSession ? 'chat' : 'history';
        const nextTranscriptLoad: SessionTranscriptLoadDescriptor = {
            key: runtimeKey,
            surface: transcriptSurface,
        };
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
        setIsStreaming(runtimeState.isStreaming);
        setIsWaiting(runtimeState.isWaiting || !!activeRunState);
        setSessionPermissionMode(sessionPermissionModeFromSession(sess));
        setActiveSession(sess);
        setAgentExpired(false);
        syncActiveSocketState(sess, targetAgentId);

        if (draftSession) {
            sessionMsgAbortRef.current?.abort();
            setChatMessagesAfterQueued(() => []);
            setChatMessagesSessionId(sessionId);
            setHistoryMsgs([]);
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
        try {
            // First screen loads the newest window; older history pages in on demand (plan B4).
            const transcriptEvents = await chatApi.getSessionTranscript(targetAgentId, sessionId, {
                direction: 'backward',
                limit: TRANSCRIPT_INITIAL_WINDOW,
                signal: controller.signal,
            });
            if (controller.signal.aborted || loadSeq !== sessionLoadSeqRef.current) return;
            if (currentAgentIdRef.current !== targetAgentId) return;
            if (activeSessionIdRef.current !== sessionId) return;
            let preParsed: AgentChatMessage[];
            if (transcriptEvents.length > 0) {
                transcriptEventsRef.current[runtimeKey] = transcriptEvents as ChatTranscriptEventPayload[];
                setTranscriptHasOlder((prev) => ({
                    ...prev,
                    [runtimeKey]: transcriptEvents.length >= TRANSCRIPT_INITIAL_WINDOW,
                }));
                const replay = replayTranscriptEvents(transcriptEvents as ChatTranscriptEventPayload[]);
                transcriptReplayStateRef.current[runtimeKey] = replay;
                sessionUiStateRef.current[runtimeKey] = replay.ui;
                preParsed = replay.messages.map(parseChatMsg);
            } else {
                const msgs = await chatApi.getSessionMessages(targetAgentId, sessionId, { signal: controller.signal });
                if (controller.signal.aborted || loadSeq !== sessionLoadSeqRef.current) return;
                preParsed = msgs.map((m: any) => parseChatMsg(normalizeStoredChatMessage(m)));
                transcriptReplayStateRef.current[runtimeKey] = {
                    ...createEmptyTranscriptReplayState(),
                    messages: preParsed,
                };
            }
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
                if (activeProjection.draftContent) {
                    setChatInput(activeProjection.draftContent);
                }
                if (activeProjection.shouldScrollToProjectionTail) {
                    setProjectionTailScrollNonce(prev => prev + 1);
                }
            }
            
            if (writableSession) {
                setChatMessagesSessionId(sessionId);
                setChatMessagesAfterQueued(() => mergePendingForSession(runtimeKey, preParsed));
            } else {
                setHistoryMessagesSessionId(sessionId);
                setHistoryMsgs(preParsed);
            }
        } catch (err: any) {
            if (err?.name === 'AbortError') return;
            if (loadSeq !== sessionLoadSeqRef.current) return;
            if (currentAgentIdRef.current !== targetAgentId) return;
            if (activeSessionIdRef.current !== sessionId) return;
            console.error('Failed to load session messages:', err);
            const failureMessage = buildSessionTranscriptLoadFailureMessage(
                t(
                    'agent.chat.sessionLoadFailed',
                    'Conversation failed to load. Please retry this session or refresh the page.',
                ),
            );
            setTranscriptHasOlder((prev) => ({ ...prev, [runtimeKey]: false }));
            if (writableSession) {
                setChatMessagesSessionId(sessionId);
                setChatMessagesAfterQueued(() => mergePendingForSession(runtimeKey, [failureMessage]));
            } else {
                setHistoryMessagesSessionId(sessionId);
                setHistoryMsgs([failureMessage]);
            }
        } finally {
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

    const loadOlderMessages = async () => {
        const targetAgentId = currentAgentIdRef.current;
        const sessionId = activeSessionIdRef.current;
        if (!targetAgentId || !sessionId || olderMessagesLoading) return;
        const runtimeKey = buildSessionRuntimeKey(targetAgentId, sessionId);
        const existing = transcriptEventsRef.current[runtimeKey] || [];
        const earliestSequence = existing.length > 0 ? Number(existing[0]?.sequence ?? 0) : 0;
        if (!earliestSequence || earliestSequence <= 1) {
            setTranscriptHasOlder((prev) => ({ ...prev, [runtimeKey]: false }));
            return;
        }
        setOlderMessagesLoading(true);
        try {
            const older = await chatApi.getSessionTranscript(targetAgentId, sessionId, {
                beforeSequence: earliestSequence,
                limit: TRANSCRIPT_OLDER_PAGE,
            });
            if (activeSessionIdRef.current !== sessionId) return;
            setTranscriptHasOlder((prev) => ({ ...prev, [runtimeKey]: older.length >= TRANSCRIPT_OLDER_PAGE }));
            if (older.length === 0) return;
            const merged = [...(older as ChatTranscriptEventPayload[]), ...existing];
            transcriptEventsRef.current[runtimeKey] = merged;
            const replay = replayTranscriptEvents(merged);
            transcriptReplayStateRef.current[runtimeKey] = replay;
            sessionUiStateRef.current[runtimeKey] = replay.ui;
            const preParsed = replay.messages.map(parseChatMsg);
            if (chatMessagesSessionId === sessionId) {
                setChatMessagesAfterQueued(() => mergePendingForSession(runtimeKey, preParsed));
            } else if (historyMessagesSessionId === sessionId) {
                setHistoryMsgs(preParsed);
            }
        } catch (err) {
            console.warn('Failed to load older transcript window:', err);
        } finally {
            setOlderMessagesLoading(false);
        }
    };

    const fetchBranchLineage = async (agentId: string | undefined = id, sessionId: string | undefined = activeSession?.id ? String(activeSession.id) : undefined) => {
        if (!agentId || !sessionId) {
            setBranchLineage([]);
            return;
        }
        setBranchLineageLoading(true);
        try {
            const lineage = await chatApi.getSessionLineage(agentId, sessionId);
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
    };

    const normalizeSessionCommandCheckpoints = (value: unknown): SessionCommandCheckpoint[] => {
        if (!Array.isArray(value)) return [];
        return value
            .filter((item): item is Record<string, unknown> => item != null && typeof item === 'object' && !Array.isArray(item))
            .map((item) => ({
                checkpoint_event_id: typeof item.checkpoint_event_id === 'string' ? item.checkpoint_event_id : null,
                event_id: typeof item.event_id === 'string' ? item.event_id : null,
                sequence: typeof item.sequence === 'number' || typeof item.sequence === 'string' ? item.sequence : null,
                turn_index: typeof item.turn_index === 'number' || typeof item.turn_index === 'string' ? item.turn_index : null,
                role: typeof item.role === 'string' ? item.role : null,
                content: typeof item.content === 'string' ? item.content : null,
                created_at: typeof item.created_at === 'string' ? item.created_at : null,
            }));
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
                title: message || 'Select checkpoint',
                message: checkpoints.length > 0
                    ? 'Select a checkpoint to rewind this session context.'
                    : 'No checkpoints are available for this session.',
                command: response.command,
                checkpoints,
                payload: actionResult,
            });
            invalidateSessionRuntimeQueries(id, currentSessionId);
            showToast(message, uiAction.level === 'error' ? 'error' : 'success');
            return true;
        }

        if (uiAction.type === 'open_resume_picker') {
            openSessionCommandControl({
                type: 'resume_picker',
                title: message || 'Resume session',
                message: 'Session resume status is ready.',
                command: response.command,
                level: uiAction.level === 'error' ? 'error' : 'info',
                payload: actionResult,
            });
            invalidateSessionRuntimeQueries(id, currentSessionId);
            showToast(message, uiAction.level === 'error' ? 'error' : 'success');
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
                    const replay = transcriptReplayStateRef.current[activeKey];
                    if (replay) {
                        transcriptReplayStateRef.current[activeKey] = {
                            ...replay,
                            messages: trimMessagesBeforeTranscriptEvent(replay.messages, checkpointEventId, checkpointCreatedAt),
                        };
                    }
                    const marker = rewindMarkerMessage(checkpointEventId);
                    setChatMessagesAfterQueued(prev => (
                        chatMessagesSessionId === currentSessionId
                            ? [...trimMessagesBeforeTranscriptEvent(prev, checkpointEventId, checkpointCreatedAt), marker]
                            : prev
                    ));
                    setHistoryMsgs(prev => (
                        historyMessagesSessionId === currentSessionId
                            ? trimMessagesBeforeTranscriptEvent(prev, checkpointEventId, checkpointCreatedAt)
                            : prev
                    ));
                    setProjectionTailScrollNonce(prev => prev + 1);
                }
            }
            openSessionCommandControl({
                type: 'projection_status',
                title: message || (uiAction.type === 'install_compacted_context' ? 'Context compacted' : workspaceRestore ? 'Workspace restored' : 'Session projection installed'),
                message: uiAction.type === 'install_compacted_context'
                    ? 'Future turns in this session will use the compacted context projection.'
                    : workspaceRestore
                        ? 'Workspace files were restored from the selected checkpoint.'
                        : 'Future turns in this session will use the active rewind projection.',
                command: response.command,
                level: uiAction.level === 'error' ? 'error' : 'success',
                payload: actionResult,
            });
            invalidateSessionRuntimeQueries(id, currentSessionId);
            showToast(message, uiAction.level === 'error' ? 'error' : 'success');
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

    const createDraftChatSession = () => {
        const now = new Date();
        const pad = (value: number) => String(value).padStart(2, '0');
        const randomId =
            typeof globalThis.crypto?.randomUUID === 'function'
                ? globalThis.crypto.randomUUID()
                : `${now.getTime()}-${Math.random().toString(36).slice(2)}`;
        const draftId = `draft:${randomId}`;
        return {
            id: draftId,
            draft_client_id: draftId,
            is_draft: true,
            agent_id: id,
            user_id: currentUser?.id ? String(currentUser.id) : undefined,
            title: `Session ${pad(now.getMonth() + 1)}-${pad(now.getDate())} ${pad(now.getHours())}:${pad(now.getMinutes())}`,
            created_at: now.toISOString(),
            updated_at: now.toISOString(),
            source_channel: 'web',
            session_kind: 'human_chat',
            actor_type: 'user',
            runtime_source: 'web_chat',
            visibility_scope: 'direct_user',
            listed_surface: 'chat',
            is_current_user_session: true,
            read_only: false,
            message_count: 0,
        };
    };

    const createNewSession = async () => {
        if (!id) return;
        const draft = createDraftChatSession();
        const params = new URLSearchParams(location.search);
        params.delete('session_id');
        params.delete('manage');
        navigate(
            {
                pathname: location.pathname,
                search: params.toString() ? `?${params.toString()}` : '',
                hash: '#chat',
            },
            { replace: true },
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
                setHistoryMsgs([]);
                setHistoryMessagesSessionId(null);
                setTransportNotice(null);
                setWsConnected(false);
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
    const resolveCurrentChatMessagesSessionId = React.useCallback(() => (
        activeSessionIdRef.current
        || chatMessagesSessionId
        || (activeSession?.id ? String(activeSession.id) : null)
    ), [activeSession?.id, chatMessagesSessionId]);
    const enqueueChatMessagesUpdate = React.useCallback((update: ChatMessagesUpdater) => {
        sessionMessageStore.enqueueUpdate(resolveCurrentChatMessagesSessionId(), update);
    }, [resolveCurrentChatMessagesSessionId, sessionMessageStore]);
    const setChatMessagesAfterQueued = React.useCallback((update: ChatMessagesUpdater) => {
        sessionMessageStore.updateAfterQueued(resolveCurrentChatMessagesSessionId(), update);
    }, [resolveCurrentChatMessagesSessionId, sessionMessageStore]);
    useEffect(() => () => {
        sessionMessageStore.clearAll();
    }, [sessionMessageStore]);
    const [chatInput, setChatInput] = useState('');
    const [planModeRequested, setPlanModeRequested] = useState(false);
    const [sessionPermissionMode, setSessionPermissionMode] = useState<SessionPermissionMode>(DEFAULT_SESSION_PERMISSION_MODE);
    const [sessionCommandControl, setSessionCommandControl] = useState<SessionCommandControlState | null>(null);
    const [projectionTailScrollNonce, setProjectionTailScrollNonce] = useState(0);
    const handleSetSessionPermissionMode = async (mode: SessionPermissionMode) => {
        const previous = sessionPermissionMode;
        setSessionPermissionMode(mode);
        if (!id || !activeSession?.id) return;
        const sessionId = String(activeSession.id);
        try {
            await chatApi.updateSessionPermissionProfile(id, sessionId, { permission_mode: mode });
            setActiveSession((current: any | null) =>
                current && String(current.id) === sessionId ? withSessionPermissionMode(current, mode) : current,
            );
            setSessions((current) =>
                current.map((session) =>
                    String(session.id) === sessionId ? withSessionPermissionMode(session, mode) : session,
                ),
            );
            setAllSessions((current) =>
                current.map((session) =>
                    String(session.id) === sessionId ? withSessionPermissionMode(session, mode) : session,
                ),
            );
            invalidateSessionRuntimeQueries(id, sessionId);
        } catch (err: any) {
            setSessionPermissionMode(previous);
            const msg = err?.message || t('agent.chat.permission.updateFailed', 'Failed to update session permission mode');
            showToast(msg, 'error');
        }
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
    }, [planModeScopeKey]);
    useEffect(() => {
        setSessionPermissionMode(sessionPermissionModeFromSession(activeSession));
    }, [
        activeSession?.id,
        activeSession?.permission_mode,
        activeSession?.permission_profile?.mode,
        activeSession?.transcript_metadata_json?.permission_mode,
    ]);
    const [wsConnected, setWsConnected] = useState(false);
    const [uploading, setUploading] = useState(false);
    const [isWaiting, setIsWaiting] = useState(false);
    const [isStreaming, setIsStreaming] = useState(false);
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
    const wsRef = useRef<WebSocket | null>(null);
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
        smart_model_routing_enabled: false,
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
                smart_model_routing_enabled: !!(agent as any).smart_model_routing?.enabled,
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
                parsed.imageUrl = `/api/agents/${id}/files/download?path=workspace/uploads/${encodeURIComponent(parsed.fileName)}&token=${token}`;
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
        setHistoryMsgs([]);
        setHistoryMessagesSessionId(null);
        setTransportNotice(null);
        setIsStreaming(false);
        setIsWaiting(false);
        setWsConnected(false);
        wsRef.current = null;
        activeRunStateRef.current = {};
        pendingUserMessagesRef.current = {};
        runtimeActivityAtRef.current = {};
        setActiveRunStateBySession({});
        setChatScope('mine');
        setAgentExpired(false);
        settingsInitRef.current = false;
    }, [id]);

    useEffect(() => {
        if (!canLoadAgentScopedData || !token || activeTab !== 'chat') return;
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

    const ensureSessionSocket = (sess: any, agentId: string, authToken: string) => {
        const sessionId = String(sess.id);
        const key = buildSessionRuntimeKey(agentId, sessionId);
        const existing = wsMapRef.current[key];
        if (existing && (existing.readyState === WebSocket.OPEN || existing.readyState === WebSocket.CONNECTING)) return;
        reconnectDisabledRef.current[key] = false;
        const protocol = window.location.protocol === 'https:' ? 'wss:' : 'ws:';
        const sessionParam = `&session_id=${sessionId}`;

        const scheduleReconnect = () => {
            if (reconnectDisabledRef.current[key]) return;
            const attempts = reconnectAttemptsRef.current[key] ?? 0;
            if (attempts >= 20) {
                console.warn(`[WS] Giving up reconnect for ${key} after ${attempts} attempts`);
                reconnectDisabledRef.current[key] = true;
                return;
            }
            reconnectAttemptsRef.current[key] = attempts + 1;
            const baseDelay = Math.min(2000 * Math.pow(2, attempts), 60000);
            const delay = baseDelay * (0.5 + Math.random() * 0.5);
            clearReconnectTimer(key);
            reconnectTimerRef.current[key] = setTimeout(() => {
                reconnectTimerRef.current[key] = null;
                if (!reconnectDisabledRef.current[key]) ensureSessionSocket(sess, agentId, authToken);
            }, delay);
        };

        const ws = new WebSocket(`${protocol}//${window.location.host}/ws/chat/${agentId}?token=${authToken}${sessionParam}`);
        wsMapRef.current[key] = ws;
        ws.onopen = () => {
            if (reconnectDisabledRef.current[key]) {
                ws.close();
                return;
            }
            reconnectAttemptsRef.current[key] = 0;
            startKeepaliveTimer(key, ws);
            if (currentAgentIdRef.current === agentId && activeSessionIdRef.current === sessionId) {
                wsRef.current = ws;
                setWsConnected(true);
            }
        };
        ws.onclose = (e) => {
            if (wsMapRef.current[key] === ws) delete wsMapRef.current[key];
            clearKeepaliveTimer(key);
            const runStillActive = !!activeRunStateRef.current[key];
            setSessionUiState(key, { isWaiting: runStillActive, isStreaming: false });
            const isActiveRuntime = currentAgentIdRef.current === agentId && activeSessionIdRef.current === sessionId;
            if (isActiveRuntime) {
                wsRef.current = null;
                setWsConnected(false);
                setIsWaiting(runStillActive);
                setIsStreaming(false);
            }
            if (e.code === 4003 || e.code === 4002) {
                reconnectDisabledRef.current[key] = true;
                clearReconnectTimer(key);
                if (isActiveRuntime && e.code === 4003) setAgentExpired(true);
                return;
            }
            scheduleReconnect();
        };
        ws.onerror = (error) => {
            const isActiveRuntime = currentAgentIdRef.current === agentId && activeSessionIdRef.current === sessionId;
            if (isActiveRuntime) setWsConnected(false);
            console.warn(`WebSocket error for session ${sessionId}:`, error);
            // Error automatically triggers onclose with abnormal code, which handles reconnect
        };
        ws.onmessage = (e) => {
            const d = JSON.parse(e.data);
            const isActiveRuntime = currentAgentIdRef.current === agentId && activeSessionIdRef.current === sessionId;
            if (
                typeof d === 'object'
                && d
                && (typeof d.sequence === 'number' || d.transcript_event_id || d.metadata?.transcript_event_id)
                && (d.event_type || d.type)
            ) {
                const transcriptEvent = {
                    ...d,
                    id: d.id || d.transcript_event_id || d.metadata?.transcript_event_id,
                    event_type: d.event_type || d.type,
                    metadata: d.metadata || d.metadata_json || {},
                };
                applyTranscriptToSession(agentId, sessionId, transcriptEvent, isActiveRuntime);
                if (isActiveRuntime && isTerminalRealtimeChatEvent(transcriptEvent)) {
                    void selectSession(sess);
                    fetchMySessions(true, agentId);
                }
                return;
            }
            if (d.type === 'run_started' && d.run_id) {
                setActiveRunState(key, { runId: String(d.run_id), status: d.status || 'running' });
                invalidateSessionRuntimeQueries(agentId, sessionId);
                if (isActiveRuntime) {
                    setIsWaiting(true);
                    setIsStreaming(false);
                }
                return;
            }
            if (d.type === 'run_cancelled') {
                markActiveRunTerminal(key, d.run_id ? String(d.run_id) : null);
                invalidateSessionRuntimeQueries(agentId, sessionId);
                if (isActiveRuntime) {
                    setIsWaiting(false);
                    setIsStreaming(false);
                    void selectSession(sess);
                }
                fetchMySessions(true, agentId);
                return;
            }
            if (['thinking', 'chunk', 'tool_call', 'done', 'error', 'quota_exceeded'].includes(d.type)) {
                const nextStreaming = ['thinking', 'chunk', 'tool_call'].includes(d.type);
                const endStreaming = ['done', 'error', 'quota_exceeded'].includes(d.type);
                setSessionUiState(key, {
                    isWaiting: false,
                    isStreaming: endStreaming ? false : nextStreaming,
                });
                if (d.type === 'tool_call') {
                    // Tool-heavy turns fire this event in bursts; coalesce the
                    // HTTP refetch amplification into a 2s window (plan D2).
                    const now = Date.now();
                    if (now - (toolCallInvalidateAtRef.current[key] || 0) >= 2000) {
                        toolCallInvalidateAtRef.current[key] = now;
                        invalidateSessionRuntimeQueries(agentId, sessionId, false);
                    }
                }
                if (endStreaming) {
                    markActiveRunTerminal(key, d.run_id ? String(d.run_id) : null);
                    invalidateSessionRuntimeQueries(agentId, sessionId);
                }
            }
            if (!isActiveRuntime) {
                if (['done', 'error', 'quota_exceeded', 'trigger_notification'].includes(d.type)) {
                    fetchMySessions(true, agentId);
                }
                if (['done', 'error', 'quota_exceeded'].includes(d.type)) {
                    closeSessionSocket(key, true);
                }
                return;
            }
            setChatMessagesSessionId(sessionId);

            // Idle dream events — ignored (extraction now per-response, not idle-triggered)
            if (d.type === 'dreaming') {
                return;
            }

            const transportMessage = getTransportNotice(d);
            if (transportMessage) {
                setTransportNotice(transportMessage);
                return;
            }

            if (['thinking', 'chunk', 'tool_call', 'done', 'error', 'quota_exceeded'].includes(d.type)) {
                setIsWaiting(false);
                if (['thinking', 'chunk', 'tool_call'].includes(d.type)) setIsStreaming(true);
                if (['done', 'error', 'quota_exceeded'].includes(d.type)) setIsStreaming(false);
            }

            const runtimeEvent = getRuntimeEventMessage({ ...d, timestamp: new Date().toISOString() });
            if (runtimeEvent) {
                enqueueChatMessagesUpdate(prev => [...prev, parseChatMsg(runtimeEvent)]);
                return;
            }

            if (d.type === 'thinking') {
                enqueueChatMessagesUpdate(prev => {
                    const last = prev[prev.length - 1];
                    if (last && last.role === 'assistant' && (last as any)._streaming) {
                        return [...prev.slice(0, -1), { ...last, thinking: (last.thinking || '') + d.content } as any];
                    }
                    return [...prev, { role: 'assistant', content: '', thinking: d.content, _streaming: true } as any];
                });
            } else if (d.type === 'tool_call') {
                const normalizedResult = normalizeToolCallResult(d.name, d.result);
                const toolMsg: AgentChatMessage = normalizeToolCallMessage({
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
                if (isTerminalTranscriptToolMessage(toolMsg)) {
                    markActiveRunTerminal(key, d.run_id ? String(d.run_id) : null);
                    setSessionUiState(key, { isWaiting: false, isStreaming: false });
                    setIsWaiting(false);
                    setIsStreaming(false);
                }
                if (normalizedResult.createdAgentId) {
                    setCreatedAgentId(normalizedResult.createdAgentId);
                }
                enqueueChatMessagesUpdate(prev => appendToolCallMessage(prev, toolMsg));
            } else if (d.type === 'chunk') {
                enqueueChatMessagesUpdate(prev => applyStreamingChunkEvent(prev, d));
            } else if (d.type === 'done') {
                setChatMessagesAfterQueued(prev => applyRuntimeDoneEvent(prev, d).map(parseChatMsg));
                fetchMySessions(true, agentId);
                void selectSession(sess);
            } else if (d.type === 'error' || d.type === 'quota_exceeded') {
                const msg = d.content || d.detail || d.message || 'Request denied';
                setChatMessagesAfterQueued(prev => {
                    const last = prev[prev.length - 1];
                    if (last && last.role === 'assistant' && last.content === `⚠️ ${msg}`) return prev;
                    return [...prev, parseChatMsg({ role: 'assistant', content: `⚠️ ${msg}` })];
                });
                void selectSession(sess);
                if (msg.includes('expired') || msg.includes('Setup failed') || msg.includes('no LLM model') || msg.includes('No model')) {
                    reconnectDisabledRef.current[key] = true;
                    if (msg.includes('expired')) setAgentExpired(true);
                }
            } else if (d.type === 'trigger_notification') {
                enqueueChatMessagesUpdate(prev => [...prev, parseChatMsg({ role: 'assistant', content: d.content })]);
                fetchMySessions(true, agentId);
                queryClient.invalidateQueries({ queryKey: ['autonomy-overview', agentId] });
                queryClient.invalidateQueries({ queryKey: ['triggers', agentId] });
            } else if (typeof d.content === 'string' && (d.role === 'assistant' || d.role === 'user')) {
                enqueueChatMessagesUpdate(prev => [...prev, parseChatMsg({ role: d.role, content: d.content })]);
            }
        };
    };

    useEffect(() => {
        if (!canLoadAgentScopedData || !token || activeTab !== 'chat') return;
        if (!activeSession) {
            syncActiveSocketState(null, id);
            return;
        }
        activeSessionIdRef.current = String(activeSession.id);
        if (!isWritableSession(activeSession) || isDraftHumanChatSession(activeSession)) {
            syncActiveSocketState(activeSession, id);
            return;
        }
        ensureSessionSocket(activeSession, id, token);
        syncActiveSocketState(activeSession, id);
    }, [canLoadAgentScopedData, id, token, activeTab, activeSession?.id]);

    useEffect(() => {
        if (!canLoadAgentScopedData || activeTab !== 'chat' || !id || !activeSession?.id || isDraftHumanChatSession(activeSession)) {
            setBranchLineage([]);
            return;
        }
        fetchBranchLineage(id, String(activeSession.id));
    }, [canLoadAgentScopedData, activeTab, id, activeSession?.id]);

    useEffect(() => {
        return () => {
            sessionMsgAbortRef.current?.abort();
            Object.keys(reconnectDisabledRef.current).forEach((key) => { reconnectDisabledRef.current[key] = true; });
            Object.keys(reconnectTimerRef.current).forEach((key) => clearReconnectTimer(key));
            Object.keys(keepaliveTimerRef.current).forEach((key) => clearKeepaliveTimer(key));
            reconnectAttemptsRef.current = {};
            Object.values(wsMapRef.current).forEach((ws) => {
                if (ws.readyState !== WebSocket.CLOSED) ws.close();
            });
            wsMapRef.current = {};
            wsRef.current = null;
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
        let contentForLLM = userMsg;
        let displayFiles = '';
        const attachmentPayload = attachedFiles.map((file) => ({
            name: file.name,
            path: file.path,
            is_image: Boolean(file.imageUrl),
            has_image_data: Boolean(file.imageUrl),
            conversion: file.conversion || null,
        }));

        if (attachedFiles.length === 0) {
            let parsedSlashCommand: ReturnType<typeof parseSlashCommandInput> = null;
            try {
                parsedSlashCommand = parseSlashCommandInput(userMsg);
            } catch (err) {
                setChatMessagesSessionId(String(activeSession.id));
                setChatMessagesAfterQueued(prev => [
                    ...prev,
                    parseChatMsg({ role: 'user', content: userMsg, timestamp: new Date().toISOString() }),
                    parseChatMsg({
                        role: 'assistant',
                        content: `⚠️ ${err instanceof Error ? err.message : t('agent.chat.commands.invalid', 'Invalid slash command.')}`,
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
                    setChatMessagesAfterQueued(prev => [...prev, parseChatMsg({ role: 'assistant', content: `⚠️ ${msg}` })]);
                    return;
                }
                if (!commandSession?.id) return;
                const commandSessionId = String(commandSession.id);
                const activeRuntimeKey = buildSessionRuntimeKey(id, commandSessionId);
                setIsWaiting(true);
                setIsStreaming(false);
                setTransportNotice(null);
                setSessionUiState(activeRuntimeKey, { isWaiting: true, isStreaming: false });
                setChatMessagesSessionId(commandSessionId);
                setChatMessagesAfterQueued(prev => [...prev, parseChatMsg({
                    role: 'user',
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
                        setActiveRunState(activeRuntimeKey, { runId: run.run_id, status: run.status || 'running' });
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
                    setChatMessagesAfterQueued(prev => [...prev, parseChatMsg({ role: 'assistant', content: `⚠️ ${msg}` })]);
                } finally {
                    if (!commandStartedRun) {
                        setIsWaiting(false);
                        setIsStreaming(false);
                        setSessionUiState(activeRuntimeKey, { isWaiting: false, isStreaming: false });
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
        try {
            const started = await startRunForActiveSession({
                content: contentForLLM,
                display_content: userMsg,
                file_name: fileName,
                attachments: attachmentPayload,
                plan_mode_requested: planModeRequested,
                permission_mode: sessionPermissionMode,
            });
            if (!started?.session?.id) return;
            const runSessionId = String(started.session.id);
            const activeRuntimeKey = buildSessionRuntimeKey(id, runSessionId);
            setIsWaiting(true);
            setIsStreaming(false);
            setTransportNotice(null);
            setSessionUiState(activeRuntimeKey, { isWaiting: true, isStreaming: false });
            setChatMessagesSessionId(runSessionId);
            appendOptimisticUserMessage(activeRuntimeKey, {
                role: 'user',
                content: userMsg,
                fileName,
                imageUrl: attachedFiles.length === 1 ? attachedFiles[0].imageUrl : undefined,
                timestamp: new Date().toISOString(),
            });
            setChatInput('');
            setAttachedFiles([]);
            setPlanModeRequested(false);
            setActiveRunState(activeRuntimeKey, { runId: started.run.run_id, status: started.run.status || 'running' });
            invalidateSessionRuntimeQueries(id, runSessionId);
        } catch (err: any) {
            setIsWaiting(false);
            setIsStreaming(false);
            const msg = err?.message || t('agent.chat.runStartFailed', 'Failed to start run');
            setChatMessagesSessionId(String(activeSession.id));
            setChatMessagesAfterQueued(prev => [...prev, parseChatMsg({ role: 'assistant', content: `⚠️ ${msg}` })]);
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
        try {
            const started = await startRunForActiveSession({
                content: userMsg,
                display_content: userMsg,
                ...(options?.planMode ? { plan_mode_requested: true } : {}),
                permission_mode: sessionPermissionMode,
            });
            if (!started?.session?.id) return;
            const runSessionId = String(started.session.id);
            const activeRuntimeKey = buildSessionRuntimeKey(id, runSessionId);
            setIsWaiting(true);
            setIsStreaming(false);
            setTransportNotice(null);
            setSessionUiState(activeRuntimeKey, { isWaiting: true, isStreaming: false });
            setChatMessagesSessionId(runSessionId);
            appendOptimisticUserMessage(activeRuntimeKey, {
                role: 'user',
                content: userMsg,
                timestamp: new Date().toISOString(),
            });
            setActiveRunState(activeRuntimeKey, { runId: started.run.run_id, status: started.run.status || 'running' });
            invalidateSessionRuntimeQueries(id, runSessionId);
        } catch (err: any) {
            setIsWaiting(false);
            setIsStreaming(false);
            const msg = err?.message || t('agent.chat.runStartFailed', 'Failed to start run');
            setChatMessagesSessionId(String(activeSession.id));
            setChatMessagesAfterQueued(prev => [...prev, parseChatMsg({ role: 'assistant', content: `⚠️ ${msg}` })]);
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
                setActiveRunState(activeRuntimeKey, {
                    runId: response.run.run_id,
                    status: response.run.status || 'running',
                });
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
                start_run: !['fork', 'branch', 'rewind'].includes(mode),
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
                setActiveRunState(branchKey, { runId: response.run.run_id, status: response.run.status || 'running' });
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
                text: data.preview_text || data.extracted_text,
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
                text: data.preview_text || data.extracted_text,
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
        queryFn: () => chatApi.getRuntimeSummary(String(activeSession!.id)),
        enabled: canLoadAgentScopedData && activeTab === 'chat' && !!activeSession?.id && !isDraftHumanChatSession(activeSession),
        refetchInterval: activeTab === 'chat' && activeSession?.id && !isDraftHumanChatSession(activeSession) ? 10000 : false,
    });

    const { data: activeSessionRun } = useQuery({
        queryKey: ['chat-active-run', id, activeSession?.id],
        queryFn: () => chatApi.getActiveSessionRun(id!, String(activeSession!.id)),
        enabled: canLoadAgentScopedData && activeTab === 'chat' && !!activeSession?.id && !!activeSession && isWritableSession(activeSession) && !isDraftHumanChatSession(activeSession),
        // Idle sessions rely on WS run events (run_started/done invalidate this
        // query); the 3s timer is only a live-run fallback (plan D1).
        refetchInterval: (query) => (isLiveRun(query.state.data as SessionRun | null | undefined) ? 3000 : false),
    });

    useEffect(() => {
        if (!id || !activeSession?.id || !isWritableSession(activeSession)) return;
        const key = buildSessionRuntimeKey(id, String(activeSession.id));
        if (activeSessionRun && isLiveRun(activeSessionRun)) {
            if (shouldIgnoreObservedActiveRun({
                key,
                run: {
                    runId: activeSessionRun.run_id,
                    status: activeSessionRun.status,
                },
                terminalRunIds: locallyTerminalRunIdsRef.current,
                terminalSessionKeys: locallyTerminalSessionKeysRef.current,
            })) {
                invalidateSessionRuntimeQueries(id, String(activeSession.id), false);
                return;
            }
            const observedUiState = observeActiveRunState(key, {
                runId: activeSessionRun.run_id,
                status: activeSessionRun.status,
            });
            setIsWaiting(observedUiState.isWaiting);
            setIsStreaming(observedUiState.isStreaming);
            return;
        }
        const staleRuntimeState = Boolean(
            activeRunStateRef.current[key]
            || sessionUiStateRef.current[key]?.isWaiting
            || sessionUiStateRef.current[key]?.isStreaming
        );
        if (shouldClearStaleRuntimeState({
            hasStaleRuntimeState: staleRuntimeState,
            lastRuntimeActivityAt: runtimeActivityAtRef.current[key],
            now: Date.now(),
            graceMs: ACTIVE_RUN_ABSENCE_GRACE_MS,
        })) {
            setActiveRunState(key, null);
            setIsWaiting(false);
            setIsStreaming(false);
            invalidateSessionRuntimeQueries(id, String(activeSession.id), false);
            selectSession(activeSession);
            fetchMySessions(true, id);
        }
    }, [activeSessionRun, activeSession?.id, id]);

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
            setIsStreaming(false);
            setIsWaiting(false);
            setSessionUiState(activeRuntimeKey, { isWaiting: false, isStreaming: false });
            return;
        }
        const activeSocket = wsMapRef.current[activeRuntimeKey];
        if (activeSocket?.readyState === WebSocket.OPEN) {
            activeSocket.send(JSON.stringify({ type: 'abort' }));
        }
    }, [activeSession?.id, id]);

    // ─── File viewer ─────────────────────────────────────
    const [viewingFile, setViewingFile] = useState<string | null>(null);
    const [fileEditing, setFileEditing] = useState(false);
    const [fileDraft, setFileDraft] = useState('');
    const [promptModal, setPromptModal] = useState<{ title: string; placeholder: string; action: string } | null>(null);
    const [deleteConfirm, setDeleteConfirm] = useState<{ path: string; name: string; isDir: boolean } | null>(null);
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
    const { data: fileContent } = useQuery({
        queryKey: ['file-content', id, viewingFile],
        queryFn: () => fileApi.read(id!, viewingFile!),
        enabled: canLoadAgentScopedData && !!viewingFile,
    });

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
                                    <span className="agent-detail-expired-badge">Expired</span>
                                )}
                                {(agent as any).agent_type === 'local_agent' && (
                                    <span className="agent-detail-local-badge">{t('nav.localBadge', 'Local')}</span>
                                )}
                                {!(agent as any).is_expired && (agent as any).expires_at && (
                                    <span className="agent-detail-expires-text">
                                        Expires: {new Date((agent as any).expires_at).toLocaleString()}
                                    </span>
                                )}
                                {isAdmin && (
                                    <button
                                        onClick={openExpiryModal}
                                        title="Edit expiry time"
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
                    activeTab === 'a2a' && <AgentA2ASection agentId={id!} />
                }

                {/* ── Workspace Tab ── */}
                {
                    activeTab === 'workspace' && (
                        (agent as any)?.agent_type === 'local_agent' ? (
                            <LocalAgents key="workspace" agentId={id!} agentName={agent.name} embedded initialTab="workspace" />
                        ) : (
                            <AgentWorkspaceSection agentId={id!} />
                        )
                    )
                }

                {
                    activeTab === 'office' && <OfficeWorkbenchSection agentId={id!} />
                }

                {
                    activeTab === 'chat' && (
                        (agent as any)?.agent_type === 'local_agent' ? (
                            <LocalAgentChatSection key="chat" agentId={id!} agent={agent} agentPermissions={permData ?? null} />
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
                            branchLineage={branchLineage}
                            branchLineageLoading={branchLineageLoading}
                            onSelectBranchSession={selectBranchSession}
                            wsConnected={wsConnected}
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
                            onLoadOlderMessages={loadOlderMessages}
                            olderMessagesLoading={olderMessagesLoading}
                            hasOlderMessages={Boolean(
                                activeSession?.id
                                && id
                                && transcriptHasOlder[buildSessionRuntimeKey(id, String(activeSession.id))],
                            )}
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
                            activeRunStatus={currentActiveRunState?.status || null}
                            planModeRequested={planModeRequested}
                            onTogglePlanMode={handleTogglePlanMode}
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
            </div >

            <PromptModal
                open={!!promptModal}
                title={promptModal?.title || ''}
                placeholder={promptModal?.placeholder || ''}
                onCancel={() => setPromptModal(null)}
                onConfirm={async (value) => {
                    const action = promptModal?.action;
                    setPromptModal(null);
                    if (action === 'newFolder') {
                        await fileApi.write(id!, `${workspacePath}/${value}/.gitkeep`, '');
                        queryClient.invalidateQueries({ queryKey: ['files', id, workspacePath] });
                    } else if (action === 'newFile') {
                        await fileApi.write(id!, `${workspacePath}/${value}`, '');
                        queryClient.invalidateQueries({ queryKey: ['files', id, workspacePath] });
                        setViewingFile(`${workspacePath}/${value}`);
                        setFileEditing(true);
                        setFileDraft('');
                    } else if (action === 'newSkill') {
                        const template = `---\nname: ${value}\ndescription: Describe what this skill does\n---\n\n# ${value}\n\n## Overview\nDescribe the purpose and when to use this skill.\n\n## Process\n1. Step one\n2. Step two\n\n## Output Format\nDescribe the expected output format.\n`;
                        const skillPath = buildNewSkillFilePath('skills', value);
                        await fileApi.write(id!, skillPath, template);
                        queryClient.invalidateQueries({ queryKey: ['files', id, 'skills'] });
                        setViewingFile(skillPath);
                        setFileEditing(true);
                        setFileDraft(template);
                    }
                }}
            />

            <ConfirmModal
                open={!!deleteConfirm}
                title={t('common.delete')}
                message={`${t('common.delete')}: ${deleteConfirm?.name}?`}
                confirmLabel={t('common.delete')}
                danger
                onCancel={() => setDeleteConfirm(null)}
                onConfirm={async () => {
                    const path = deleteConfirm?.path;
                    setDeleteConfirm(null);
                    if (path) {
                        try {
                            await fileApi.delete(id!, path);
                            setViewingFile(null);
                            setFileEditing(false);
                            queryClient.invalidateQueries({ queryKey: ['files', id, workspacePath] });
                            showToast(t('common.delete'));
                        } catch (err: any) {
                            showToast(t('agent.upload.failed'), 'error');
                        }
                    }
                }}
            />

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

// Error boundary to catch unhandled React errors and prevent white screen
class AgentDetailErrorBoundary extends Component<{ children: React.ReactNode }, { hasError: boolean; error: Error | null }> {
    constructor(props: { children: React.ReactNode }) {
        super(props);
        this.state = { hasError: false, error: null };
    }
    static getDerivedStateFromError(error: Error) {
        return { hasError: true, error };
    }
    componentDidCatch(error: Error, errorInfo: ErrorInfo) {
        console.error('AgentDetail crash caught by error boundary:', error, errorInfo);
    }
    render() {
        if (this.state.hasError) {
            return (
                <div className="agent-detail-error">
                    <div className="agent-detail-error-title">Something went wrong</div>
                    <div className="agent-detail-error-message">
                        {this.state.error?.message || 'An unexpected error occurred while loading this page.'}
                    </div>
                    <button
                        className="btn btn-primary agent-detail-error-action"
                        onClick={() => { this.setState({ hasError: false, error: null }); window.location.reload(); }}
                    >
                        Reload Page
                    </button>
                </div>
            );
        }
        return this.props.children;
    }
}

// Wrap the AgentDetail component with error boundary
export default function AgentDetailWithErrorBoundary() {
    return (
        <AgentDetailErrorBoundary>
            <AgentDetailInner />
        </AgentDetailErrorBoundary>
    );
}
