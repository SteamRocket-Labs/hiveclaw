import { type FormEvent, useEffect, useState } from 'react';
import { useTranslation } from 'react-i18next';

import {
  extensionsApi,
  type ExternalCapabilityReviewSummary,
  type ExternalExtensionCatalogEntry,
  type ExternalMarketplaceEntry,
  type ExternalMarketplaceSource,
  type LegacyPackMigrationReport,
} from '../../api/domains/extensions';
import { showAppToast } from '../../components/AppDialogs';
import './WorkspaceExtensionCatalogSection.css';

function reviewComponentCount(review: ExternalCapabilityReviewSummary): number {
  const components = review.normalized_manifest?.components;
  return Array.isArray(components) ? components.length : 0;
}

function parseSourceConfig(value: string): Record<string, unknown> {
  const parsed = JSON.parse(value || '{}') as unknown;
  if (!parsed || typeof parsed !== 'object' || Array.isArray(parsed)) {
    throw new Error('source config must be a JSON object');
  }
  return parsed as Record<string, unknown>;
}

const EMPTY_MANUAL_SOURCE_CONFIG = '{\n  "entries": []\n}';
const EMPTY_REMOTE_SOURCE_CONFIG = '{\n  "manifest_path": "marketplace.json"\n}';

export const MARKETPLACE_SOURCE_TYPE_OPTIONS = [
  { value: 'manual', labelKey: 'enterprise.extensions.marketplaceSourceTypeManual', fallback: 'Manual' },
  { value: 'github', labelKey: 'enterprise.extensions.marketplaceSourceTypeGithub', fallback: 'GitHub manifest' },
  { value: 'cc_marketplace', labelKey: 'enterprise.extensions.marketplaceSourceTypeCc', fallback: 'CC marketplace' },
  { value: 'codex_marketplace', labelKey: 'enterprise.extensions.marketplaceSourceTypeCodex', fallback: 'Codex marketplace' },
] as const;

export function marketplaceSourceDefaults(sourceType: string): { uri: string; config: string } {
  if (sourceType === 'github') {
    return {
      uri: 'https://raw.githubusercontent.com/org/repo/main/marketplace.json',
      config: EMPTY_REMOTE_SOURCE_CONFIG,
    };
  }
  if (sourceType === 'cc_marketplace') {
    return {
      uri: 'https://github.com/org/cc-marketplace',
      config: EMPTY_REMOTE_SOURCE_CONFIG,
    };
  }
  if (sourceType === 'codex_marketplace') {
    return {
      uri: 'https://github.com/org/codex-marketplace',
      config: EMPTY_REMOTE_SOURCE_CONFIG,
    };
  }
  return {
    uri: 'manual://workspace',
    config: EMPTY_MANUAL_SOURCE_CONFIG,
  };
}

interface LegacyPackMigrationPanelProps {
  report: LegacyPackMigrationReport | null;
  running: boolean;
  onDryRun: () => void | Promise<void>;
}

export function LegacyPackMigrationPanel({ report, running, onDryRun }: LegacyPackMigrationPanelProps) {
  const { t } = useTranslation();
  const runtimeWrites = report?.runtime_writes?.length ?? 0;
  const counts = report?.counts ?? { plugins: 0, assignments: 0, enabled_assignments: 0 };

  return (
    <section className="workspace-extension-catalog-panel" data-testid="legacy-pack-migration-panel">
      <div className="workspace-extension-catalog-heading">
        {t('enterprise.extensions.legacyMigrationTitle', 'Legacy migration dry-run')}
      </div>
      <div className="card workspace-extension-legacy-migration">
        <div className="workspace-extension-legacy-migration-main">
          <span>{t('enterprise.extensions.legacyMigrationMode', 'migration-only')}</span>
          <strong>
            {report?.blocks_new_entrypoint
              ? t('enterprise.extensions.legacyMigrationBlocksEntrypoint', 'Blocks new entrypoint')
              : t('enterprise.extensions.legacyMigrationReadOnly', 'Read-only projection')}
          </strong>
        </div>
        <div className="workspace-extension-legacy-migration-stats">
          <div>
            <span>{t('enterprise.extensions.legacyPlugins', 'Plugins')}</span>
            <strong>{counts.plugins}</strong>
          </div>
          <div>
            <span>{t('enterprise.extensions.legacyAssignments', 'Assignments')}</span>
            <strong>{counts.assignments}</strong>
          </div>
          <div>
            <span>{t('enterprise.extensions.legacyEnabledAssignments', 'Enabled')}</span>
            <strong>{counts.enabled_assignments}</strong>
          </div>
          <div>
            <span>{t('enterprise.extensions.legacyRuntimeWrites', 'Runtime writes')}</span>
            <strong>{runtimeWrites}</strong>
          </div>
        </div>
        <button
          type="button"
          className="btn btn-secondary workspace-extension-catalog-action"
          disabled={running}
          onClick={() => void onDryRun()}
        >
          {running ? t('common.loading', 'Loading...') : t('enterprise.extensions.runDryRun', 'Run dry-run')}
        </button>
      </div>
    </section>
  );
}

