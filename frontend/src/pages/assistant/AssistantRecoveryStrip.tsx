import { useQuery } from '@tanstack/react-query';
import { Link, useLocation } from 'react-router-dom';
import { useTranslation } from 'react-i18next';
import { assistantApi } from '../../api/domains/assistant';
import { useAuthStore } from '../../stores';
import { assistantScopeStillCurrent } from './assistantScope';
import './assistant.css';

const PROBLEM_STATES = new Set([
  'model_unconfigured',
  'model_forbidden',
  'provision_incomplete',
  'employee_deleted',
  'employee_inactive',
  'transferred',
  'inaccessible',
  'cross_company',
  'worker_unavailable',
  'worker_unknown',
  'model_unavailable',
  'company_required',
  'scope_changed',
  'legacy_unresolved',
]);

export default function AssistantRecoveryStrip({ userId, tenantId }: { userId?: string; tenantId?: string }) {
  const { t } = useTranslation();
  const location = useLocation();
  const role = useAuthStore((state) => state.user?.role);
  const query = useQuery({
    queryKey: ['assistant', userId || '', tenantId || '', role || ''],
    enabled: Boolean(userId && tenantId) && location.pathname !== '/' && location.pathname !== '/assistant',
    retry: false,
    queryFn: async () => {
      const startedUserId = userId || null;
      const startedTenantId = tenantId || null;
      const payload = await assistantApi.get({
        expected_user_id: startedUserId || undefined,
        expected_tenant_id: startedTenantId || undefined,
      });
      const current = useAuthStore.getState().user;
      const currentTenantId = localStorage.getItem('current_tenant_id') || current?.tenant_id || null;
      if (current?.role !== role || !assistantScopeStillCurrent({
        startedUserId,
        startedTenantId,
        currentUserId: current?.id || null,
        currentTenantId,
        payloadUserId: payload.scope.user_id,
        payloadTenantId: payload.scope.tenant_id,
      })) {
        return null;
      }
      return payload;
    },
  });
  const state = query.data?.state;
  if (!state || !PROBLEM_STATES.has(state)) return null;
  return (
    <div className="assistant-strip" role="status">
      <Link to="/assistant">{t('assistant.title')}</Link>
      <span> {t('assistant.notReady')}</span>
    </div>
  );
}
