import React, { useState, useEffect, useRef, Component, ErrorInfo } from 'react';
import { useParams, useLocation, useNavigate } from 'react-router-dom';
import { useQuery, useQueryClient } from '@tanstack/react-query';
import { useTranslation } from 'react-i18next';

import ConfirmModal from '../components/ConfirmModal';
import type { FileBrowserApi } from '../components/FileBrowser';
import FileBrowser from '../components/FileBrowser';
import PromptModal from '../components/PromptModal';
import AgentApprovalsSection from './agent-detail/AgentApprovalsSection';
import AgentWorkflowsSection from './agent-detail/AgentWorkflowsSection';
import AgentActivityLogSection from './agent-detail/AgentActivityLogSection';
import AgentAwareSection from './agent-detail/AgentAwareSection';
import AgentChatSection from './agent-detail/AgentChatSection';
import AgentEvolutionSection from './agent-detail/AgentEvolutionSection';
import AgentKnowledgeSection from './agent-detail/AgentKnowledgeSection';
import OfficeWorkbenchSection from './agent-detail/OfficeWorkbenchSection';
import AgentSettingsSection from './agent-detail/AgentSettingsSection';
import AgentSkillsSection from './agent-detail/AgentSkillsSection';
import AgentSubagentsSection from './agent-detail/AgentSubagentsSection';
import AgentStatusSection from './agent-detail/AgentStatusSection';
import AgentWorkspaceSection from './agent-detail/AgentWorkspaceSection';
import {
    CHAT_SOCKET_KEEPALIVE_INTERVAL_MS,
    applySessionActiveRunState,
    buildChatSocketKeepaliveMessage,
    buildRuntimeSummary,
    getRuntimeEventMessage,
    getTransportNotice,
    normalizeRuntimeEventMessage,
    normalizeStoredChatMessage,
    type AgentChatMessage,
    type ChatRuntimeSummary,
    type SessionRunState,
    type SessionUiState,
} from './agent-detail/chatRuntime';
import RelationshipEditor from './agent-detail/RelationshipEditor';
import ToolsManager from './agent-detail/ToolsManager';
import { normalizeToolCallResult } from './agent-detail/toolResultEnvelope';
import OpenClawSettings from './OpenClawSettings';
import { agentApi, type AgentCapabilityInstall, type AgentChannelCapability } from '../api/domains/agents';
import { activityApi } from '../api/domains/activity';
import { enterpriseApi, type CapabilityDefinition, type CapabilityPolicy } from '../api/domains/enterprise';
import { fileApi } from '../api/domains/files';
import { triggerApi } from '../api/domains/triggers';
import { autonomyApi } from '../api/domains/autonomy';
import { chatApi, type SessionRun } from '../api/domains/chat';
import { uploadFileWithProgress } from '../api/core/upload-progress';
import { useAuthStore } from '../stores';

// P8 IA (docs/agent-memory-md-first-spec.md §10): Knowledge replaces the
// raw-file "mind" tab as the primary memory view (raw Markdown lives in the
// Knowledge → Raw subview); `tools` is the MCP plane; skills and workflows
// stay standalone capability modules that Knowledge only deep-links to.
// C3 (docs/subagent-source-capability.md §12.8): subagents joins as the
// fourth capability module — the employee's craft-clone work methods.
const TABS = ['status', 'aware', 'knowledge', 'evolution', 'tools', 'skills', 'subagents', 'relationships', 'workspace', 'workflows', 'office', 'chat', 'activityLog', 'approvals', 'settings'] as const;

/** Visual grouping of tabs for the tab bar — groups are separated by thin dividers */
const TAB_GROUPS: { tabs: (typeof TABS[number])[]; }[] = [
    { tabs: ['status', 'chat'] },
    { tabs: ['aware', 'knowledge', 'evolution', 'tools', 'skills', 'subagents'] },
    { tabs: ['workspace', 'workflows', 'office', 'relationships', 'activityLog', 'approvals'] },
    { tabs: ['settings'] },
];

