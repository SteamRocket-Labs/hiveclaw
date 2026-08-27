import type { AgentChatMessage } from './chatRuntime';
import type { SessionCommandCheckpoint, SessionPermissionMode } from './AgentChatSection';

// P8 IA (docs/agent-memory-md-first-spec.md §10): Knowledge remains the
// primary memory view. External capability governance is exposed through a
// single agent-level Extensions tab, while MCP/tools, Skills, and Sub-agents
// keep their existing runtime modules behind that entry.
export const AGENT_DETAIL_TABS = ['status', 'aware', 'knowledge', 'evolution', 'extensions', 'a2a', 'workspace', 'workflows', 'chat', 'tasks', 'activityLog', 'approvals', 'settings'] as const;

export type AgentDetailTab = typeof AGENT_DETAIL_TABS[number];

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

export function normalizeSessionPermissionModeValue(value: unknown): SessionPermissionMode | null {
    if (value === 'default' || value === 'auto' || value === 'bypassPermissions') {
        return value;
    }
    return null;
}

export const DEFAULT_SESSION_PERMISSION_MODE: SessionPermissionMode = 'default';

export function defaultSessionPermissionModeFromAgent(
    agent: unknown,
): SessionPermissionMode {
    const agentRecord = objectValue(agent);
    return normalizeSessionPermissionModeValue(agentRecord.default_session_permission_mode)
        || DEFAULT_SESSION_PERMISSION_MODE;
}

// B4 transcript windowing: first screen loads a slim newest window; older
// history pages in on demand.
export function objectValue(value: unknown): Record<string, unknown> {
    return value && typeof value === 'object' ? value as Record<string, unknown> : {};
}

export function stringValueFromUnknown(value: unknown): string {
    return typeof value === 'string' ? value.trim() : '';
}

export function normalizeSessionCommandCheckpoints(value: unknown): SessionCommandCheckpoint[] {
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
}

export function draftContentFromCheckpointPayload(actionResult: Record<string, unknown> | null, uiAction?: Record<string, unknown>): string {
    const checkpoint = objectValue(actionResult?.checkpoint);
    return (
        stringValueFromUnknown(uiAction?.draft_content)
        || stringValueFromUnknown(actionResult?.draft_content)
        || stringValueFromUnknown(checkpoint.content)
    );
}

export function checkpointEventIdFromPayload(actionResult: Record<string, unknown> | null, uiAction?: Record<string, unknown>): string {
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

export function rewindMarkerMessage(checkpointEventId: string): AgentChatMessage {
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

export function branchDraftContent(response: { branch?: Record<string, unknown> } | null | undefined, fallback: string): string {
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

export function withSessionPermissionMode<T extends Record<string, unknown>>(
    session: T,
    mode: SessionPermissionMode,
): T {
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
    { tabs: ['status', 'chat', 'tasks'] },
    { tabs: ['aware', 'knowledge', 'evolution', 'extensions'] },
    { tabs: ['workspace', 'workflows', 'a2a', 'activityLog', 'approvals'] },
    { tabs: ['settings'] },
];

export const AGENT_TAB_LABELS: Record<AgentDetailTab, string> = {
    status: 'Status',
    aware: 'Awareness',
    knowledge: 'Knowledge',
    evolution: 'Evolution',
    extensions: 'Extensions',
    a2a: 'A2A',
    workspace: 'Workspace',
    workflows: 'Workflows',
    chat: 'Chat',
    tasks: 'Assignments',
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
        tabs: ['chat', 'tasks', 'aware'],
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
        tabs: ['workspace'],
    },
    {
        id: 'permissions',
        labelKey: 'agent.workbench.permissions',
        fallback: 'Permissions & Settings',
        primaryTab: 'approvals',
        tabs: ['approvals', 'settings'],
    },
];

export function isAgentDetailTabVisible(agent: any, tab: AgentDetailTab): boolean {
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
