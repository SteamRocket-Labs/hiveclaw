import React from 'react';
import { useQuery, type QueryClient } from '@tanstack/react-query';

import { ApiError } from '../../api/core';
import { activityApi } from '../../api/domains/activity';
import { agentApi } from '../../api/domains/agents';
import { chatApi } from '../../api/domains/chat';

interface UseOperatorAuthorityOptions {
  agentId?: string;
  queryClient: QueryClient;
}

export function useOperatorAuthorityLifecycle({ agentId, queryClient }: UseOperatorAuthorityOptions) {
  const { data: agent, dataUpdatedAt: agentDataUpdatedAt = 0, isLoading, error: agentError } = useQuery({
    queryKey: ['agent', agentId],
    queryFn: () => agentApi.getById(agentId!),
    enabled: Boolean(agentId),
    retry: false,
    refetchOnWindowFocus: true,
    refetchInterval: (query) => (
      (query.state.data as { access_level?: unknown } | undefined)?.access_level === 'operator'
        ? 30_000
        : false
    ),
  });
  const [operatorAuthorityDenied, setOperatorAuthorityDenied] = React.useState(false);
  const operatorAuthorityDeniedAtRef = React.useRef(0);
  const [operatorReason, setOperatorReason] = React.useState('');

  React.useEffect(() => {
    setOperatorAuthorityDenied(false);
    setOperatorReason('');
  }, [agentId]);
  React.useEffect(() => {
    if (
      operatorAuthorityDenied
      && !agentError
      && agentDataUpdatedAt > operatorAuthorityDeniedAtRef.current
    ) {
      setOperatorAuthorityDenied(false);
    }
  }, [agentDataUpdatedAt, agentError, operatorAuthorityDenied]);

  const agentShellAuthorityLost = isAgentShellAuthorityLoss(agentError);
  const effectiveAgentAuthorityLost = agentShellAuthorityLost || operatorAuthorityDenied;
  const canLoadAgentScopedData = Boolean(agentId && agent && !effectiveAgentAuthorityLost);
  const canManage = !effectiveAgentAuthorityLost && (agent as any)?.action_capabilities?.can_manage === true;
  const canManagePermissions = !effectiveAgentAuthorityLost
    && (agent as any)?.action_capabilities?.can_manage_permissions === true;
  const canOperatorInspect = !effectiveAgentAuthorityLost
    && (agent as any)?.action_capabilities?.can_operator_inspect === true;
  const isOperatorOnly = !effectiveAgentAuthorityLost && (agent as any)?.access_level === 'operator';
  const normalizedOperatorReason = operatorReason.trim();
  const operatorAuthorityScope = canLoadAgentScopedData
    && canOperatorInspect
    && normalizedOperatorReason
    && agentId
    ? `${agentId}\u0000${normalizedOperatorReason}`
    : null;
  const operatorAuthorityScopeRef = React.useRef<string | null>(operatorAuthorityScope);
  operatorAuthorityScopeRef.current = operatorAuthorityScope;
  const isOperatorOnlyRef = React.useRef(isOperatorOnly);
  isOperatorOnlyRef.current = isOperatorOnly;

  const denyOperatorAuthority = React.useCallback((error: unknown): boolean => {
    if (!isOperatorAuthorityLoss(error)) return false;
    operatorAuthorityDeniedAtRef.current = agentDataUpdatedAt;
    setOperatorAuthorityDenied(true);
    if (agentId) void queryClient.invalidateQueries({ queryKey: ['agent', agentId] });
    return true;
  }, [agentDataUpdatedAt, agentId, queryClient]);

  return {
    agent,
    agentError,
    canLoadAgentScopedData,
    canManage,
    canManagePermissions,
    canOperatorInspect,
    denyOperatorAuthority,
    effectiveAgentAuthorityLost,
    isLoading,
    isOperatorOnly,
    isOperatorOnlyRef,
    normalizedOperatorReason,
    operatorAuthorityScope,
    operatorAuthorityScopeRef,
    operatorReason,
    setOperatorReason,
  };
}

export const operatorSessionRequestOptions = (operatorView: boolean, operatorReason: string) =>
  operatorView ? { operatorView: true as const, operatorReason: operatorReason.trim() } : undefined;