function AgentDetailInner() {
    const { t, i18n } = useTranslation();
    const { id } = useParams<{ id: string }>();
    const navigate = useNavigate();
    const queryClient = useQueryClient();
    const location = useLocation();
    const validTabs = ['status', 'aware', 'knowledge', 'evolution', 'tools', 'skills', 'subagents', 'relationships', 'workspace', 'workflows', 'office', 'chat', 'activityLog', 'approvals', 'settings'];
    // Legacy deep links: #mind was the raw-file memory tab before the
    // Knowledge plane replaced it (P8).
    const rawHashTab = location.hash?.replace('#', '');
    const hashTab = rawHashTab === 'mind' ? 'knowledge' : rawHashTab;
    const [activeTab, setActiveTabRaw] = useState<string>(hashTab && validTabs.includes(hashTab) ? hashTab : 'status');

    // Sync URL hash when tab changes
    const setActiveTab = (tab: string) => {
        setActiveTabRaw(tab);
        window.history.replaceState(null, '', `#${tab}`);
    };
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

    // ── Aware tab data: focus.md ──
    const { data: focusFile } = useQuery({
        queryKey: ['file', id, 'focus.md'],
        queryFn: () => fileApi.read(id!, 'focus.md').catch(() => null),
        enabled: canLoadAgentScopedData && activeTab === 'aware',
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
    const [expandedFocus, setExpandedFocus] = useState<string | null>(null);
    const [expandedReflection, setExpandedReflection] = useState<string | null>(null);
    const [reflectionMessages, setReflectionMessages] = useState<Record<string, any[]>>({});
    const [showAllFocus, setShowAllFocus] = useState(false);
    const [showCompletedFocus, setShowCompletedFocus] = useState(false);
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
    const activeRunStateRef = useRef<Record<SessionRuntimeKey, SessionRunState>>({});
    const activeSessionIdRef = useRef<string | null>(null);
    const currentAgentIdRef = useRef<string | undefined>(id);
    const sessionMsgAbortRef = useRef<AbortController | null>(null);
    const sessionLoadSeqRef = useRef(0);
    const [activeRunStateBySession, setActiveRunStateBySession] = useState<Record<SessionRuntimeKey, SessionRunState>>({});

    const buildSessionRuntimeKey = (agentId: string, sessionId: string) => `${agentId}:${sessionId}`;
    const isLiveRun = (run?: SessionRun | null) => !!run && ['pending', 'running'].includes(String(run.status || '').toLowerCase());

    const invalidateSessionRuntimeQueries = (agentId: string, sessionId: string, includeActiveRun = true) => {
        if (includeActiveRun) void queryClient.invalidateQueries({ queryKey: ['chat-active-run', agentId, sessionId] });
        void queryClient.invalidateQueries({ queryKey: ['chat-runtime-summary', agentId, sessionId] });
        void queryClient.invalidateQueries({ queryKey: ['chat-session-work-ledger', agentId, sessionId] });
    };

    const setActiveRunState = (key: SessionRuntimeKey, run: SessionRunState | null) => {
        const next = applySessionActiveRunState(activeRunStateRef.current, sessionUiStateRef.current, key, run);
        activeRunStateRef.current = next.activeRuns;
        sessionUiStateRef.current = next.uiStates;
        setActiveRunStateBySession(next.activeRuns);
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
    };

    const setSessionUiState = (key: SessionRuntimeKey, next: Partial<{ isWaiting: boolean; isStreaming: boolean }>) => {
        const prev = sessionUiStateRef.current[key] || { isWaiting: false, isStreaming: false };
        sessionUiStateRef.current[key] = { ...prev, ...next };
    };

    const isWritableSession = (sess: any) => {
        if (!sess) return false;
        const isAgentSession = sess.source_channel === 'agent' || sess.participant_type === 'agent';
        if (isAgentSession) return false;
        if (sess.user_id && currentUser && sess.user_id !== String(currentUser.id)) return false;
        return true;
    };

    const syncActiveSocketState = (sess: any | null = activeSession, agentId: string | undefined = id) => {
        if (!sess || !agentId) {
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
            const data = await chatApi.listSessions(agentId, 'mine');
            if (currentAgentIdRef.current === agentId) setSessions(data);
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
            const all = await chatApi.listSessions(id, 'all');
            if (currentAgentIdRef.current === id) {
                setAllSessions(all.filter((s: any) => s.source_channel !== 'trigger'));
            }
        } catch { }
        setAllSessionsLoading(false);
    };

    const selectSession = async (sess: any) => {
        const targetAgentId = id;
        if (!targetAgentId) return;
        const sessionId = String(sess.id);
        const runtimeKey = buildSessionRuntimeKey(targetAgentId, sessionId);
        const runtimeState = sessionUiStateRef.current[runtimeKey] || { isWaiting: false, isStreaming: false };
        const activeRunState = activeRunStateRef.current[runtimeKey];
        const writableSession = isWritableSession(sess);
        activeSessionIdRef.current = sessionId;
        setCreatedAgentId(null);
        setChatMessages([]);
        setChatMessagesSessionId(writableSession ? sessionId : null);
        setHistoryMsgs([]);
        setHistoryMessagesSessionId(writableSession ? null : sessionId);
        setTransportNotice(null);
        setIsStreaming(runtimeState.isStreaming);
        setIsWaiting(runtimeState.isWaiting || !!activeRunState);
        setActiveSession(sess);
        setAgentExpired(false);
        syncActiveSocketState(sess, targetAgentId);

        // Abort any pending message load and increment sequence
        sessionMsgAbortRef.current?.abort();
        const controller = new AbortController();
        sessionMsgAbortRef.current = controller;
        const loadSeq = ++sessionLoadSeqRef.current;
        try {
            const msgs = await chatApi.getSessionMessages(targetAgentId, sessionId, { signal: controller.signal });
            if (controller.signal.aborted || loadSeq !== sessionLoadSeqRef.current) return;
            if (currentAgentIdRef.current !== targetAgentId) return;
            if (activeSessionIdRef.current !== sessionId) return;
            const isAgentSession = sess.source_channel === 'agent' || sess.participant_type === 'agent';
            const preParsed = msgs.map((m: any) => parseChatMsg(normalizeStoredChatMessage(m)));
            
            if (!isAgentSession && sess.user_id === String(currentUser?.id)) {
                setChatMessagesSessionId(sessionId);
                setChatMessages(preParsed);
            } else {
                setHistoryMessagesSessionId(sessionId);
                setHistoryMsgs(preParsed);
            }
        } catch (err: any) {
            if (err?.name === 'AbortError') return;
            console.error('Failed to load session messages:', err);
        }
    };

    const createNewSession = async () => {
        if (!id) return;
        try {
            const newSess = await chatApi.createSession(id);
            setSessions(prev => [newSess, ...prev]);
            setIsStreaming(false);
            setIsWaiting(false);
            await selectSession(newSess);
        } catch (err: any) {
            console.error('Failed to create session:', err);
            alert(`Failed to create session: ${err.message || err}`);
        }
    };

    const deleteSession = async (sessionId: string) => {
        if (!confirm(t('chat.deleteConfirm', 'Delete this session and all its messages? This cannot be undone.'))) return;
        try {
            await chatApi.deleteSession(id!, sessionId);
            if (id) closeSessionSocket(buildSessionRuntimeKey(id, sessionId), true);
            // If deleted the active session, clear it
            if (activeSession?.id === sessionId) {
                activeSessionIdRef.current = null;
                setActiveSession(null);
                setChatMessages([]);
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
            alert(e.message || 'Delete failed');
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
        } catch (e) { alert('Failed: ' + e); }
        setExpirySaving(false);
    };
    const [chatMessages, setChatMessages] = useState<AgentChatMessage[]>([]);
    const [chatMessagesSessionId, setChatMessagesSessionId] = useState<string | null>(null);
    const [chatInput, setChatInput] = useState('');
    const [planModeRequested, setPlanModeRequested] = useState(false);
    const [wsConnected, setWsConnected] = useState(false);
    const [uploading, setUploading] = useState(false);
    const [isWaiting, setIsWaiting] = useState(false);
    const [isStreaming, setIsStreaming] = useState(false);
    const [transportNotice, setTransportNotice] = useState<string | null>(null);

    const [uploadProgress, setUploadProgress] = useState(-1);
    const uploadAbortRef = useRef<(() => void) | null>(null);
    const [attachedFiles, setAttachedFiles] = useState<{ name: string; text: string; path?: string; imageUrl?: string }[]>([]);
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
            window.history.replaceState(null, '', `#${activeTab}`);
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
        activeSessionIdRef.current = null;
        setActiveSession(null);
        setChatMessages([]);
        setChatMessagesSessionId(null);
        setHistoryMsgs([]);
        setHistoryMessagesSessionId(null);
        setTransportNotice(null);
        setIsStreaming(false);
        setIsWaiting(false);
        setWsConnected(false);
        wsRef.current = null;
        activeRunStateRef.current = {};
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
            if (data && data.length > 0) selectSession(data[0]);
        });
    }, [canLoadAgentScopedData, id, token, activeTab]);

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
                setActiveRunState(key, null);
                invalidateSessionRuntimeQueries(agentId, sessionId);
                if (isActiveRuntime) {
                    setIsWaiting(false);
                    setIsStreaming(false);
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
                if (d.type === 'tool_call') invalidateSessionRuntimeQueries(agentId, sessionId, false);
                if (endStreaming) {
                    setActiveRunState(key, null);
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
                setChatMessages(prev => [...prev, parseChatMsg(runtimeEvent)]);
                return;
            }

            if (d.type === 'thinking') {
                setChatMessages(prev => {
                    const last = prev[prev.length - 1];
                    if (last && last.role === 'assistant' && (last as any)._streaming) {
                        return [...prev.slice(0, -1), { ...last, thinking: (last.thinking || '') + d.content } as any];
                    }
                    return [...prev, { role: 'assistant', content: '', thinking: d.content, _streaming: true } as any];
                });
            } else if (d.type === 'tool_call') {
                const normalizedResult = normalizeToolCallResult(d.name, d.result);
                setChatMessages(prev => {
                    const toolMsg: AgentChatMessage = normalizeToolCallMessage({
                        role: 'tool_call',
                        content: '',
                        toolName: d.name,
                        toolArgs: d.args,
                        toolStatus: d.status,
                        toolResult: normalizedResult.displayResult,
                        toolRawResult: normalizedResult.raw,
                        toolMeta: normalizedResult.toolMeta,
                    });
                    if (d.status === 'done') {
                        const lastIdx = prev.length - 1;
                        const last = prev[lastIdx];
                        if (last && last.role === 'tool_call' && last.toolName === d.name && last.toolStatus === 'running') return [...prev.slice(0, lastIdx), toolMsg];
                    }
                    return [...prev, toolMsg];
                });
                if (normalizedResult.createdAgentId) {
                    setCreatedAgentId(normalizedResult.createdAgentId);
                }
            } else if (d.type === 'chunk') {
                setChatMessages(prev => {
                    const last = prev[prev.length - 1];
                    if (last && last.role === 'assistant' && (last as any)._streaming) return [...prev.slice(0, -1), { ...last, content: last.content + d.content } as any];
                    return [...prev, { role: 'assistant', content: d.content, _streaming: true } as any];
                });
            } else if (d.type === 'done') {
                setChatMessages(prev => {
                    const last = prev[prev.length - 1];
                    const thinking = (last && last.role === 'assistant' && (last as any)._streaming) ? last.thinking : undefined;
                    if (last && last.role === 'assistant' && (last as any)._streaming) return [...prev.slice(0, -1), parseChatMsg({ role: 'assistant', content: d.content, thinking, timestamp: new Date().toISOString() })];
                    return [...prev, parseChatMsg({ role: d.role, content: d.content, timestamp: new Date().toISOString() })];
                });
                fetchMySessions(true, agentId);
            } else if (d.type === 'error' || d.type === 'quota_exceeded') {
                const msg = d.content || d.detail || d.message || 'Request denied';
                setChatMessages(prev => {
                    const last = prev[prev.length - 1];
                    if (last && last.role === 'assistant' && last.content === `⚠️ ${msg}`) return prev;
                    return [...prev, parseChatMsg({ role: 'assistant', content: `⚠️ ${msg}` })];
                });
                if (msg.includes('expired') || msg.includes('Setup failed') || msg.includes('no LLM model') || msg.includes('No model')) {
                    reconnectDisabledRef.current[key] = true;
                    if (msg.includes('expired')) setAgentExpired(true);
                }
            } else if (d.type === 'trigger_notification') {
                setChatMessages(prev => [...prev, parseChatMsg({ role: 'assistant', content: d.content })]);
                fetchMySessions(true, agentId);
                queryClient.invalidateQueries({ queryKey: ['autonomy-overview', agentId] });
                queryClient.invalidateQueries({ queryKey: ['triggers', agentId] });
            } else if (typeof d.content === 'string' && (d.role === 'assistant' || d.role === 'user')) {
                setChatMessages(prev => [...prev, parseChatMsg({ role: d.role, content: d.content })]);
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
        if (!isWritableSession(activeSession)) {
            syncActiveSocketState(activeSession, id);
            return;
        }
        ensureSessionSocket(activeSession, id, token);
        syncActiveSocketState(activeSession, id);
    }, [canLoadAgentScopedData, id, token, activeTab, activeSession?.id]);

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
        const activeRuntimeKey = buildSessionRuntimeKey(id, String(activeSession.id));
        if (activeRunStateRef.current[activeRuntimeKey]) return;
        if (!chatInput.trim() && attachedFiles.length === 0) return;
        
        let userMsg = chatInput.trim();
        let contentForLLM = userMsg;
        let displayFiles = '';

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
                    filesPrompt += `[File: ${file.name}]${fileLoc}\n${file.text}\n\n`;
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

        setIsWaiting(true);
        setIsStreaming(false);
        setTransportNotice(null);
        setSessionUiState(activeRuntimeKey, { isWaiting: true, isStreaming: false });
        setChatMessagesSessionId(String(activeSession.id));
        setChatMessages(prev => [...prev, parseChatMsg({ 
            role: 'user', 
            content: userMsg, 
            fileName: attachedFiles.map(f => f.name).join(', '), 
            imageUrl: attachedFiles.length === 1 ? attachedFiles[0].imageUrl : undefined, 
            timestamp: new Date().toISOString() 
        })]);
        setChatInput(''); 
        setAttachedFiles([]);
        try {
            const run = await chatApi.startSessionRun(id, String(activeSession.id), {
                content: contentForLLM,
                display_content: userMsg,
                file_name: attachedFiles.map(f => f.name).join(', '),
                plan_mode_requested: planModeRequested,
            });
            setPlanModeRequested(false);
            setActiveRunState(activeRuntimeKey, { runId: run.run_id, status: run.status || 'running' });
            invalidateSessionRuntimeQueries(id, String(activeSession.id));
        } catch (err: any) {
            setIsWaiting(false);
            setIsStreaming(false);
            setSessionUiState(activeRuntimeKey, { isWaiting: false, isStreaming: false });
            const msg = err?.message || t('agent.chat.runStartFailed', 'Failed to start run');
            setChatMessages(prev => [...prev, parseChatMsg({ role: 'assistant', content: `⚠️ ${msg}` })]);
        }
    };

    // Sends an explicit text message (not from the composer). Used by inline
    // chat cards (e.g. the ask_user_question clarification card) to post the
    // user's answer so the agent's blocked turn resumes. Reuses the same
    // startSessionRun path as sendChatMsg — the answer reaches the backend as a
    // normal user message.
    const sendChatMessageText = async (text: string) => {
        const userMsg = text.trim();
        if (!id || !activeSession?.id || !userMsg) return;
        const activeRuntimeKey = buildSessionRuntimeKey(id, String(activeSession.id));
        if (activeRunStateRef.current[activeRuntimeKey]) return;

        setIsWaiting(true);
        setIsStreaming(false);
        setTransportNotice(null);
        setSessionUiState(activeRuntimeKey, { isWaiting: true, isStreaming: false });
        setChatMessagesSessionId(String(activeSession.id));
        setChatMessages(prev => [...prev, parseChatMsg({
            role: 'user',
            content: userMsg,
            timestamp: new Date().toISOString(),
        })]);
        try {
            const run = await chatApi.startSessionRun(id, String(activeSession.id), {
                content: userMsg,
                display_content: userMsg,
            });
            setActiveRunState(activeRuntimeKey, { runId: run.run_id, status: run.status || 'running' });
            invalidateSessionRuntimeQueries(id, String(activeSession.id));
        } catch (err: any) {
            setIsWaiting(false);
            setIsStreaming(false);
            setSessionUiState(activeRuntimeKey, { isWaiting: false, isStreaming: false });
            const msg = err?.message || t('agent.chat.runStartFailed', 'Failed to start run');
            setChatMessages(prev => [...prev, parseChatMsg({ role: 'assistant', content: `⚠️ ${msg}` })]);
            throw err;
        }
    };

    const handleChatFile = async (e: React.ChangeEvent<HTMLInputElement>) => {
        const files = Array.from(e.target.files || []);
        if (!files.length) return;
        const allowedFiles = files.slice(0, 10 - attachedFiles.length);
        if (!allowedFiles.length) {
            alert('Limit of 10 attached files reached.');
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
                name: data.filename, text: data.extracted_text, path: data.workspace_path, imageUrl: data.image_data_url || undefined
            }));
            setAttachedFiles(prev => [...prev, ...newAttached].slice(0, 10));
        } catch (err: any) {
            if (err?.message !== 'Upload cancelled') alert(t('agent.upload.failed'));
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
            alert('Limit of 10 attached files reached.');
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
                name: data.filename, text: data.extracted_text, path: data.workspace_path, imageUrl: data.image_data_url || undefined
            }));
            setAttachedFiles(prev => [...prev, ...newAttached].slice(0, 10));
        } catch (err: any) {
            if (err?.message !== 'Upload cancelled') alert(t('agent.upload.failed'));
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
        enabled: canLoadAgentScopedData && activeTab === 'chat' && !!activeSession?.id,
        refetchInterval: activeTab === 'chat' && activeSession?.id ? 10000 : false,
    });

    const { data: activeSessionRun } = useQuery({
        queryKey: ['chat-active-run', id, activeSession?.id],
        queryFn: () => chatApi.getActiveSessionRun(id!, String(activeSession!.id)),
        enabled: canLoadAgentScopedData && activeTab === 'chat' && !!activeSession?.id && !!activeSession && isWritableSession(activeSession),
        refetchInterval: activeTab === 'chat' && activeSession?.id ? 3000 : false,
    });

    useEffect(() => {
        if (!id || !activeSession?.id || !isWritableSession(activeSession)) return;
        const key = buildSessionRuntimeKey(id, String(activeSession.id));
        if (activeSessionRun && isLiveRun(activeSessionRun)) {
            setActiveRunState(key, { runId: activeSessionRun.run_id, status: activeSessionRun.status });
            setIsWaiting(true);
            setIsStreaming(false);
            return;
        }
        const staleRuntimeState = Boolean(
            activeRunStateRef.current[key]
            || sessionUiStateRef.current[key]?.isWaiting
            || sessionUiStateRef.current[key]?.isStreaming
        );
        if (staleRuntimeState) {
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
        enabled: canLoadAgentScopedData && activeTab === 'settings',
    });

    const canManageCapabilityPolicies = isAdmin;

    const {
        data: capabilityDefinitions = [],
        isLoading: capabilityDefinitionsLoading,
        error: capabilityDefinitionsError,
    } = useQuery<CapabilityDefinition[]>({
        queryKey: ['capability-definitions'],
        queryFn: () => enterpriseApi.listCapabilityDefinitions(),
        enabled: canLoadAgentScopedData && activeTab === 'settings' && canManageCapabilityPolicies,
        retry: false,
    });

    const {
        data: capabilityPolicies = [],
        isLoading: capabilityPoliciesLoading,
        error: capabilityPoliciesError,
    } = useQuery<CapabilityPolicy[]>({
        queryKey: ['capability-policies', id],
        queryFn: () => enterpriseApi.listCapabilityPolicies(id!),
        enabled: canLoadAgentScopedData && activeTab === 'settings' && canManageCapabilityPolicies,
        retry: false,
    });

    const capabilityPolicyError =
        (capabilityPoliciesError as Error | null)?.message ||
        (capabilityDefinitionsError as Error | null)?.message ||
        '';

    // ─── File viewer ─────────────────────────────────────
    const [viewingFile, setViewingFile] = useState<string | null>(null);
    const [fileEditing, setFileEditing] = useState(false);
    const [fileDraft, setFileDraft] = useState('');
    const [promptModal, setPromptModal] = useState<{ title: string; placeholder: string; action: string } | null>(null);
    const [deleteConfirm, setDeleteConfirm] = useState<{ path: string; name: string; isDir: boolean } | null>(null);
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

    // ─── Task creation & detail ───────────────────────────────────
    const [showDeleteConfirm, setShowDeleteConfirm] = useState(false);

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
        return <div style={{ padding: '40px', color: 'var(--text-tertiary)' }}>{t('common.loading')}</div>;
    }

    if (!agent) {
        const status = (agentError as { status?: number } | null | undefined)?.status;
        const message = status === 403
            ? t('agent.detail.accessDenied', 'You do not have access to this employee.')
            : t('agent.detail.loadFailed', 'Unable to load this employee right now.');
        return <div style={{ padding: '40px', color: 'var(--text-tertiary)' }}>{message}</div>;
    }

    // Compute display status (including OpenClaw disconnected detection)
    const computeStatusKey = () => {
        if (agent.status === 'error') return 'error';
        if (agent.status === 'creating') return 'creating';
        if (agent.status === 'stopped') return 'stopped';
        if ((agent as any).agent_type === 'openclaw' && agent.status === 'running' && (agent as any).openclaw_last_seen) {
            const elapsed = Date.now() - new Date((agent as any).openclaw_last_seen).getTime();
            if (elapsed > 60 * 60 * 1000) return 'disconnected';
        }
        return agent.status === 'running' ? 'running' : 'idle';
    };
    const statusKey = computeStatusKey();
    const isSystemHrRaw = (agent as any).agent_class === 'internal_system';
    const isManageMode = new URLSearchParams(location.search).has('manage');
    const isSystemHr = isSystemHrRaw && !isManageMode;

    // HR system agent: force chat-only mode
    if (isSystemHr && activeTab !== 'chat') {
        setActiveTab('chat');
    }

    return (
        <>
            <div>
                {/* Header */}
                {isSystemHr ? (
                    <div className="page-header">
                        <div style={{ display: 'flex', alignItems: 'center', gap: '12px' }}>
                            <div style={{ width: '40px', height: '40px', borderRadius: '10px', background: 'var(--accent-subtle)', display: 'flex', alignItems: 'center', justifyContent: 'center', fontSize: '20px' }}>&#x1F464;</div>
                            <div>
                                <h1 className="page-title" style={{ marginBottom: 0 }}>{t('nav.newAgent', 'Create Digital Employee')}</h1>
                                <p className="page-subtitle" style={{ marginTop: '2px' }}>{t('hrChat.subtitle', 'Tell the HR agent what kind of digital employee you need')}</p>
                            </div>
                        </div>
                    </div>
                ) : (
                <div className="page-header">
                    <div style={{ display: 'flex', alignItems: 'center', gap: '16px' }}>
                        <div style={{ width: '48px', height: '48px', borderRadius: '12px', background: 'var(--accent-subtle)', display: 'flex', alignItems: 'center', justifyContent: 'center', fontSize: '24px' }}>{(Array.from(agent.name || 'A')[0] as string || 'A').toUpperCase()}</div>
                        <div style={{ flex: 1, minWidth: 0, overflow: 'hidden' }}>
                            {canManage && editingName ? (
                                <input
                                    className="page-title"
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
                                    style={{
                                        background: 'var(--bg-elevated)', border: '1px solid var(--accent-primary)',
                                        borderRadius: '6px', color: 'var(--text-primary)',
                                        padding: '4px 10px', minWidth: '320px', width: 'auto', outline: 'none',
                                        marginBottom: '0', display: 'block',
                                    }}
                                />
                            ) : (
                                <h1 className="page-title"
                                    title={canManage ? "Click to edit name" : undefined}
                                    onClick={() => { if (canManage) { setNameInput(agent.name); setEditingName(true); } }}
                                    style={{ cursor: canManage ? 'text' : 'default', borderBottom: canManage ? '1px dashed transparent' : 'none', display: 'inline-block', marginBottom: '0' }}
                                    onMouseEnter={e => { if (canManage) e.currentTarget.style.borderBottomColor = 'var(--text-tertiary)'; }}
                                    onMouseLeave={e => { if (canManage) e.currentTarget.style.borderBottomColor = 'transparent'; }}
                                >
                                    {agent.name}
                                </h1>
                            )}
                            <p className="page-subtitle" style={{ display: 'flex', alignItems: 'center', gap: '8px', marginTop: '4px' }}>
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
                                        style={{
                                            background: 'var(--bg-elevated)', border: '1px solid var(--accent-primary)',
                                            borderRadius: '6px', color: 'var(--text-primary)', fontSize: '13px',
                                            padding: '6px 10px', width: 'min(500px, 50vw)', outline: 'none',
                                            resize: 'vertical', lineHeight: '1.5', fontFamily: 'inherit',
                                        }}
                                    />
                                ) : (
                                    <span
                                        title={canManage ? (agent.role_description || 'Click to edit') : (agent.role_description || '')}
                                        onClick={() => { if (canManage) { setRoleInput(agent.role_description || ''); setEditingRole(true); } }}
                                        style={{ cursor: canManage ? 'text' : 'default', borderBottom: canManage ? '1px dashed transparent' : 'none', maxWidth: '38vw', overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap', display: 'inline-block', verticalAlign: 'middle' }}
                                        onMouseEnter={e => { if (canManage) e.currentTarget.style.borderBottomColor = 'var(--text-tertiary)'; }}
                                        onMouseLeave={e => { if (canManage) e.currentTarget.style.borderBottomColor = 'transparent'; }}
                                    >
                                        {agent.role_description ? `· ${agent.role_description}` : (canManage ? <span style={{ color: 'var(--text-tertiary)', fontSize: '12px' }}>· {t('agent.fields.role', 'Click to add a description...')}</span> : null)}
                                    </span>
                                )}
                                {(agent as any).is_expired && (
                                    <span style={{ background: 'var(--error)', color: '#fff', padding: '2px 8px', borderRadius: '4px', fontSize: '11px', fontWeight: 600 }}>Expired</span>
                                )}
                                {(agent as any).agent_type === 'openclaw' && (
                                    <span style={{
                                        fontSize: '10px', padding: '2px 6px', borderRadius: '4px',
                                        background: 'linear-gradient(135deg, #6366f1, #8b5cf6)', color: '#fff', fontWeight: 600,
                                        letterSpacing: '0.5px',
                                    }}>OpenClaw · Lab</span>
                                )}
                                {!(agent as any).is_expired && (agent as any).expires_at && (
                                    <span style={{ fontSize: '11px', color: 'var(--text-tertiary)' }}>
                                        Expires: {new Date((agent as any).expires_at).toLocaleString()}
                                    </span>
                                )}
                                {isAdmin && (
                                    <button
                                        onClick={openExpiryModal}
                                        title="Edit expiry time"
                                        style={{ background: 'none', border: 'none', cursor: 'pointer', fontSize: '11px', color: 'var(--text-tertiary)', padding: '1px 4px', borderRadius: '4px', lineHeight: 1 }}
                                        onMouseEnter={e => (e.currentTarget.style.background = 'var(--bg-secondary)')}
                                        onMouseLeave={e => (e.currentTarget.style.background = 'none')}
                                    >✏️ {t((agent as any).expires_at || (agent as any).is_expired ? 'agent.settings.expiry.renew' : 'agent.settings.expiry.setExpiry')}</button>
                                )}
                            </p>
                        </div>
                    </div>
                    <div style={{ display: 'flex', gap: '8px' }}>
                        <button className="btn btn-primary" onClick={() => setActiveTab('chat')}>{t('agent.actions.chat')}</button>
                        {(agent as any)?.agent_type !== 'openclaw' && (
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
                )}

                {/* Tabs — hidden for HR system agent */}
                {!isSystemHr && (
                <div className="tabs">
                    {TAB_GROUPS.map((group, gi) => {
                        const visibleTabs = group.tabs.filter(tab => {
                            if ((agent as any)?.access_level === 'use') {
                                if (tab === 'settings' || tab === 'approvals') return false;
                            }
                            if ((agent as any)?.agent_type === 'openclaw') {
                                return ['status', 'relationships', 'chat', 'activityLog', 'settings'].includes(tab);
                            }
                            return true;
                        });
                        if (visibleTabs.length === 0) return null;
                        return (
                            <React.Fragment key={gi}>
                                {gi > 0 && <div className="tab-separator" />}
                                {visibleTabs.map(tab => {
                                    const tooltipKey = `agent.tabs.${tab}Tooltip`;
                                    const tooltip = t(tooltipKey, { defaultValue: '' });
                                    return (
                                        <div
                                            key={tab}
                                            className={`tab ${activeTab === tab ? 'active' : ''}`}
                                            onClick={() => setActiveTab(tab)}
                                            title={tooltip || undefined}
                                        >
                                            {t(`agent.tabs.${tab}`)}
                                        </div>
                                    );
                                })}
                            </React.Fragment>
                        );
                    })}
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
                        onSelectTab={setActiveTab}
                    />
                )}

                {/* ── Aware Tab ── */}
                {activeTab === 'aware' && (
                    <AgentAwareSection
                        agentId={id!}
                        focusContent={focusFile?.content || ''}
                        awareTriggers={awareTriggers}
                        activityLogs={activityLogs}
                        reflectionSessions={reflectionSessions}
                        reflectionMessages={reflectionMessages}
                        expandedFocus={expandedFocus}
                        expandedReflection={expandedReflection}
                        showAllFocus={showAllFocus}
                        showCompletedFocus={showCompletedFocus}
                        showAllTriggers={showAllTriggers}
                        reflectionPage={reflectionPage}
                        onSetExpandedFocus={setExpandedFocus}
                        onSetExpandedReflection={setExpandedReflection}
                        onSetReflectionMessages={setReflectionMessages}
                        onSetShowAllFocus={setShowAllFocus}
                        onSetShowCompletedFocus={setShowCompletedFocus}
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
                        onNavigateTab={setActiveTab}
                    />
                )}

                {/* ── Evolution Tab (skill lifecycle + evolution timeline) ── */}
                {activeTab === 'evolution' && <AgentEvolutionSection agentId={id!} active={canLoadAgentScopedData} />}
                {activeTab === 'workflows' && (
                    <AgentWorkflowsSection agentId={id!} canManage={(agent as any)?.access_level !== 'use'} />
                )}

                {/* ── Tools Tab ── */}
                {
                    activeTab === 'tools' && (
                        <div>
                            <div style={{ marginBottom: '16px' }}>
                                <h3 style={{ marginBottom: '4px' }}>{t('agent.toolMgmt.title')}</h3>
                                <p style={{ fontSize: '13px', color: 'var(--text-tertiary)' }}>{t('agent.toolMgmt.description')}</p>
                            </div>
                            <ToolsManager agentId={id!} canManage={canManage} />
                        </div>
                    )
                }

                {/* ── Skills Tab ── */}
                {
                    activeTab === 'skills' && <AgentSkillsSection agentId={id!} />
                }

                {/* ── Sub-agents Tab (C3, §12.8 fourth capability module) ── */}
                {
                    activeTab === 'subagents' && <AgentSubagentsSection agentId={id!} canManage={canManage} />
                }

                {/* ── Relationships Tab ── */}
                {
                    activeTab === 'relationships' && (
                        <RelationshipEditor agentId={id!} agent={agent} readOnly={(agent as any)?.access_level === 'use'} />
                    )
                }

                {/* ── Workspace Tab ── */}
                {
                    activeTab === 'workspace' && <AgentWorkspaceSection agentId={id!} />
                }

                {
                    activeTab === 'office' && <OfficeWorkbenchSection agentId={id!} />
                }

                {
                    activeTab === 'chat' && (
                        <AgentChatSection
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
                            showHistoryScrollBtn={showHistoryScrollBtn}
                            onScrollHistoryToBottom={scrollHistoryToBottom}
                            chatContainerRef={chatContainerRef}
                            onChatScroll={handleChatScroll}
                            chatMessages={chatMessages}
                            chatMessagesSessionId={chatMessagesSessionId}
                            runtimeSummary={runtimeSummary}
                            transportNotice={transportNotice}
                            isWaiting={isWaiting}
                            activeRunStatus={currentActiveRunState?.status || null}
                            planModeRequested={planModeRequested}
                            onTogglePlanMode={() => setPlanModeRequested((value) => !value)}

                            chatEndRef={chatEndRef}
                            showScrollBtn={showScrollBtn}
                            onScrollToBottom={scrollToBottom}
                            agentExpired={agentExpired}
                            attachedFiles={attachedFiles}
                            onRemoveAttachedFile={(index) => setAttachedFiles((prev) => prev.filter((_, i) => i !== index))}
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
                            onSendMessage={sendChatMessageText}
                            isStreaming={isStreaming}
                            onAbortGeneration={() => {
                                if (!id || !activeSession?.id) return;
                                const activeRuntimeKey = buildSessionRuntimeKey(id, String(activeSession.id));
                                const activeRun = activeRunStateRef.current[activeRuntimeKey];
                                if (activeRun?.runId) {
                                    chatApi.cancelSessionRun(id, String(activeSession.id), activeRun.runId).catch((err) => {
                                        console.warn('Failed to cancel chat run:', err);
                                    });
                                    setActiveRunState(activeRuntimeKey, null);
                                    setIsStreaming(false);
                                    setIsWaiting(false);
                                    setSessionUiState(activeRuntimeKey, { isWaiting: false, isStreaming: false });
                                    return;
                                }
                                const activeSocket = wsMapRef.current[activeRuntimeKey];
                                if (activeSocket?.readyState === WebSocket.OPEN) {
                                    activeSocket.send(JSON.stringify({ type: 'abort' }));
                                }
                            }}
                        />
                    )
                }

                {/* Agent creation success banner (HR Agent flow) */}
                {createdAgentId && activeTab === 'chat' && (
                    <div style={{
                        position: 'fixed', bottom: '24px', left: '50%', transform: 'translateX(-50%)',
                        zIndex: 1000, padding: '16px 24px', borderRadius: '12px',
                        background: 'var(--success-subtle, #f0fdf4)', border: '1px solid var(--success, #22c55e)',
                        boxShadow: '0 4px 24px rgba(0,0,0,0.12)', display: 'flex', alignItems: 'center', gap: '16px',
                    }}>
                        <span style={{ fontSize: '22px' }}>&#x2705;</span>
                        <div>
                            <div style={{ fontWeight: 600, fontSize: '14px' }}>{t('hrChat.created', 'Digital employee created successfully!')}</div>
                            <div style={{ fontSize: '12px', color: 'var(--text-secondary)', marginTop: '2px' }}>{t('hrChat.createdDesc', 'You can now visit the detail page to further customize or start chatting.')}</div>
                        </div>
                        <button className="btn btn-primary" style={{ flexShrink: 0 }} onClick={() => navigate(`/agents/${createdAgentId}`)}>
                            {t('hrChat.goToAgent', 'Go to Detail Page')}
                        </button>
                        <button style={{ background: 'none', border: 'none', cursor: 'pointer', fontSize: '16px', color: 'var(--text-tertiary)', padding: '4px' }} onClick={() => setCreatedAgentId(null)}>&#x2715;</button>
                    </div>
                )}

                {
                    activeTab === 'activityLog' && (
                        <AgentActivityLogSection
                            agentType={(agent as any)?.agent_type}
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
                    activeTab === 'settings' && (agent as any)?.agent_type === 'openclaw' && (
                        <OpenClawSettings agent={agent} agentId={id!} isAdmin={isAdmin} />
                    )
                }
                {
                    activeTab === 'settings' && (agent as any)?.agent_type !== 'openclaw' && (
                        <AgentSettingsSection
                            agentId={id!}
                            agent={agent}
                            llmModels={llmModels}
                            permData={permData}
                            canManage={canManage}
                            canManageCapabilityPolicies={canManageCapabilityPolicies}
                            capabilityDefinitions={capabilityDefinitions}
                            capabilityPolicies={capabilityPolicies}
                            capabilityPolicyLoading={capabilityDefinitionsLoading || capabilityPoliciesLoading}
                            capabilityPolicyError={capabilityPolicyError}
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
                            showDeleteConfirm={showDeleteConfirm}
                            onSetShowDeleteConfirm={setShowDeleteConfirm}
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
                        await fileApi.write(id!, `skills/${value}/SKILL.md`, template);
                        queryClient.invalidateQueries({ queryKey: ['files', id, 'skills'] });
                        setViewingFile(`skills/${value}/SKILL.md`);
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

            {
                uploadToast && (
                    <div style={{
                        position: 'fixed', top: '20px', right: '20px', zIndex: 20000,
                        padding: '12px 20px', borderRadius: '8px',
                        background: uploadToast.type === 'success' ? 'rgba(34, 197, 94, 0.9)' : 'rgba(239, 68, 68, 0.9)',
                        color: '#fff', fontSize: '14px', fontWeight: 500,
                        boxShadow: '0 4px 12px rgba(0,0,0,0.3)',
                    }}>
                        {''}{uploadToast.message}
                    </div>
                )
            }

            {/* ── Expiry Editor Modal (admin only) ── */}
            {
                showExpiryModal && (
                    <div style={{ position: 'fixed', inset: 0, background: 'rgba(0,0,0,0.55)', zIndex: 9000, display: 'flex', alignItems: 'center', justifyContent: 'center' }}
                        onClick={() => setShowExpiryModal(false)}>
                        <div style={{ background: 'var(--bg-secondary)', border: '1px solid var(--border-subtle)', borderRadius: '12px', padding: '24px', width: '360px', maxWidth: '90vw' }}
                            onClick={e => e.stopPropagation()}>
                            <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', marginBottom: '16px' }}>
                                <h3 style={{ margin: 0, fontSize: '15px', fontWeight: 600 }}>⏰ {t('agent.settings.expiry.title')}</h3>
                                <button onClick={() => setShowExpiryModal(false)} style={{ background: 'none', border: 'none', cursor: 'pointer', color: 'var(--text-tertiary)', fontSize: '18px', lineHeight: 1 }}>×</button>
                            </div>
                            <div style={{ fontSize: '12px', color: 'var(--text-tertiary)', marginBottom: '16px' }}>
                                {(agent as any).is_expired
                                    ? <span style={{ color: 'var(--error)', fontWeight: 600 }}>⏰ {t('agent.settings.expiry.expired')}</span>
                                    : (agent as any).expires_at
                                        ? <>{t('agent.settings.expiry.currentExpiry')} <strong>{new Date((agent as any).expires_at).toLocaleString(i18n.language === 'zh' ? 'zh-CN' : 'en-US')}</strong></>
                                        : <span style={{ color: 'var(--success)' }}>{t('agent.settings.expiry.neverExpires')}</span>
                                }
                            </div>
                            <div style={{ marginBottom: '16px' }}>
                                <div style={{ fontSize: '11px', color: 'var(--text-tertiary)', marginBottom: '8px' }}>{t('agent.settings.expiry.quickRenew')}</div>
                                <div style={{ display: 'flex', gap: '6px', flexWrap: 'wrap' }}>
                                    {([
                                        ['+ 24h', 24],
                                        [`+ ${t('agent.settings.expiry.days', { count: 7 })}`, 168],
                                        [`+ ${t('agent.settings.expiry.days', { count: 30 })}`, 720],
                                        [`+ ${t('agent.settings.expiry.days', { count: 90 })}`, 2160],
                                    ] as [string, number][]).map(([label, h]) => (
                                        <button key={h} onClick={() => addHours(h)}
                                            style={{ padding: '4px 10px', borderRadius: '6px', border: '1px solid var(--border-subtle)', background: 'var(--bg-primary)', cursor: 'pointer', fontSize: '12px', color: 'var(--text-primary)' }}>
                                            {label}
                                        </button>
                                    ))}
                                </div>
                            </div>
                            <div style={{ marginBottom: '20px' }}>
                                <div style={{ fontSize: '11px', color: 'var(--text-tertiary)', marginBottom: '6px' }}>{t('agent.settings.expiry.customDeadline')}</div>
                                <input type="datetime-local" value={expiryValue} onChange={e => setExpiryValue(e.target.value)}
                                    style={{ width: '100%', padding: '8px 10px', borderRadius: '8px', border: '1px solid var(--border-subtle)', background: 'var(--bg-primary)', color: 'var(--text-primary)', fontSize: '13px', boxSizing: 'border-box' }} />
                            </div>
                            <div style={{ display: 'flex', gap: '8px', justifyContent: 'space-between', alignItems: 'center' }}>
                                <button onClick={() => saveExpiry(true)} disabled={expirySaving}
                                    style={{ padding: '7px 12px', borderRadius: '8px', border: '1px solid var(--border-subtle)', background: 'none', cursor: 'pointer', fontSize: '12px', color: 'var(--text-secondary)' }}>
                                    🔓 {t('agent.settings.expiry.neverExpires')}
                                </button>
                                <div style={{ display: 'flex', gap: '8px' }}>
                                    <button onClick={() => setShowExpiryModal(false)} disabled={expirySaving}
                                        style={{ padding: '7px 14px', borderRadius: '8px', border: '1px solid var(--border-subtle)', background: 'none', cursor: 'pointer', fontSize: '13px', color: 'var(--text-secondary)' }}>
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
                <div style={{ display: 'flex', flexDirection: 'column', alignItems: 'center', justifyContent: 'center', height: '60vh', gap: '16px' }}>
                    <div style={{ fontSize: '20px', fontWeight: 600, color: 'var(--text-primary)' }}>Something went wrong</div>
                    <div style={{ fontSize: '13px', color: 'var(--text-tertiary)', maxWidth: '400px', textAlign: 'center' }}>
                        {this.state.error?.message || 'An unexpected error occurred while loading this page.'}
                    </div>
                    <button
                        className="btn btn-primary"
                        onClick={() => { this.setState({ hasError: false, error: null }); window.location.reload(); }}
                        style={{ marginTop: '8px' }}
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