export default function WorkspaceExtensionCatalogSection() {
  const { t } = useTranslation();
  const [catalog, setCatalog] = useState<ExternalExtensionCatalogEntry[]>([]);
  const [reviews, setReviews] = useState<ExternalCapabilityReviewSummary[]>([]);
  const [marketplaceSources, setMarketplaceSources] = useState<ExternalMarketplaceSource[]>([]);
  const [marketplaceEntries, setMarketplaceEntries] = useState<ExternalMarketplaceEntry[]>([]);
  const [legacyMigrationReport, setLegacyMigrationReport] = useState<LegacyPackMigrationReport | null>(null);
  const [loading, setLoading] = useState(true);
  const [approvingReviewId, setApprovingReviewId] = useState<string | null>(null);
  const [rejectingReviewId, setRejectingReviewId] = useState<string | null>(null);
  const [revokingSnapshotId, setRevokingSnapshotId] = useState<string | null>(null);
  const [creatingSource, setCreatingSource] = useState(false);
  const [syncingSourceId, setSyncingSourceId] = useState<string | null>(null);
  const [submittingEntryId, setSubmittingEntryId] = useState<string | null>(null);
  const [runningLegacyMigrationDryRun, setRunningLegacyMigrationDryRun] = useState(false);
  const [sourceType, setSourceType] = useState('manual');
  const [sourceName, setSourceName] = useState('');
  const [sourceUri, setSourceUri] = useState('manual://workspace');
  const [sourceConfigJson, setSourceConfigJson] = useState(EMPTY_MANUAL_SOURCE_CONFIG);

  const loadCatalog = async () => {
    setLoading(true);
    try {
      const [
        catalogEntries,
        reviewEntries,
        marketplaceSourceEntries,
        marketplaceCandidateEntries,
        legacyMigrationDryRun,
      ] = await Promise.all([
        extensionsApi.listExternalExtensionCatalog(),
        extensionsApi.listExternalCapabilityReviews(),
        extensionsApi.listMarketplaceSources(),
        extensionsApi.listMarketplaceEntries(),
        extensionsApi.dryRunLegacyPackMigration(),
      ]);
      setCatalog(catalogEntries);
      setReviews(reviewEntries);
      setMarketplaceSources(marketplaceSourceEntries);
      setMarketplaceEntries(marketplaceCandidateEntries);
      setLegacyMigrationReport(legacyMigrationDryRun);
    } catch (error) {
      console.error(error);
      showAppToast(t('enterprise.extensions.catalogLoadFailed', 'Failed to load extension catalog'), 'error');
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => {
    void loadCatalog();
  }, []);

  const runLegacyMigrationDryRun = async () => {
    setRunningLegacyMigrationDryRun(true);
    try {
      setLegacyMigrationReport(await extensionsApi.dryRunLegacyPackMigration());
      showAppToast(t('enterprise.extensions.legacyMigrationDryRunComplete', 'Legacy migration dry-run complete'), 'success');
    } catch (error) {
      console.error(error);
      showAppToast(t('enterprise.extensions.legacyMigrationDryRunFailed', 'Legacy migration dry-run failed'), 'error');
    } finally {
      setRunningLegacyMigrationDryRun(false);
    }
  };

  const createMarketplaceSource = async (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    const name = sourceName.trim();
    const uri = sourceUri.trim();
    if (!name || !uri) {
      showAppToast(t('enterprise.extensions.marketplaceSourceRequired', 'Source name and URI are required'), 'error');
      return;
    }

    let config: Record<string, unknown>;
    try {
      config = parseSourceConfig(sourceConfigJson);
    } catch (error) {
      console.error(error);
      showAppToast(t('enterprise.extensions.marketplaceSourceConfigInvalid', 'Source config must be valid JSON'), 'error');
      return;
    }

    setCreatingSource(true);
    try {
      await extensionsApi.createMarketplaceSource({
        name,
        source_type: sourceType,
        source_uri: uri,
        status: 'enabled',
        config,
      });
      showAppToast(t('enterprise.extensions.marketplaceSourceCreated', 'Marketplace source created'), 'success');
      setSourceName('');
      setSourceType('manual');
      setSourceUri(marketplaceSourceDefaults('manual').uri);
      setSourceConfigJson(marketplaceSourceDefaults('manual').config);
      await loadCatalog();
    } catch (error) {
      console.error(error);
      showAppToast(t('enterprise.extensions.marketplaceSourceCreateFailed', 'Failed to create marketplace source'), 'error');
    } finally {
      setCreatingSource(false);
    }
  };

  const changeMarketplaceSourceType = (nextType: string) => {
    setSourceType(nextType);
    const defaults = marketplaceSourceDefaults(nextType);
    setSourceUri(defaults.uri);
    setSourceConfigJson(defaults.config);
  };

  const syncMarketplaceSource = async (source: ExternalMarketplaceSource) => {
    setSyncingSourceId(source.id);
    try {
      await extensionsApi.syncMarketplaceSource(source.id);
      showAppToast(t('enterprise.extensions.marketplaceSourceSynced', 'Marketplace source synced'), 'success');
      await loadCatalog();
    } catch (error) {
      console.error(error);
      showAppToast(t('enterprise.extensions.marketplaceSourceSyncFailed', 'Failed to sync marketplace source'), 'error');
    } finally {
      setSyncingSourceId(null);
    }
  };

  const submitMarketplaceEntry = async (entry: ExternalMarketplaceEntry) => {
    setSubmittingEntryId(entry.id);
    try {
      await extensionsApi.submitMarketplaceEntryForReview(entry.id);
      showAppToast(t('enterprise.extensions.marketplaceEntrySubmitted', 'Marketplace entry submitted for review'), 'success');
      await loadCatalog();
    } catch (error) {
      console.error(error);
      showAppToast(t('enterprise.extensions.marketplaceEntrySubmitFailed', 'Failed to submit marketplace entry'), 'error');
    } finally {
      setSubmittingEntryId(null);
    }
  };

  const approveReview = async (review: ExternalCapabilityReviewSummary) => {
    setApprovingReviewId(review.id);
    try {
      await extensionsApi.approveExternalCapabilityReview(review.id);
      showAppToast(t('enterprise.extensions.reviewApproved', 'Review approved'), 'success');
      await loadCatalog();
    } catch (error) {
      console.error(error);
      showAppToast(t('enterprise.extensions.reviewApproveFailed', 'Failed to approve review'), 'error');
    } finally {
      setApprovingReviewId(null);
    }
  };

  const rejectReview = async (review: ExternalCapabilityReviewSummary) => {
    setRejectingReviewId(review.id);
    try {
      await extensionsApi.rejectExternalCapabilityReview(review.id, { reason: 'Rejected from workspace catalog review' });
      showAppToast(t('enterprise.extensions.reviewRejected', 'Review rejected'), 'success');
      await loadCatalog();
    } catch (error) {
      console.error(error);
      showAppToast(t('enterprise.extensions.reviewRejectFailed', 'Failed to reject review'), 'error');
    } finally {
      setRejectingReviewId(null);
    }
  };

  const revokeEntry = async (entry: ExternalExtensionCatalogEntry) => {
    setRevokingSnapshotId(entry.snapshot_id);
    try {
      await extensionsApi.revokeExternalCapabilitySnapshot(entry.snapshot_id);
      showAppToast(t('enterprise.extensions.snapshotRevoked', 'Snapshot revoked'), 'success');
      await loadCatalog();
    } catch (error) {
      console.error(error);
      showAppToast(t('enterprise.extensions.snapshotRevokeFailed', 'Failed to revoke snapshot'), 'error');
    } finally {
      setRevokingSnapshotId(null);
    }
  };

  if (loading) {
    return <div className="workspace-extension-catalog-loading">{t('common.loading', 'Loading...')}</div>;
  }

  const pendingReviews = reviews.filter((review) => review.status === 'review_required');
  const reviewableMarketplaceEntries = new Set(
    marketplaceEntries.filter((entry) => entry.status === 'available' && !entry.review_id).map((entry) => entry.id),
  );

  return (
    <section className="workspace-extension-catalog" data-testid="workspace-extension-catalog">
      <div className="workspace-extension-catalog-summary">
        <div className="card workspace-extension-catalog-stat">
          <span>{t('enterprise.extensions.catalogAvailable', 'Available')}</span>
          <strong>{catalog.length}</strong>
        </div>
        <div className="card workspace-extension-catalog-stat">
          <span>{t('enterprise.extensions.reviewQueue', 'Review queue')}</span>
          <strong>{pendingReviews.length}</strong>
        </div>
        <div className="card workspace-extension-catalog-stat">
          <span>{t('enterprise.extensions.marketplaceSources', 'Marketplace sources')}</span>
          <strong>{marketplaceSources.length}</strong>
        </div>
        <div className="card workspace-extension-catalog-stat">
          <span>{t('enterprise.extensions.marketplaceEntries', 'Marketplace entries')}</span>
          <strong>{marketplaceEntries.length}</strong>
        </div>
      </div>

      <LegacyPackMigrationPanel
        report={legacyMigrationReport}
        running={runningLegacyMigrationDryRun}
        onDryRun={runLegacyMigrationDryRun}
      />

      <section className="workspace-extension-catalog-panel">
        <div className="workspace-extension-catalog-heading">
          {t('enterprise.extensions.marketplaceSources', 'Marketplace sources')}
        </div>
        <form className="card workspace-extension-marketplace-form" onSubmit={createMarketplaceSource}>
          <select
            value={sourceType}
            onChange={(event) => changeMarketplaceSourceType(event.target.value)}
            aria-label={t('enterprise.extensions.marketplaceSourceType', 'Marketplace source type')}
          >
            {MARKETPLACE_SOURCE_TYPE_OPTIONS.map((option) => (
              <option key={option.value} value={option.value}>
                {t(option.labelKey, option.fallback)}
              </option>
            ))}
          </select>
          <input
            type="text"
            value={sourceName}
            onChange={(event) => setSourceName(event.target.value)}
            placeholder={t('enterprise.extensions.marketplaceSourceNamePlaceholder', 'Source name')}
          />
          <input
            type="text"
            value={sourceUri}
            onChange={(event) => setSourceUri(event.target.value)}
            placeholder={t('enterprise.extensions.marketplaceSourceUriPlaceholder', 'manual://workspace')}
          />
          <textarea
            value={sourceConfigJson}
            onChange={(event) => setSourceConfigJson(event.target.value)}
            placeholder={t('enterprise.extensions.marketplaceSourceConfigPlaceholder', 'Manual source JSON')}
            rows={4}
          />
          <button type="submit" className="btn btn-primary workspace-extension-catalog-action" disabled={creatingSource}>
            {creatingSource ? t('common.saving', 'Saving...') : t('enterprise.extensions.createSource', 'Create source')}
          </button>
        </form>
        {marketplaceSources.length === 0 ? (
          <div className="card workspace-extension-catalog-empty">
            {t('enterprise.extensions.noMarketplaceSources', 'No marketplace sources have been configured yet.')}
          </div>
        ) : (
          <div className="workspace-extension-catalog-list">
            {marketplaceSources.map((source) => (
              <div key={source.id} className="card workspace-extension-catalog-row">
                <div className="workspace-extension-catalog-main">
                  <div className="workspace-extension-catalog-name">{source.name}</div>
                  <div className="workspace-extension-catalog-sub">{source.source_uri}</div>
                </div>
                <div className="workspace-extension-catalog-tags">
                  <span>{source.source_type}</span>
                  <span>{source.status}</span>
                  <span>{source.sync_status || 'never_synced'}</span>
                </div>
                <button
                  type="button"
                  className="btn btn-secondary workspace-extension-catalog-action"
                  disabled={syncingSourceId === source.id}
                  onClick={() => void syncMarketplaceSource(source)}
                >
                  {syncingSourceId === source.id
                    ? t('common.saving', 'Saving...')
                    : t('enterprise.extensions.syncSource', 'Sync')}
                </button>
              </div>
            ))}
          </div>
        )}
      </section>

      <section className="workspace-extension-catalog-panel">
        <div className="workspace-extension-catalog-heading">
          {t('enterprise.extensions.marketplaceEntries', 'Marketplace entries')}
        </div>
        {marketplaceEntries.length === 0 ? (
          <div className="card workspace-extension-catalog-empty">
            {t('enterprise.extensions.noMarketplaceEntries', 'No marketplace entries are available from configured sources.')}
          </div>
        ) : (
          <div className="workspace-extension-catalog-list">
            {marketplaceEntries.map((entry) => (
              <div key={entry.id} className="card workspace-extension-catalog-row">
                <div className="workspace-extension-catalog-main">
                  <div className="workspace-extension-catalog-name">{entry.display_name}</div>
                  <div className="workspace-extension-catalog-sub">{entry.source_uri}</div>
                </div>
                <div className="workspace-extension-catalog-tags">
                  <span>{entry.source_format}</span>
                  <span>{entry.status}</span>
                  {entry.review_id && <span>{t('enterprise.extensions.reviewLinked', 'review linked')}</span>}
                </div>
                <button
                  type="button"
                  className="btn btn-secondary workspace-extension-catalog-action"
                  disabled={!reviewableMarketplaceEntries.has(entry.id) || submittingEntryId === entry.id}
                  onClick={() => void submitMarketplaceEntry(entry)}
                >
                  {submittingEntryId === entry.id
                    ? t('common.saving', 'Saving...')
                    : t('enterprise.extensions.submitReview', 'Submit review')}
                </button>
              </div>
            ))}
          </div>
        )}
      </section>

      <section className="workspace-extension-catalog-panel">
        <div className="workspace-extension-catalog-heading">
          {t('enterprise.extensions.approvedCatalog', 'Approved catalog')}
        </div>
        {catalog.length === 0 ? (
          <div className="card workspace-extension-catalog-empty">
            {t('enterprise.extensions.catalogEmpty', 'No approved external extensions have been published yet.')}
          </div>
        ) : (
          <div className="workspace-extension-catalog-list">
            {catalog.map((entry) => (
              <div key={entry.id} className="card workspace-extension-catalog-row">
                <div className="workspace-extension-catalog-main">
                  <div className="workspace-extension-catalog-name">{entry.component_name}</div>
                  <div className="workspace-extension-catalog-sub">{entry.qualified_name}</div>
                </div>
                <div className="workspace-extension-catalog-tags">
                  <span>{entry.component_type}</span>
                  <span>{entry.policy}</span>
                  <span>{entry.status}</span>
                </div>
                <button
                  type="button"
                  className="btn btn-secondary workspace-extension-catalog-action"
                  disabled={revokingSnapshotId === entry.snapshot_id}
                  onClick={() => void revokeEntry(entry)}
                >
                  {revokingSnapshotId === entry.snapshot_id
                    ? t('common.saving', 'Saving...')
                    : t('enterprise.extensions.revoke', 'Revoke')}
                </button>
              </div>
            ))}
          </div>
        )}
      </section>

      <section className="workspace-extension-catalog-panel">
        <div className="workspace-extension-catalog-heading">
          {t('enterprise.extensions.pendingReviews', 'Pending reviews')}
        </div>
        {pendingReviews.length === 0 ? (
          <div className="card workspace-extension-catalog-empty">
            {t('enterprise.extensions.noPendingReviews', 'No external capability reviews are waiting.')}
          </div>
        ) : (
          <div className="workspace-extension-catalog-list">
            {pendingReviews.map((review) => (
              <div key={review.id} className="card workspace-extension-catalog-row">
                <div className="workspace-extension-catalog-main">
                  <div className="workspace-extension-catalog-name">{review.normalized_name}</div>
                  <div className="workspace-extension-catalog-sub">
                    {review.source_format} · {reviewComponentCount(review)} {t('enterprise.extensions.components', 'components')}
                  </div>
                </div>
                <div className="workspace-extension-catalog-actions">
                  <button
                    type="button"
                    className="btn btn-secondary workspace-extension-catalog-action"
                    disabled={rejectingReviewId === review.id || approvingReviewId === review.id}
                    onClick={() => void rejectReview(review)}
                  >
                    {rejectingReviewId === review.id
                      ? t('common.saving', 'Saving...')
                      : t('enterprise.extensions.reject', 'Reject')}
                  </button>
                  <button
                    type="button"
                    className="btn btn-primary workspace-extension-catalog-action"
                    disabled={approvingReviewId === review.id || rejectingReviewId === review.id}
                    onClick={() => void approveReview(review)}
                  >
                    {approvingReviewId === review.id
                      ? t('common.saving', 'Saving...')
                      : t('enterprise.extensions.approve', 'Approve')}
                  </button>
                </div>
              </div>
            ))}
          </div>
        )}
      </section>
    </section>
  );
}
