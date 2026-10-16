/** Compare the scope captured before a request with the scope after it returns. */

import { useSyncExternalStore } from 'react';

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
