import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { Navigate, useSearchParams } from 'react-router-dom';
import { useTranslation } from 'react-i18next';
import { ApiError } from '../../api/core';
import { assistantApi, type AssistantBinding, type AssistantScopeEcho } from '../../api/domains/assistant';
import { agentApi } from '../../api/domains/agents';
import { useAuthStore } from '../../stores';
import AssistantPanel from './AssistantPanel';
import { assistantScopeStillCurrent, entryShouldNavigate, sortSidebarAgents, useAssistantTenant } from './assistantScope';
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
  void mode;
  return <ScopedAssistantEntry key={`${scope.userId}|${scope.tenantId}|${scope.role}`} scope={scope} />;
}

function ScopedAssistantEntry({ scope }: { scope: Scope }) {
  const { t } = useTranslation();
  const [params] = useSearchParams();
  const settings = params.get('settings') === 'true';
  const queryClient = useQueryClient();
  const bindingKey = ['assistant', scope.userId || '', scope.tenantId || '', scope.role || ''];
  // Entry owns automatic provisioning; the sidebar's read must not win query deduplication.
  const queryKey = ['assistant-entry', ...bindingKey.slice(1), settings];
  const query = useQuery({
    queryKey,
    queryFn: async () => {
      let result = await assistantApi.get(echo(scope));
      if (!stillCurrent(scope, result)) throw new ApiError(409, 'scope changed', { state: 'scope_changed' });
      if (!settings && ['unbound', 'provision_incomplete'].includes(result.state)) {
        result = await assistantApi.open(echo(scope));
        if (!stillCurrent(scope, result)) throw new ApiError(409, 'scope changed', { state: 'scope_changed' });
        if (result.created) void queryClient.invalidateQueries({ queryKey: ['agents', scope.tenantId] });
      }
      queryClient.setQueryData(bindingKey, result);
      return result;
    },
    enabled: Boolean(scope.userId),
    retry: false,
  });
  const payload = query.data && stillCurrent(scope, query.data) ? query.data : null;
  const hasConversation = payload && (entryShouldNavigate(payload.entry) || (payload.state === 'active' && payload.agent?.id));
  const fallback = useQuery({
    queryKey: ['agents', scope.tenantId || ''],
    queryFn: () => agentApi.list(scope.tenantId || undefined),
    enabled: !settings && !query.isPending && !query.isFetching && !hasConversation && Boolean(scope.userId && scope.tenantId),
    staleTime: 30_000,
    retry: false,
  });

  const saveBinding = useMutation({
    mutationFn: (operation: () => Promise<AssistantBinding>) => operation(),
    onSuccess: (next) => {
      if (!stillCurrent(scope, next)) return;
      queryClient.setQueryData(queryKey, next);
      queryClient.setQueryData(bindingKey, next);
    },
  });

  if (!query.isFetching && payload) {
    // B5 resolves the latest conversation for every bot, including empty drafts.
    if ((!settings || payload.applied) && hasConversation && payload.agent?.id) {
      return <Navigate to={`/agents/${payload.agent.id}#chat`} replace />;
    }
  }

  if (!settings) {
    const first = sortSidebarAgents(fallback.data || [], payload?.agent?.id)
      .find((agent) => agent.id !== payload?.agent?.id);
    if (!query.isFetching && stillCurrent(scope) && first) return <Navigate to={`/agents/${first.id}#chat`} replace />;
    return <p className="assistant-entry" role="status">{t(query.isPending || query.isFetching || fallback.isFetching
      ? 'assistant.loading' : 'assistant.setupByAdmin')}</p>;
  }

  return <AssistantPanel
    payload={payload}
    loading={query.isLoading}
    errorState={query.error instanceof ApiError ? String((query.error.data as { state?: string })?.state || 'unavailable')
      : query.error ? 'unavailable' : null}
    actionError={saveBinding.error?.message || (saveBinding.data?.rejection ? t('assistant.inaccessible') : null)}
    busy={saveBinding.isPending}
    onOpen={(replace) => saveBinding.mutate(() => assistantApi.open({ ...echo(scope), replace_unavailable: replace }))}
    onSelect={(agentId) => saveBinding.mutate(() => assistantApi.select({ ...echo(scope), agent_id: agentId }))}
    onRetry={() => {
      if (payload?.state === 'provision_incomplete') {
        saveBinding.mutate(() => assistantApi.open(echo(scope)));
      } else {
        saveBinding.reset();
        void query.refetch();
      }
    }}
  />;
}
