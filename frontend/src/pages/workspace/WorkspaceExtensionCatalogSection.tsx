import { useEffect, useState } from 'react';
import { useTranslation } from 'react-i18next';

import {
  extensionsApi,
  type ExternalCapabilityReviewSummary,
  type ExternalExtensionCatalogEntry,
} from '../../api/domains/extensions';
import { showAppToast } from '../../components/AppDialogs';
import './WorkspaceExtensionCatalogSection.css';

function reviewComponentCount(review: ExternalCapabilityReviewSummary): number {
  const components = review.normalized_manifest?.components;
  return Array.isArray(components) ? components.length : 0;
}

export default function WorkspaceExtensionCatalogSection() {
  const { t } = useTranslation();
  const [catalog, setCatalog] = useState<ExternalExtensionCatalogEntry[]>([]);
  const [reviews, setReviews] = useState<ExternalCapabilityReviewSummary[]>([]);
  const [loading, setLoading] = useState(true);
  const [approvingReviewId, setApprovingReviewId] = useState<string | null>(null);

  const loadCatalog = async () => {
    setLoading(true);
    try {
      const [catalogEntries, reviewEntries] = await Promise.all([
        extensionsApi.listExternalExtensionCatalog(),
        extensionsApi.listExternalCapabilityReviews(),
      ]);
      setCatalog(catalogEntries);
      setReviews(reviewEntries);
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

  if (loading) {
    return <div className="workspace-extension-catalog-loading">{t('common.loading', 'Loading...')}</div>;
  }

  const pendingReviews = reviews.filter((review) => review.status === 'review_required');

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
      </div>

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
                <button
                  type="button"
                  className="btn btn-primary workspace-extension-catalog-action"
                  disabled={approvingReviewId === review.id}
                  onClick={() => void approveReview(review)}
                >
                  {approvingReviewId === review.id
                    ? t('common.saving', 'Saving...')
                    : t('enterprise.extensions.approve', 'Approve')}
                </button>
              </div>
            ))}
          </div>
        )}
      </section>
    </section>
  );
}