interface UseOperatorInspectionDataOptions {
  activeTab: string;
  agentId?: string;
  canLoadAgentScopedData: boolean;
  canOperatorInspect: boolean;
  denyOperatorAuthority: (error: unknown) => boolean;
  isOperatorOnly: boolean;
  normalizedOperatorReason: string;
  operatorAuthorityScope: string | null;
  operatorAuthorityScopeRef: React.MutableRefObject<string | null>;
}

interface OperatorAuthorityCacheLifecycleOptions {
  activeSession: any;
  agentId?: string;
  normalizedOperatorReason: string;
  operatorAuthorityScope: string | null;
  queryClient: QueryClient;
  refs: Record<string, React.MutableRefObject<any>>;
  closeSessionSocket: (key: string, dispose?: boolean) => void;
  resetActiveTransportState: () => void;
  sessionMessageStore: {
    clearAll: () => void;
    clearSession: (sessionId: string) => void;
  };
  clearChatMessages: () => void;
  clearHistoryMessages: () => void;
  setters: Record<string, (value: any) => void>;
}

export function useOperatorInspectionData({
  activeTab,
  agentId,
  canLoadAgentScopedData,
  canOperatorInspect,
  denyOperatorAuthority,
  isOperatorOnly,
  normalizedOperatorReason,
  operatorAuthorityScope,
  operatorAuthorityScopeRef,
}: UseOperatorInspectionDataOptions) {
  const [expandedReflection, setExpandedReflection] = React.useState<string | null>(null);
  const [reflectionMessages, setReflectionMessages] = React.useState<Record<string, any[]>>({});
  const [showAllTriggers, setShowAllTriggers] = React.useState(false);
  const [reflectionPage, setReflectionPage] = React.useState(0);
  const [activityOperatorView, setActivityOperatorView] = React.useState(false);
  const previousAuthorityScopeRef = React.useRef(operatorAuthorityScope);
  const effectiveActivityOperatorView = isOperatorOnly
    ? Boolean(operatorAuthorityScope)
    : activityOperatorView && Boolean(operatorAuthorityScope);
  const operatorOnlyActivityReady = !isOperatorOnly || Boolean(operatorAuthorityScope);

  React.useEffect(() => {
    const previousScope = previousAuthorityScopeRef.current;
    previousAuthorityScopeRef.current = operatorAuthorityScope;
    setActivityOperatorView(false);
    if (!previousScope || previousScope === operatorAuthorityScope) return;
    setExpandedReflection(null);
    setReflectionMessages({});
    setReflectionPage(0);
  }, [operatorAuthorityScope]);

  const { data: reflectionSessions = [] } = useQuery({
    queryKey: ['reflection-sessions', agentId, normalizedOperatorReason],
    queryFn: async () => {
      const authorityScope = operatorAuthorityScope;
      if (!authorityScope) return [];
      try {
        const sessions = await chatApi.listSessions(agentId!, 'all', {
          operatorView: true,
          operatorReason: normalizedOperatorReason,
        });
        if (operatorAuthorityScopeRef.current !== authorityScope) return [];
        return sessions.filter((session: any) => session.source_channel === 'trigger');
      } catch (error) {
        denyOperatorAuthority(error);
        return [];
      }
    },
    enabled: canLoadAgentScopedData
      && canOperatorInspect
      && Boolean(normalizedOperatorReason)
      && activeTab === 'aware',
    refetchInterval: activeTab === 'aware' ? 10_000 : false,
  });
  const { data: activityLogs = [], error: activityError } = useQuery({
    queryKey: ['activity', agentId, effectiveActivityOperatorView ? normalizedOperatorReason : 'owner'],
    queryFn: () => activityApi.list(
      agentId!,
      100,
      effectiveActivityOperatorView ? { operatorView: true, reason: normalizedOperatorReason } : undefined,
    ),
    enabled: canLoadAgentScopedData
      && operatorOnlyActivityReady
      && (activeTab === 'activityLog' || activeTab === 'status'),
    refetchInterval: activeTab === 'activityLog' ? 10_000 : false,
  });
  const { data: toolFailureSummary, error: toolFailureError } = useQuery({
    queryKey: ['activity', 'tool-failures', agentId, effectiveActivityOperatorView ? normalizedOperatorReason : 'owner'],
    queryFn: () => activityApi.getToolFailureSummary(
      agentId!,
      24,
      200,
      effectiveActivityOperatorView ? { operatorView: true, reason: normalizedOperatorReason } : undefined,
    ),
    enabled: canLoadAgentScopedData && operatorOnlyActivityReady && activeTab === 'activityLog',
    refetchInterval: activeTab === 'activityLog' ? 10_000 : false,
  });

  React.useEffect(() => {
    if (!effectiveActivityOperatorView) return;
    denyOperatorAuthority(activityError);
    denyOperatorAuthority(toolFailureError);
  }, [activityError, denyOperatorAuthority, effectiveActivityOperatorView, toolFailureError]);

  const loadReflectionMessages = React.useCallback(async (sessionId: string) => {
    const authorityScope = operatorAuthorityScopeRef.current;
    if (!agentId || !authorityScope) return undefined;
    try {
      const messages = await chatApi.getSessionMessages(agentId, sessionId, {
        operatorView: true,
        operatorReason: normalizedOperatorReason,
      });
      return operatorAuthorityScopeRef.current === authorityScope ? messages : undefined;
    } catch (error) {
      denyOperatorAuthority(error);
      return undefined;
    }
  }, [agentId, denyOperatorAuthority, normalizedOperatorReason, operatorAuthorityScopeRef]);

  return {
    activityLogs,
    activityOperatorView,
    effectiveActivityOperatorView,
    expandedReflection,
    loadReflectionMessages,
    reflectionMessages,
    reflectionPage,
    reflectionSessions,
    setActivityOperatorView,
    setExpandedReflection,
    setReflectionMessages,
    setReflectionPage,
    setShowAllTriggers,
    showAllTriggers,
    toolFailureSummary,
  };
}

