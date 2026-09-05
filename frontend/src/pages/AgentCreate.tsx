import { useState } from 'react';
import { useNavigate } from 'react-router-dom';
import { useTranslation } from 'react-i18next';
import type { TFunction } from 'i18next';
import {
  IconArrowRight,
  IconChecklist,
  IconMessageCircle,
  IconShieldCheck,
  IconSparkles,
} from '@tabler/icons-react';
import { agentApi } from '../api/domains/agents';
import { chatApi } from '../api/domains/chat';
import { ApiError } from '../api/core';
import PlatformAdminCompanyPicker from '../components/PlatformAdminCompanyPicker';
import { useAuthStore } from '../stores';

const capabilityRails = [
  { key: 'hrOnly', fallback: 'Employee creation is guided by the HR Agent as the single creation role.' },
  { key: 'memoryGate', fallback: 'Memory writes go through Memory Gate and Platform Gate.' },
  { key: 'toolGovernance', fallback: 'Tools, MCP, workflow, and sub-agent access stay permission-governed.' },
  { key: 'auditTrace', fallback: 'Every durable action remains traceable to the employee, user, and company.' },
];

const hrOpenError = (caught: unknown, t: TFunction) => {
  if (caught instanceof ApiError && [502, 503, 504].includes(caught.status)) {
    return t(
      'agentCreate.hrUnavailable',
      'HR Agent is temporarily unavailable. Nothing has been submitted. Please try again in a moment.',
    );
  }
  // A typed server refusal (e.g. the per-Agent access check on session
  // creation) is shown truthfully — never disguised as company selection.
  if (caught instanceof ApiError && caught.message) {
    return t(
      'agentCreate.hrErrorReason',
      'Could not open HR Agent: {{reason}}. Nothing has been submitted. Please try again.',
      { reason: caught.message },
    );
  }
  return t(
    'agentCreate.hrError',
    'Could not open HR Agent. Nothing has been submitted. Please try again.',
  );
};

// On GET /agents/system/hr the only 400/403/404 sources for an authenticated
// platform administrator are the typed company-selection responses (missing,
// stale, or disabled selected company) — recovery is choosing a company, not
// retrying. The follow-up session-creation call has its own unrelated denial
// sources (the server's per-Agent access check), so it never routes here.
const isCompanySelectionError = (caught: unknown) =>
  caught instanceof ApiError && [400, 403, 404].includes(caught.status);

export default function AgentCreate() {
  const { t } = useTranslation();
  const navigate = useNavigate();
  const user = useAuthStore((s) => s.user);
  const isPlatformAdmin = user?.role === 'platform_admin';
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [selectionRequired, setSelectionRequired] = useState(() =>
    Boolean(isPlatformAdmin && !localStorage.getItem('current_tenant_id') && !user?.tenant_id)
  );

  const openHrAgent = async () => {
    setLoading(true);
    setError(null);
    let hrAgent;
    try {
      hrAgent = await agentApi.getHrAgent();
    } catch (caught) {
      if (isPlatformAdmin && isCompanySelectionError(caught)) {
        setSelectionRequired(true);
      } else {
        setError(hrOpenError(caught, t));
      }
      setLoading(false);
      return;
    }
    try {
      const session = await chatApi.createSession(hrAgent.id);
      navigate(`/agents/${hrAgent.id}?session_id=${encodeURIComponent(String(session.id))}#chat`);
    } catch (caught) {
      setError(hrOpenError(caught, t));
    } finally {
      setLoading(false);
    }
  };

  return (
    <div className="workbench-page agent-create-page">
      <div className="workbench-hero">
        <div>
          <span className="workbench-eyebrow">{t('agentCreate.eyebrow', 'Digital employee setup')}</span>
          <h1 className="page-title">{t('agentCreate.title', 'Create digital employee')}</h1>
          <p className="page-subtitle">
            {t(
              'agentCreate.subtitle',
              'Hive creates employees through the HR Agent. It clarifies role, authority, extension catalog access, memory boundaries, and the first working session before creation is finalized.',
            )}
          </p>
        </div>
      </div>

      <section className="workbench-panel agent-create-assistant">
        <div className="agent-create-hr-card">
          <span className="agent-create-method-icon">
            <IconMessageCircle size={18} stroke={1.7} />
          </span>
          <div>
            <h2>{t('agentCreate.hrOnlyTitle', 'HR Agent is the creation interface')}</h2>
            <p>
              {t(
                'agentCreate.hrOnlyDesc',
                'There is only one exposed creation path: enter the HR Agent session, describe the role, confirm governance, and let the HR Agent create the employee through the governed backend.',
              )}
            </p>
            {error && <div className="workbench-error" role="alert" aria-live="polite">{error}</div>}
          </div>
        </div>
        {isPlatformAdmin && selectionRequired ? (
          <PlatformAdminCompanyPicker
            onSelected={() => {
              setSelectionRequired(false);
              setError(null);
              void openHrAgent();
            }}
          />
        ) : (
          <button className="btn btn-primary" onClick={openHrAgent} disabled={loading}>
            <IconSparkles size={16} stroke={1.7} />
            {loading ? t('common.loading', 'Loading...') : t('agentCreate.useHr', 'Use HR Agent for guided creation')}
            <IconArrowRight size={16} stroke={1.7} />
          </button>
        )}
      </section>

      <section className="workbench-panel">
        <div className="workbench-panel-header">
          <div>
            <h2>{t('agentCreate.governanceTitle', 'Capability governance')}</h2>
            <p>{t('agentCreate.governanceDesc', 'The platform sets boundaries and audit; the HR Agent owns the creation conversation and final employee definition.')}</p>
          </div>
          <IconShieldCheck size={20} stroke={1.6} />
        </div>
        <div className="workbench-rail-list">
          {capabilityRails.map((item) => (
            <div key={item.key} className="workbench-rail-row">
              <IconChecklist size={15} stroke={1.7} />
              <span>{t(`agentCreate.rails.${item.key}`, item.fallback)}</span>
            </div>
          ))}
        </div>
      </section>
    </div>
  );
}
