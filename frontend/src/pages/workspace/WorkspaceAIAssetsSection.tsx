import { useCallback, useEffect, useMemo, useState } from 'react';
import { useTranslation } from 'react-i18next';

import {
  aiAssetsApi,
  type AIAssetDetail,
  type AIAssetRecord,
  type AIAssetRevision,
} from '../../api/domains/aiAssets';
import { requestAppConfirm, showAppToast } from '../../components/AppDialogs';
import './WorkspaceAIAssetsSection.css';

type Props = { selectedTenantId: string };
const ASSET_TYPES = ['', 'agent', 'skill', 'workflow', 'subagent', 'external_capability'];
const LIFECYCLE_STATES = ['', 'draft', 'active', 'deprecated', 'revoked', 'deleted', 'quarantined'];

const pretty = (value: unknown) => JSON.stringify(value, null, 2);
const shortHash = (value: string) => value.length > 16 ? `${value.slice(0, 8)}…${value.slice(-6)}` : value;

function RevisionDiff({ revision }: { revision: AIAssetRevision }) {
  const setKeys = Object.keys(revision.diff_from_prev?.set ?? {});
  const removed = revision.diff_from_prev?.removed ?? [];
  if (!setKeys.length && !removed.length) return <span className="ai-asset-muted">No content delta</span>;
  return (
    <div className="ai-asset-diff">
      {setKeys.length > 0 && <code>+ {setKeys.join(', ')}</code>}
      {removed.length > 0 && <code className="removed">− {removed.join(', ')}</code>}
    </div>
  );
}

export function AIAssetDetailPanel({
  detail,
  busy,
  onRollback,
  onReconcile,
}: {
  detail: AIAssetDetail;
  busy: boolean;
  onRollback: (version: number) => void | Promise<void>;
  onReconcile: () => void | Promise<void>;
}) {
  const { t } = useTranslation();
  const { asset, history, usage_events: usageEvents } = detail;
  return (
    <aside className="ai-asset-inspector" aria-label={t('enterprise.extensions.aiAssetsInspector', 'AI asset inspector')}>
      <div className="ai-asset-inspector-heading">
        <div>
          <span className="ai-asset-eyebrow">{asset.asset_type}</span>
          <h4>{asset.display_name}</h4>
          <code>{asset.native_key}</code>
        </div>
        <button type="button" className="btn btn-secondary" disabled={busy} onClick={() => void onReconcile()}>
          {t('enterprise.extensions.aiAssetsReconcile', 'Reconcile')}
        </button>
      </div>

      <div className="ai-asset-facts">
        <div><span>{t('enterprise.extensions.aiAssetsOwner', 'Owner')}</span><strong>{asset.owner.type}:{asset.owner.id ?? '—'}</strong></div>
        <div><span>{t('enterprise.extensions.aiAssetsTrust', 'Trust')}</span><strong>{asset.trust_state}</strong></div>
        <div><span>{t('enterprise.extensions.aiAssetsAdmission', 'Admission')}</span><strong>{asset.admission_state}</strong></div>
        <div><span>{t('enterprise.extensions.aiAssetsProjection', 'Projection')}</span><strong>{asset.projection.status}</strong></div>
        <div><span>{t('enterprise.extensions.aiAssetsSource', 'Source')}</span><strong>{asset.source.type}</strong></div>
        <div><span>{t('enterprise.extensions.aiAssetsHash', 'Content hash')}</span><strong title={asset.content_hash}>{shortHash(asset.content_hash)}</strong></div>
      </div>

      {(asset.quarantine_reason || asset.projection.error) && (
        <div className="ai-asset-warning" role="alert">{asset.quarantine_reason || asset.projection.error}</div>
      )}

      <section className="ai-asset-inspector-section">
        <h5>{t('enterprise.extensions.aiAssetsDependencies', 'Dependencies')}</h5>
        {asset.dependencies.length ? (
          <div className="ai-asset-chips">{asset.dependencies.map((item) => <code key={item}>{item}</code>)}</div>
        ) : <span className="ai-asset-muted">None</span>}
        {Object.keys(asset.compatibility).length > 0 && <pre>{pretty(asset.compatibility)}</pre>}
      </section>

      <section className="ai-asset-inspector-section">
        <h5>{t('enterprise.extensions.aiAssetsUsage', 'Usage evidence')} · {asset.usage.count}</h5>
        {usageEvents.length ? (
          <div className="ai-asset-history">
            {usageEvents.map((event) => (
              <article key={event.id} className="ai-asset-revision">
                <div className="ai-asset-revision-line">
                  <strong>{event.usage_kind}</strong>
                  <span>v{event.revision_version}</span>
                  <code>{event.runtime_task_id ?? event.tool_call_id ?? event.span_id ?? event.idempotency_key}</code>
                </div>
                <small>{event.session_id ? `session ${event.session_id}` : 'no session'} · {event.usage_units} use</small>
              </article>
            ))}
          </div>
        ) : <span className="ai-asset-muted">No durable usage events yet</span>}
        {asset.usage.evidence.length > 0 && <details><summary>Compatibility evidence</summary><pre>{pretty(asset.usage.evidence)}</pre></details>}
      </section>

      <section className="ai-asset-inspector-section">
        <h5>{t('enterprise.extensions.aiAssetsHistory', 'Revision history')}</h5>
        <div className="ai-asset-history">
          {history.map((revision) => (
            <article key={revision.id} className={`ai-asset-revision${revision.is_active ? ' active' : ''}`}>
              <div className="ai-asset-revision-line">
                <strong>v{revision.version}</strong><span>{revision.change_source}</span><code>{shortHash(revision.content_hash)}</code>
              </div>
              <RevisionDiff revision={revision} />
              {revision.change_message && <p>{revision.change_message}</p>}
              {!revision.is_active && (
                <button type="button" className="btn btn-secondary" disabled={busy} onClick={() => void onRollback(revision.version)}>
                  {t('enterprise.extensions.aiAssetsRollbackVersion', `Rollback to v${revision.version}`)}
                </button>
              )}
            </article>
          ))}
        </div>
      </section>
    </aside>
  );
}