export function useOperatorAuthorityCacheLifecycle({
  activeSession,
  agentId,
  normalizedOperatorReason,
  operatorAuthorityScope,
  queryClient,
  refs,
  closeSessionSocket,
  resetActiveTransportState,
  sessionMessageStore,
  clearChatMessages,
  clearHistoryMessages,
  setters,
}: OperatorAuthorityCacheLifecycleOptions) {
  const previousAuthorityRef = React.useRef({
    scope: operatorAuthorityScope,
    agentId,
    reason: normalizedOperatorReason,
  });

  React.useEffect(() => {
    const previous = previousAuthorityRef.current;
    previousAuthorityRef.current = {
      scope: operatorAuthorityScope,
      agentId,
      reason: normalizedOperatorReason,
    };
    if (!previous.scope || previous.scope === operatorAuthorityScope) return;

    const operatorQueryPrefixes = new Set([
      'reflection-sessions',
      'activity',
      'chat-runtime-summary',
      'chat-active-run',
      'chat-session-work-ledger',
      'chat-work-ledger',
      'chat-session-workbench',
      'chat-session-index',
      'chat-session-decisions',
      'chat-session-context-usage',
    ]);
    const operatorQueryPredicate = (query: any) => {
      const queryKey = Array.isArray(query?.queryKey) ? query.queryKey : [];
      return operatorQueryPrefixes.has(String(queryKey[0] || ''))
        && queryKey.some((part: unknown) => String(part) === String(previous.agentId || ''))
        && (queryKey.includes('operator') || queryKey.includes(previous.reason));
    };
    void queryClient.cancelQueries({ predicate: operatorQueryPredicate });
    queryClient.removeQueries({ predicate: operatorQueryPredicate });

    const operatorSessionWasActive = activeSession?.operator_view === true
      && refs.activeOperatorAuthorityScope.current === previous.scope;
    if (operatorSessionWasActive) {
      refs.sessionMsgAbort.current?.abort();
      refs.sessionLoadSeq.current += 1;
      refs.sessionTranscriptLoad.current = null;
      refs.activeSessionId.current = null;
      refs.activeOperatorAuthorityScope.current = null;
      setters.setActiveSession(null);
      setters.setChatMessagesSessionId(null);
      setters.setHistoryMessagesSessionId(null);
      clearHistoryMessages();
      setters.setTransportNotice(null);
      setters.setIsStreaming(false);
      setters.setIsWaiting(false);
      resetActiveTransportState();
    }
    refs.operatorRuntimeKeys.current.forEach((key: string) => {
      closeSessionSocket(key, true);
      const separator = key.indexOf(':');
      sessionMessageStore.clearSession(separator >= 0 ? key.slice(separator + 1) : key);
      delete refs.sessionUiState.current[key];
      delete refs.transcriptReplayState.current[key];
      delete refs.sessionCompatibilityTimelines.current[key];
      delete refs.sessionVisibilityBoundaries.current[key];
      delete refs.transcriptEvents.current[key];
      delete refs.sessionEventStores.current[key];
      refs.sessionEventFullHydrationKeys.current.delete(key);
      delete refs.transcriptBackfillInFlight.current[key];
      delete refs.toolCallInvalidateAt.current[key];
      delete refs.activeRunState.current[key];
      delete refs.pendingUserMessages.current[key];
      delete refs.runtimeActivityAt.current[key];
      refs.locallyTerminalSessionKeys.current.delete(key);
    });
    refs.operatorRuntimeKeys.current.clear();
    setters.setActiveRunStateBySession({ ...refs.activeRunState.current });
    refs.allSessionsAuthorityScope.current = null;
    setters.setAllSessions([]);
    setters.setAllSessionsLoading(false);
    setters.setBranchLineage([]);
    setters.setBranchLineageLoading(false);
    setters.setChatScope('mine');
  }, [operatorAuthorityScope]);

  React.useEffect(() => {
    refs.currentAgentId.current = agentId;
  }, [agentId]);

  React.useEffect(() => {
    refs.sessionMsgAbort.current?.abort();
    refs.sessionLoadSeq.current += 1;
    refs.sessionTranscriptLoad.current = null;
    refs.activeSessionId.current = null;
    refs.activeOperatorAuthorityScope.current = null;
    refs.allSessionsAuthorityScope.current = null;
    setters.setActiveSession(null);
    setters.setSessions([]);
    setters.setAllSessions([]);
    setters.setAllSessionsLoading(false);
    setters.setBranchLineage([]);
    setters.setBranchLineageLoading(false);
    clearChatMessages();
    sessionMessageStore.clearAll();
    setters.setChatMessagesSessionId(null);
    clearHistoryMessages();
    setters.setHistoryMessagesSessionId(null);
    setters.setTransportNotice(null);
    setters.setIsStreaming(false);
    setters.setIsWaiting(false);
    resetActiveTransportState();
    refs.operatorRuntimeKeys.current.forEach((key: string) => closeSessionSocket(key, true));
    refs.operatorRuntimeKeys.current.clear();
    refs.sessionUiState.current = {};
    refs.transcriptReplayState.current = {};
    refs.sessionCompatibilityTimelines.current = {};
    refs.sessionVisibilityBoundaries.current = {};
    refs.transcriptEvents.current = {};
    refs.sessionEventStores.current = {};
    refs.sessionEventFullHydrationKeys.current = new Set();
    refs.transcriptBackfillInFlight.current = {};
    refs.toolCallInvalidateAt.current = {};
    refs.activeRunState.current = {};
    refs.pendingUserMessages.current = {};
    refs.runtimeActivityAt.current = {};
    setters.setActiveRunStateBySession({});
    setters.setChatScope('mine');
    setters.setAgentExpired(false);
    setters.setSessionAccessError(null);
    refs.settingsInit.current = false;
  }, [agentId]);

  React.useEffect(() => () => {
    refs.sessionMsgAbort.current?.abort();
  }, []);
}

function apiErrorStatus(error: unknown): number | null {
  if (error instanceof ApiError) return error.status;
  if (!error || typeof error !== 'object') return null;
  const status = Number((error as { status?: unknown }).status);
  return Number.isFinite(status) ? status : null;
}

function isAgentShellAuthorityLoss(error: unknown): boolean {
  const status = apiErrorStatus(error);
  return status === 403 || status === 410;
}

function isOperatorAuthorityLoss(error: unknown): boolean {
  return apiErrorStatus(error) === 403;
}
