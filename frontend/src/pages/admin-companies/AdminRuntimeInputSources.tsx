import { useEffect, useRef, useState } from 'react';
import { useTranslation } from 'react-i18next';

import { adminApi, type RuntimeInputSources } from '../../api/domains/admin';

export default function AdminRuntimeInputSources({ tenantId, taskId, label, disabled }: {
  tenantId: string; taskId: string; label: string; disabled: boolean;
}) {
  const { t } = useTranslation();
  const alive = useRef(true);
  const flight = useRef<Promise<void> | null>(null);
  const [loading, setLoading] = useState(false);
  const [source, setSource] = useState<RuntimeInputSources | null>(null);
  const [error, setError] = useState<string | null>(null);
  const title = `${t('admin.reconciliation.inputSources', 'Input sources')} — ${label} (${taskId.slice(0, 8)})`;

  useEffect(() => {
    alive.current = true;
    return () => { alive.current = false; };
  }, []);

  const load = () => {
    if (flight.current) return;
    setLoading(true);
    setSource(null);
    setError(null);
    flight.current = (async () => {
      try {
        const detail = await adminApi.getRuntimeReconciliation(taskId, { tenantId, includeInputSources: true });
        if (!alive.current) return;
        if (detail.input_sources?.schema !== 'hive.runtime_input_sources.v1') {
          throw new Error(t('admin.reconciliation.evidenceUnavailable', 'Unavailable'));
        }
        setSource(detail.input_sources);
      } catch (err) {
        if (alive.current) setError(err instanceof Error ? err.message : String(err));
      } finally {
        flight.current = null;
        if (alive.current) setLoading(false);
      }
    })();
  };

  return (
    <div className="admin-reconcile-evidence" data-runtime-input-sources={taskId}>
      <button type="button" className="btn-secondary" aria-label={title} onClick={load} disabled={disabled || loading}>
        {loading ? t('common.loading', 'Loading...') : t('admin.reconciliation.inputSources', 'Input sources')}
      </button>
      {error && <div role="alert" className="admin-reconcile-error">{error}</div>}
      {source && (
        <div role="region" aria-label={title}>
          <pre className="admin-reconcile-source-data">{JSON.stringify(source, null, 2)}</pre>
        </div>
      )}
    </div>
  );
}
