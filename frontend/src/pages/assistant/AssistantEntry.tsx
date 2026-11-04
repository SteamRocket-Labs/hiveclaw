import { useRef, useState } from 'react';
import { useQuery, useQueryClient } from '@tanstack/react-query';
import { Navigate } from 'react-router-dom';
import { useTranslation } from 'react-i18next';
import { ApiError } from '../../api/core';
import { assistantApi, type AssistantBinding, type AssistantScopeEcho } from '../../api/domains/assistant';
import { useAuthStore } from '../../stores';
import AssistantPanel from './AssistantPanel';
import { assistantScopeStillCurrent, entryShouldNavigate, useAssistantTenant } from './assistantScope';
import './assistant.css';

function currentScope() {
  const user = useAuthStore.getState().user;
  return {
    userId: user?.id ?? null,
    tenantId: localStorage.getItem('current_tenant_id') || user?.tenant_id || null,
    role: user?.role ?? null,
  };
}

type Scope = ReturnType<typeof currentScope>;

function echo(scope: Scope): AssistantScopeEcho {
  return { expected_user_id: scope.userId || undefined, expected_tenant_id: scope.tenantId || undefined };
}

function stillCurrent(started: Scope, payload?: AssistantBinding) {
  const current = currentScope();
  return started.role === current.role && assistantScopeStillCurrent({
    startedUserId: started.userId,
    startedTenantId: started.tenantId,
    currentUserId: current.userId,
    currentTenantId: current.tenantId,
    payloadUserId: payload?.scope.user_id,
    payloadTenantId: payload?.scope.tenant_id,
  });
}

export default function AssistantEntry({ mode }: { mode: 'entry' | 'assistant' }) {
  const user = useAuthStore((state) => state.user);
  const tenantId = useAssistantTenant(user?.tenant_id ?? null);
  const scope = { userId: user?.id ?? null, tenantId: tenantId || null, role: user?.role ?? null };
  // Both "/" and "/assistant" land in the same place: the latest My Assistant
  // conversation, or the minimal open/retry panel when none can open.
  void mode;
  return <ScopedAssistantEntry key={`${scope.userId}|${scope.tenantId}|${scope.role}`} scope={scope} />;
}

function ScopedAssistantEntry({ scope }: { scope: Scope }) {
  const { t } = useTranslation();
  const queryClient = useQueryClient();
  const [pending, setPending] = useState(false);
  const [actionError, setActionError] = useState<string | null>(null);
  const actionLock = useRef(false);
  const queryKey = ['assistant', scope.userId || '', scope.tenantId || '', scope.role || ''];
  const query = useQuery({
    queryKey,
    queryFn: async () => {
      const result = await assistantApi.get(echo(scope));
      if (!stillCurrent(scope, result)) throw new ApiError(409, 'scope changed', { state: 'scope_changed' });
      return result;
    },
    enabled: Boolean(scope.userId),
    retry: false,
  });
  const payload = query.data && stillCurrent(scope, query.data) ? query.data : null;
  const errorState = query.error instanceof ApiError && query.error.data && typeof query.error.data === 'object'
    ? String((query.error.data as { state?: string }).state || 'unavailable')
    : query.error ? 'unavailable' : null;

  async function saveBinding(operation: () => Promise<AssistantBinding>) {
    if (actionLock.current) return;
    actionLock.current = true;
    setPending(true);
    setActionError(null);
    try {
      const next = await operation();
      if (!stillCurrent(scope, next)) return;
      queryClient.setQueryData(queryKey, next);
      if (next.rejection) setActionError(t('assistant.inaccessible'));
    } catch (error) {
      if (stillCurrent(scope)) setActionError(error instanceof Error ? error.message : t('assistant.actionFailed'));
    } finally {
      actionLock.current = false;
      setPending(false);
    }
  }

  if (!query.isFetching && payload) {
    // The app opens straight into the latest conversation with My Assistant
    // (product definition P3). When no conversation is bound yet, open the
    // assistant's own chat page; only a missing/ineligible assistant falls
    // through to the minimal open/retry panel.
    if (entryShouldNavigate(payload.entry)) {
      return <Navigate to={payload.entry.path} replace />;
    }
    if (payload.state === 'active' && payload.agent?.id) {
      // No conversation bound yet: open the bot's conversation (a fresh one
      // opens there), not its management page (B5).
      return <Navigate to={`/agents/${payload.agent.id}#chat`} replace />;
    }
  }

  return <AssistantPanel
    payload={payload}
    loading={query.isLoading}
    errorState={errorState}
    actionError={actionError}
    busy={pending}
    onOpen={(replace) => { void saveBinding(() => assistantApi.open({ ...echo(scope), replace_unavailable: replace })); }}
    onSelect={(agentId) => { void saveBinding(() => assistantApi.select({ ...echo(scope), agent_id: agentId })); }}
    onRetry={() => {
      if (payload?.state === 'provision_incomplete') {
        void saveBinding(() => assistantApi.open(echo(scope)));
      } else {
        setActionError(null);
        void query.refetch();
      }
    }}
  />;
}
