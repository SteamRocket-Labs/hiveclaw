import { useRef, useState } from 'react';
import { useQuery, useQueryClient, type QueryClient } from '@tanstack/react-query';
import { Navigate, useSearchParams } from 'react-router-dom';
import { useTranslation } from 'react-i18next';
import { ApiError } from '../../api/core';
import { assistantApi, type AssistantBinding, type AssistantScopeEcho } from '../../api/domains/assistant';
import { agentApi } from '../../api/domains/agents';
import { useAuthStore } from '../../stores';
import AssistantPanel from './AssistantPanel';
import { assistantBindingQuery, assistantScopeStillCurrent, entryShouldNavigate, sidebarAgentLink, sortSidebarAgents, useAssistantTenant } from './assistantScope';
import './assistant.css';

function currentScope() {
  const user = useAuthStore.getState().user;
  return {
    userId: user?.id ?? null,
    tenantId: localStorage.getItem('current_tenant_id') || (user?.role === 'platform_admin' ? null : user?.tenant_id) || null,
    role: user?.role ?? null,
  };
}

type Scope = ReturnType<typeof currentScope>;

// Deduplicate automatic opens across remounts; cancelled attempts stay recorded.
const automaticOpens = new WeakMap<QueryClient, Set<string>>();

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
  const tenantId = useAssistantTenant(user?.role === 'platform_admin' ? null : user?.tenant_id ?? null);
  const scope = { userId: user?.id ?? null, tenantId: tenantId || null, role: user?.role ?? null };
  void mode;
  return <ScopedAssistantEntry key={`${scope.userId}|${scope.tenantId}|${scope.role}`} scope={scope} />;
}

function ScopedAssistantEntry({ scope }: { scope: Scope }) {
  const { t } = useTranslation();
  const [params] = useSearchParams();
  const settings = params.get('settings') === 'true';
  const queryClient = useQueryClient();
  const [pending, setPending] = useState(false);
  const [actionError, setActionError] = useState<string | null>(null);
  const actionLock = useRef(false);
  const bindingQuery = assistantBindingQuery(scope.userId, scope.tenantId, scope.role);
  const bindingKey = bindingQuery.queryKey;
  // Share the sidebar read; entry alone owns automatic provisioning.
  const queryKey = ['assistant-entry', ...bindingKey.slice(1), settings];
  const query = useQuery({
    queryKey,
    queryFn: async ({ signal }) => {
      let result = await queryClient.fetchQuery({ ...bindingQuery, staleTime: 0 });
      signal.throwIfAborted();
      if (!stillCurrent(scope, result)) throw new ApiError(409, 'scope changed', { state: 'scope_changed' });
      const attempted = automaticOpens.get(queryClient) || new Set<string>();
      automaticOpens.set(queryClient, attempted);
      const scopeKey = JSON.stringify(bindingKey);
      if (!settings && ['member', 'org_admin'].includes(scope.role || '')
        && ['unbound', 'provision_incomplete'].includes(result.state) && !attempted.has(scopeKey)) {
        attempted.add(scopeKey);
        try {
          result = await assistantApi.open(echo(scope), { signal });
          signal.throwIfAborted();
        } catch (error) {
          if (signal.aborted) queryClient.removeQueries({ queryKey: bindingKey, exact: true, type: 'inactive' });
          else attempted.delete(scopeKey);
          throw error;
        }
        if (!stillCurrent(scope, result)) throw new ApiError(409, 'scope changed', { state: 'scope_changed' });
        // Drop a sidebar GET started before this write before publishing the binding.
        await queryClient.cancelQueries({ queryKey: bindingKey, exact: true });
        signal.throwIfAborted();
        if (result.created) void queryClient.invalidateQueries({ queryKey: ['agents', scope.tenantId] });
      }
      queryClient.setQueryData(bindingKey, result);
      return result;
    },
    enabled: Boolean(scope.userId),
    retry: false,
  });
  const payload = query.data && stillCurrent(scope, query.data) ? query.data : null;
  const errorState = query.error instanceof ApiError
    ? String((query.error.data as { state?: string } | null)?.state || 'unavailable')
    : query.error ? 'unavailable' : null;
  const hasConversation = payload && (entryShouldNavigate(payload.entry) || (payload.state === 'active' && payload.agent?.id));
  const fallback = useQuery({
    queryKey: ['agents', scope.tenantId || ''],
    queryFn: () => agentApi.list(scope.tenantId || undefined),
    enabled: !settings && query.isSuccess && !query.isFetching && !hasConversation && Boolean(scope.userId && scope.tenantId),
    staleTime: 30_000,
    retry: false,
  });

  async function saveBinding(operation: () => Promise<AssistantBinding>) {
    if (actionLock.current) return;
    actionLock.current = true;
    setPending(true);
    setActionError(null);
    try {
      const next = await operation();
      if (!stillCurrent(scope, next)) return;
      queryClient.setQueryData(queryKey, next);
      queryClient.setQueryData(bindingKey, next);
      if (next.rejection) setActionError(t('assistant.inaccessible'));
    } catch (error) {
      if (stillCurrent(scope)) setActionError(error instanceof Error ? error.message : t('assistant.actionFailed'));
    } finally {
      actionLock.current = false;
      setPending(false);
    }
  }

  if (query.isSuccess && !query.isFetching && payload) {
    // B5 resolves the latest conversation for every bot, including empty drafts.
    if ((!settings || payload.applied) && hasConversation && payload.agent?.id) {
      return <Navigate to={`/agents/${payload.agent.id}#chat`} replace />;
    }
  }

  if (!settings) {
    if (!scope.tenantId || errorState === 'company_required' || payload?.state === 'company_required') {
      return <p className="assistant-entry" role="status">{t('assistant.companyRequired')}</p>;
    }
    if (query.isError) return <div className="assistant-entry">
      <p role="status">{t('assistant.connectionFailed')}</p>
      <button className="btn btn-secondary" onClick={() => { void query.refetch(); }}>{t('assistant.retry')}</button>
    </div>;
    const first = fallback.isSuccess && !fallback.isFetching
      ? sortSidebarAgents(fallback.data, payload?.agent?.id)[0] : null;
    if (query.isSuccess && !query.isFetching && stillCurrent(scope) && first) return <Navigate to={sidebarAgentLink(first)} replace />;
    return <p className="assistant-entry" role="status">{t(query.isPending || query.isFetching || fallback.isFetching
      ? 'assistant.loading' : 'assistant.setupByAdmin')}</p>;
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
