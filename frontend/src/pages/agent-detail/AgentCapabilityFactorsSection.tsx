import { type FormEvent, useEffect, useState } from 'react';
import { useTranslation } from 'react-i18next';

import { extensionsApi, type CapabilityFactor } from '../../api/domains/extensions';
import { showAppToast } from '../../components/AppDialogs';
import './AgentExtensionCatalogSection.css';

type AgentCapabilityFactorsSectionProps = {
  agentId: string;
  canManage?: boolean;
};

export default function AgentCapabilityFactorsSection({ agentId, canManage = false }: AgentCapabilityFactorsSectionProps) {
  const { t } = useTranslation();
  const [factors, setFactors] = useState<CapabilityFactor[]>([]);
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);
  const [displayName, setDisplayName] = useState('');
  const [summary, setSummary] = useState('');
  const [factorKind, setFactorKind] = useState('skill_candidate');

  const loadFactors = async () => {
    setLoading(true);
    try {
      setFactors(await extensionsApi.listAgentCapabilityFactors(agentId));
    } catch (error) {
      console.error(error);
      showAppToast(t('agent.extensions.factorsLoadFailed', 'Failed to load capability factors'), 'error');
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => {
    void loadFactors();
  }, [agentId]);

  const submitFactor = async (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    const name = displayName.trim();
    if (!name) {
      showAppToast(t('agent.extensions.factorNameRequired', 'Factor name is required'), 'error');
      return;
    }
    setSaving(true);
    try {
      await extensionsApi.captureAgentCapabilityFactor(agentId, {
        factor_kind: factorKind,
        display_name: name,
        summary: summary.trim() || null,
        suggested_scope: 'workspace',
      });
      showAppToast(t('agent.extensions.factorCaptured', 'Capability factor captured'), 'success');
      setDisplayName('');
      setSummary('');
      await loadFactors();
    } catch (error) {
      console.error(error);
      showAppToast(t('agent.extensions.factorCaptureFailed', 'Failed to capture capability factor'), 'error');
    } finally {
      setSaving(false);
    }
  };

  if (loading) {
    return <div className="agent-extension-catalog-loading">{t('common.loading', 'Loading...')}</div>;
  }

  return (
    <section className="agent-extension-catalog" data-testid="agent-capability-factors-section">
      {canManage && (
        <form className="card agent-extension-catalog-card" onSubmit={submitFactor}>
          <div className="agent-extension-catalog-main">
            <input
              type="text"
              value={displayName}
              onChange={(event) => setDisplayName(event.target.value)}
              placeholder={t('agent.extensions.factorNamePlaceholder', 'Candidate name')}
            />
            <input
              type="text"
              value={summary}
              onChange={(event) => setSummary(event.target.value)}
              placeholder={t('agent.extensions.factorSummaryPlaceholder', 'Why this is reusable')}
            />
            <select value={factorKind} onChange={(event) => setFactorKind(event.target.value)}>
              <option value="skill_candidate">{t('agent.extensions.factorKind.skill_candidate', 'Skill candidate')}</option>
              <option value="subagent_candidate">{t('agent.extensions.factorKind.subagent_candidate', 'Sub-agent candidate')}</option>
              <option value="external_usage_factor">{t('agent.extensions.factorKind.external_usage_factor', 'External usage')}</option>
            </select>
          </div>
          <button type="submit" className="btn btn-primary agent-extension-catalog-action" disabled={saving}>
            {saving ? t('common.saving', 'Saving...') : t('agent.extensions.captureFactor', 'Capture')}
          </button>
        </form>
      )}
      <div className="agent-extension-catalog-group">
        <div className="agent-extension-catalog-group-title">
          {t('agent.extensions.selfGrownCandidates', 'Self-grown candidates')}
        </div>
        {factors.length === 0 ? (
          <div className="card agent-extension-catalog-empty">
            {t('agent.extensions.noCapabilityFactors', 'No self-grown capability candidates have been captured yet.')}
          </div>
        ) : (
          <div className="agent-extension-catalog-grid">
            {factors.map((factor) => (
              <div key={factor.id} className="card agent-extension-catalog-card">
                <div className="agent-extension-catalog-main">
                  <div className="agent-extension-catalog-name">{factor.display_name}</div>
                  {factor.summary && <div className="agent-extension-catalog-desc">{factor.summary}</div>}
                  <div className="agent-extension-catalog-meta">
                    <span>{factor.factor_kind}</span>
                    <span>{factor.status}</span>
                    {factor.suggested_scope && <span>{factor.suggested_scope}</span>}
                  </div>
                </div>
              </div>
            ))}
          </div>
        )}
      </div>
    </section>
  );
}
