import { useEffect, useState } from 'react';
import { useAuthStore } from '../stores';

export function useCompanyChannelScope(selectedTenant?: string) {
  const user = useAuthStore((state) => state.user);
  const [generation, setGeneration] = useState(0);
  useEffect(() => {
    const changed = (event: StorageEvent) => { if (!event.key || event.key === 'current_tenant_id') setGeneration((value) => value + 1); };
    window.addEventListener('storage', changed);
    return () => window.removeEventListener('storage', changed);
  }, []);
  let tenant = user?.tenant_id || '';
  try { tenant = localStorage.getItem('current_tenant_id') || tenant; } catch { /* Storage is optional. */ }
  tenant = selectedTenant ?? tenant;
  return { user, tenant, key: `${user?.id}:${user?.role}:${tenant}:${generation}`,
    scope: { expected_tenant_id: tenant, expected_user_id: user?.id || '' } };
}
