import { useEffect, useMemo, useState } from 'react';
import { useTranslation } from 'react-i18next';

import {
  extensionsApi,
  type CapabilityFactor,
  type CapabilityPromotionProposal,
} from '../../api/domains/extensions';
import { showAppToast } from '../../components/AppDialogs';
import './WorkspaceExtensionCatalogSection.css';

export default function WorkspaceCapabilityFactorsSection() {
  const { t } = useTranslation();
  const [factors, setFactors] = useState<CapabilityFactor[]>([]);
  const [proposals, setProposals] = useState<CapabilityPromotionProposal[]>([]);
  const [loading, setLoading] = useState(true);
  const [busyId, setBusyId] = useState<string | null>(null);

  const loadFactors = async () => {
    setLoading(true);
    try {
      const [factorRows, proposalRows] = await Promise.all([
        extensionsApi.listEnterpriseCapabilityFactors(),
        extensionsApi.listCapabilityPromotionProposals(),
      ]);
      setFactors(factorRows);
      setProposals(proposalRows);
    } catch (error) {
      console.error(error);
      showAppToast(t('enterprise.extensions.factorLoadFailed', 'Failed to load capability factor intake'), 'error');
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => {
    void loadFactors();
  }, []);

  const pendingProposalByFactor = useMemo(() => {
    const result = new Map<string, CapabilityPromotionProposal>();
    proposals
      .filter((proposal) => proposal.decision === 'pending')
      .forEach((proposal) => result.set(proposal.factor_id, proposal));
    return result;
  }, [proposals]);

  const createProposal = async (factor: CapabilityFactor) => {
    setBusyId(factor.id);
    try {
      await extensionsApi.createCapabilityPromotionProposal(factor.id, {
        proposed_snapshot_kind: factor.factor_kind.includes('subagent') ? 'subagent' : 'skill',
        proposed_catalog_scope: 'workspace',
        proposed_activation_policy: 'requestable',
        proposed_selector: { factor_kind: factor.factor_kind },
      });
      showAppToast(t('enterprise.extensions.proposalCreated', 'Promotion proposal created'), 'success');
      await loadFactors();
    } catch (error) {
      console.error(error);
      showAppToast(t('enterprise.extensions.proposalCreateFailed', 'Failed to create promotion proposal'), 'error');
    } finally {
      setBusyId(null);
    }
  };

  const archiveFactor = async (factor: CapabilityFactor) => {
    setBusyId(factor.id);
    try {
      await extensionsApi.archiveCapabilityFactor(factor.id);
      showAppToast(t('enterprise.extensions.factorArchived', 'Capability factor archived'), 'success');
      await loadFactors();
    } catch (error) {
      console.error(error);
      showAppToast(t('enterprise.extensions.factorArchiveFailed', 'Failed to archive capability factor'), 'error');
    } finally {
      setBusyId(null);
    }
  };

  const decideProposal = async (proposal: CapabilityPromotionProposal, decision: 'approved' | 'rejected') => {
    setBusyId(proposal.id);
    try {
      if (decision === 'approved') {
        await extensionsApi.approveCapabilityPromotionProposal(proposal.id, {
          reason: 'Approved from Capability Factor Intake',
        });
        showAppToast(t('enterprise.extensions.proposalApproved', 'Promotion proposal approved'), 'success');
      } else {
        await extensionsApi.rejectCapabilityPromotionProposal(proposal.id, {
          reason: 'Rejected from Capability Factor Intake',
        });
        showAppToast(t('enterprise.extensions.proposalRejected', 'Promotion proposal rejected'), 'success');
      }
      await loadFactors();
    } catch (error) {
      console.error(error);
      showAppToast(t('enterprise.extensions.proposalDecisionFailed', 'Failed to update promotion proposal'), 'error');
    } finally {
      setBusyId(null);
    }
  };

  if (loading) {
    return <div className="workspace-extension-catalog-loading">{t('common.loading', 'Loading...')}</div>;
  }

  return (
    <section className="workspace-extension-catalog" data-testid="workspace-capability-factors-section">
      <section className="workspace-extension-catalog-panel">
        <div className="workspace-extension-catalog-heading">
          {t('enterprise.extensions.factorIntake', 'Capability Factor Intake')}
        </div>
        {factors.length === 0 ? (
          <div className="card workspace-extension-catalog-empty">
            {t('enterprise.extensions.noFactors', 'No capability factors have been captured yet.')}
          </div>
        ) : (
          <div className="workspace-extension-catalog-list">
            {factors.map((factor) => {
              const pendingProposal = pendingProposalByFactor.get(factor.id);
              return (
                <div key={factor.id} className="card workspace-extension-catalog-row">
                  <div className="workspace-extension-catalog-main">
                    <div className="workspace-extension-catalog-name">{factor.display_name}</div>
                    <div className="workspace-extension-catalog-sub">{factor.summary || factor.factor_kind}</div>
                  </div>
                  <div className="workspace-extension-catalog-tags">
                    <span>{factor.factor_kind}</span>
                    <span>{factor.status}</span>
                    {factor.suggested_scope && <span>{factor.suggested_scope}</span>}
                  </div>
                  <div className="workspace-extension-catalog-actions">
                    <button
                      type="button"
                      className="btn btn-secondary workspace-extension-catalog-action"
                      disabled={Boolean(pendingProposal) || factor.status === 'archived' || busyId === factor.id}
                      onClick={() => void createProposal(factor)}
                    >
                      {t('enterprise.extensions.promote', 'Promote')}
                    </button>
                    <button
                      type="button"
                      className="btn btn-secondary workspace-extension-catalog-action"
                      disabled={factor.status === 'archived' || busyId === factor.id}
                      onClick={() => void archiveFactor(factor)}
                    >
                      {t('enterprise.extensions.archive', 'Archive')}
                    </button>
                  </div>
                </div>
              );
            })}
          </div>
        )}
      </section>

      <section className="workspace-extension-catalog-panel">
        <div className="workspace-extension-catalog-heading">
          {t('enterprise.extensions.promotionProposals', 'Promotion proposals')}
        </div>
        {proposals.length === 0 ? (
          <div className="card workspace-extension-catalog-empty">
            {t('enterprise.extensions.noPromotionProposals', 'No promotion proposals are waiting.')}
          </div>
        ) : (
          <div className="workspace-extension-catalog-list">
            {proposals.map((proposal) => (
              <div key={proposal.id} className="card workspace-extension-catalog-row">
                <div className="workspace-extension-catalog-main">
                  <div className="workspace-extension-catalog-name">{proposal.proposed_snapshot_kind}</div>
                  <div className="workspace-extension-catalog-sub">{proposal.factor_id}</div>
                </div>
                <div className="workspace-extension-catalog-tags">
                  <span>{proposal.proposed_catalog_scope}</span>
                  <span>{proposal.proposed_activation_policy}</span>
                  <span>{proposal.decision}</span>
                </div>
                <div className="workspace-extension-catalog-actions">
                  <button
                    type="button"
                    className="btn btn-primary workspace-extension-catalog-action"
                    disabled={proposal.decision !== 'pending' || busyId === proposal.id}
                    onClick={() => void decideProposal(proposal, 'approved')}
                  >
                    {t('enterprise.extensions.approve', 'Approve')}
                  </button>
                  <button
                    type="button"
                    className="btn btn-secondary workspace-extension-catalog-action"
                    disabled={proposal.decision !== 'pending' || busyId === proposal.id}
                    onClick={() => void decideProposal(proposal, 'rejected')}
                  >
                    {t('enterprise.extensions.reject', 'Reject')}
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
