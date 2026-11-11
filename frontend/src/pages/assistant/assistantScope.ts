/** Compare the scope captured before a request with the scope after it returns. */

import { useSyncExternalStore } from 'react';
import { useQuery } from '@tanstack/react-query';
import { assistantApi } from '../../api/domains/assistant';

/** Localize the platform default before the binding request has resolved. */
export function assistantNameForDisplay(name: string, assistantLabel: string): string {
  return name === 'My Assistant' ? assistantLabel : name;
}

/** The stored platform default for a provisioned assistant's description
 * (`backend/app/services/assistant_binding.py` ASSISTANT_ROLE). Display layers
 * map this sentinel to a localized label; stored rows keep the exact bytes. */
export const PLATFORM_ASSISTANT_ROLE = "The user's assistant";

export function assistantRoleForDisplay(role: string, assistantRoleLabel: string): string {
  return role === PLATFORM_ASSISTANT_ROLE ? assistantRoleLabel : role;
}

export function sidebarAgentLink(agent: { id: string; access_level?: string; agent_type?: string }): string {
  return agent.access_level === 'operator' ? `/agents/${agent.id}?manage=true#chat`
    : agent.agent_type === 'local_agent' ? `/agents/${agent.id}` : `/agents/${agent.id}#chat`;
}

export function assistantBindingQuery(userId: string | null, tenantId: string | null, role: string | null) {
  return {
    queryKey: ['assistant', userId || '', tenantId || '', role || ''],
    queryFn: ({ signal }: { signal: AbortSignal }) => assistantApi.get({
      expected_user_id: userId || undefined, expected_tenant_id: tenantId || undefined,
    }, { signal }),
    staleTime: 30_000,
    retry: false as const,
  };
}

export function useAssistantAgentId(user: { id?: string; tenant_id?: string | null; role?: string } | null, company?: string) {
  const homeTenant = useAssistantTenant(user?.role === 'platform_admin' ? null : user?.tenant_id ?? null);
  const tenantId = company ?? homeTenant;
  const { data } = useQuery({
    ...assistantBindingQuery(user?.id || null, tenantId || null, user?.role || null),
    enabled: Boolean(user?.id && tenantId),
  });
  return data?.agent?.id || null;
}

/** Shared by the sidebar and the landing fallback. Keep their ordering identical. */
export function sortSidebarAgents<T extends { id: string; created_at?: string; last_active_at?: string | null }>(
  agents: T[], assistantAgentId?: string | null,
): T[] {
  return [...agents].sort((a, b) => {
    if (assistantAgentId) {
      if (String(a.id) === assistantAgentId) return -1;
      if (String(b.id) === assistantAgentId) return 1;
    }
    const aTime = a.last_active_at ? new Date(a.last_active_at).getTime() : (a.created_at ? new Date(a.created_at).getTime() : 0);
    const bTime = b.last_active_at ? new Date(b.last_active_at).getTime() : (b.created_at ? new Date(b.created_at).getTime() : 0);
    return bTime - aTime;
  });
}

function subscribeCompany(onChange: () => void) {
  const changed = (event: StorageEvent) => {
    if (event.key === 'current_tenant_id' || event.key === null) onChange();
  };
  window.addEventListener('storage', changed);
  return () => window.removeEventListener('storage', changed);
}

export function useAssistantTenant(homeTenant: string | null) {
  return useSyncExternalStore(
    subscribeCompany,
    () => localStorage.getItem('current_tenant_id') || homeTenant || '',
    () => homeTenant || '',
  );
}

export interface AssistantScopeSnapshot {
  startedUserId: string | null;
  startedTenantId: string | null;
  currentUserId: string | null;
  currentTenantId: string | null;
  payloadUserId?: string | null;
  payloadTenantId?: string | null;
}

export function assistantScopeStillCurrent(input: AssistantScopeSnapshot): boolean {
  if (!input.startedUserId || !input.startedTenantId) return false;
  if (input.currentUserId !== input.startedUserId) return false;
  if (input.currentTenantId !== input.startedTenantId) return false;
  if (input.payloadUserId && input.payloadUserId !== input.startedUserId) return false;
  if (input.payloadTenantId && input.payloadTenantId !== input.startedTenantId) return false;
  return true;
}

export function entryShouldNavigate(entry: { kind?: string; path?: string } | null | undefined): boolean {
  if (!entry) return false;
  if (entry.kind !== 'session' && entry.kind !== 'employee') return false;
  return typeof entry.path === 'string' && entry.path.startsWith('/agents/');
}