export default function WorkspaceAIAssetsSection({ selectedTenantId }: Props) {
  const { t } = useTranslation();
  const [assetType, setAssetType] = useState('');
  const [lifecycleStatus, setLifecycleStatus] = useState('');
  const [assets, setAssets] = useState<AIAssetRecord[]>([]);
  const [selectedAssetId, setSelectedAssetId] = useState<string | null>(null);
  const [detail, setDetail] = useState<AIAssetDetail | null>(null);
  const [loading, setLoading] = useState(true);
  const [busy, setBusy] = useState(false);

  const loadAssets = useCallback(async () => {
    setLoading(true);
    try {
      const rows = await aiAssetsApi.list({ assetType, lifecycleStatus });
      setAssets(rows);
      setSelectedAssetId((current) => current && rows.some((row) => row.id === current) ? current : rows[0]?.id ?? null);
    } catch (error) {
      console.error(error);
      showAppToast(t('enterprise.extensions.aiAssetsLoadFailed', 'Failed to load AI assets'), 'error');
    } finally {
      setLoading(false);
    }
  }, [assetType, lifecycleStatus, selectedTenantId, t]);

  const loadDetail = useCallback(async (assetId: string) => {
    try {
      setDetail(await aiAssetsApi.detail(assetId));
    } catch (error) {
      console.error(error);
      showAppToast(t('enterprise.extensions.aiAssetsDetailFailed', 'Failed to load asset details'), 'error');
    }
  }, [t]);

  useEffect(() => { void loadAssets(); }, [loadAssets]);
  useEffect(() => {
    if (selectedAssetId) void loadDetail(selectedAssetId);
    else setDetail(null);
  }, [loadDetail, selectedAssetId]);

  const stats = useMemo(() => ({
    total: assets.length,
    review: assets.filter((asset) => asset.admission_state === 'review_required' || asset.lifecycle_status === 'quarantined').length,
    drift: assets.filter((asset) => asset.projection.status !== 'applied').length,
    used: assets.filter((asset) => asset.usage.count > 0).length,
  }), [assets]);

  const rollback = async (version: number) => {
    if (!detail) return;
    const confirmed = await requestAppConfirm({
      title: t('enterprise.extensions.aiAssetsRollbackTitle', 'Rollback AI asset'),
      message: t('enterprise.extensions.aiAssetsRollbackConfirm', `Apply revision v${version} to the native asset and create a new revision?`),
      confirmLabel: t('enterprise.extensions.aiAssetsRollback', 'Rollback'),
      danger: true,
    });
    if (!confirmed) return;
    setBusy(true);
    try {
      await aiAssetsApi.rollback(detail.asset.id, version);
      await Promise.all([loadAssets(), loadDetail(detail.asset.id)]);
      showAppToast(t('enterprise.extensions.aiAssetsRollbackDone', 'Asset rolled back and re-versioned'), 'success');
    } catch (error) {
      console.error(error);
      showAppToast(t('enterprise.extensions.aiAssetsRollbackFailed', 'Asset rollback failed'), 'error');
    } finally { setBusy(false); }
  };

  const reconcile = async () => {
    if (!detail) return;
    setBusy(true);
    try {
      const result = await aiAssetsApi.reconcile(detail.asset.id);
      await Promise.all([loadAssets(), loadDetail(detail.asset.id)]);
      showAppToast(`${t('enterprise.extensions.aiAssetsReconciled', 'Reconciliation complete')}: ${result.status}`, result.status === 'applied' ? 'success' : 'info');
    } catch (error) {
      console.error(error);
      showAppToast(t('enterprise.extensions.aiAssetsReconcileFailed', 'Asset reconciliation failed'), 'error');
    } finally { setBusy(false); }
  };

  return (
    <section className="workspace-ai-assets" data-testid="workspace-ai-assets">
      <div className="ai-asset-summary">
        <div><span>{t('enterprise.extensions.aiAssetsTotal', 'Assets')}</span><strong>{stats.total}</strong></div>
        <div><span>{t('enterprise.extensions.aiAssetsReview', 'Review')}</span><strong>{stats.review}</strong></div>
        <div><span>{t('enterprise.extensions.aiAssetsDrift', 'Drift')}</span><strong>{stats.drift}</strong></div>
        <div><span>{t('enterprise.extensions.aiAssetsUsed', 'Used')}</span><strong>{stats.used}</strong></div>
      </div>
      <div className="ai-asset-toolbar">
        <select aria-label={t('enterprise.extensions.aiAssetsTypeFilter', 'Asset type')} value={assetType} onChange={(event) => setAssetType(event.target.value)}>
          {ASSET_TYPES.map((value) => <option key={value || 'all'} value={value}>{value || t('enterprise.extensions.aiAssetsAllTypes', 'All types')}</option>)}
        </select>
        <select aria-label={t('enterprise.extensions.aiAssetsStatusFilter', 'Lifecycle status')} value={lifecycleStatus} onChange={(event) => setLifecycleStatus(event.target.value)}>
          {LIFECYCLE_STATES.map((value) => <option key={value || 'all'} value={value}>{value || t('enterprise.extensions.aiAssetsAllStates', 'All states')}</option>)}
        </select>
        <button type="button" className="btn btn-secondary" onClick={() => void loadAssets()}>{t('common.refresh', 'Refresh')}</button>
      </div>
      <div className="ai-asset-layout">
        <div className="ai-asset-list" role="list" aria-busy={loading}>
          {loading && <div className="ai-asset-empty">{t('common.loading', 'Loading...')}</div>}
          {!loading && !assets.length && <div className="ai-asset-empty">{t('enterprise.extensions.aiAssetsEmpty', 'No tenant AI assets found')}</div>}
          {!loading && assets.map((asset) => (
            <button key={asset.id} type="button" role="listitem" className={`ai-asset-row${selectedAssetId === asset.id ? ' active' : ''}`} onClick={() => setSelectedAssetId(asset.id)}>
              <span className="ai-asset-type">{asset.asset_type}</span><strong>{asset.display_name}</strong>
              <span>{asset.lifecycle_status} · {asset.trust_state}</span>
              <small>{asset.owner.type}:{asset.owner.id ?? '—'} · {asset.usage.count} uses</small>
              {asset.projection.status !== 'applied' && <em>{asset.projection.status}</em>}
            </button>
          ))}
        </div>
        {detail ? (
          <AIAssetDetailPanel detail={detail} busy={busy} onRollback={rollback} onReconcile={reconcile} />
        ) : (
          <div className="ai-asset-empty ai-asset-inspector-empty">{t('enterprise.extensions.aiAssetsSelect', 'Select an asset to inspect evidence and revisions')}</div>
        )}
      </div>
    </section>
  );
}
