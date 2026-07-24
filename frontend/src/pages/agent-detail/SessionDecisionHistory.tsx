import { useTranslation } from 'react-i18next';

import type { SessionDecisionTrace } from '../../api/domains/chat';

type DecisionTranslator = (key: string, defaultValue: string) => string;

const DECISION_ACTION_FALLBACKS: Record<string, string> = {
  create_digital_employee: 'Create a digital employee',
  send_feishu_message: 'Send Feishu Message',
  send_channel_message: 'Send a channel message',
  send_channel_file: 'Send a channel file',
  send_email: 'Send an email',
  reply_email: 'Reply to an email',
  feishu_calendar_create: 'Create a calendar event',
  feishu_calendar_update: 'Update a calendar event',
  feishu_calendar_delete: 'Cancel a calendar event',
  write_file: 'Update a workspace file',
  delete_file: 'Delete a workspace file',
  execute_code: 'Run code',
  run_command: 'Run a command',
  set_trigger: 'Change a schedule',
  import_mcp_server: 'Connect an external service',
  send_message_to_agent: 'Message another digital employee',
  delegate_to_agent: 'Delegate to another digital employee',
};

function decisionActionLabel(value: string, t: DecisionTranslator): string {
  const normalized = String(value || '').trim().toLowerCase();
  const fallback = DECISION_ACTION_FALLBACKS[normalized];
  return fallback
    ? t(`sessionWorkbench.rightPanel.decisionActions.${normalized}`, fallback)
    : t('sessionWorkbench.rightPanel.decisionActions.generic', 'Agent action');
}

function decisionOutcomeLabel(outcome: string, t: DecisionTranslator): string {
  const normalized = String(outcome || '').trim().toLowerCase();
  const fallbacks: Record<string, string> = {
    ask: 'Approval needed',
    escalate: 'Admin review needed',
    prepare_only: 'Prepared only',
    refuse: 'Blocked',
  };
  const fallback = fallbacks[normalized];
  return fallback
    ? t(`sessionWorkbench.rightPanel.decisionOutcomes.${normalized}`, fallback)
    : t('sessionWorkbench.rightPanel.decisionOutcomes.recorded', 'Decision recorded');
}

function decisionReasonLabel(reason: string, t: DecisionTranslator): string {
  const normalized = String(reason || '').trim().toLowerCase();
  const fallbacks: Record<string, string> = {
    charter_confirm_first: 'Your settings require approval',
    charter_never_do: 'Your settings prohibit this action',
    company_boundary_conflict: 'Company policy needs an administrator',
    runtime_permission_denied: 'This session does not have permission',
    truth_search_unavailable: 'Required evidence is temporarily unavailable',
    pl4_zero_retention: 'Credential data cannot be retained',
  };
  if (fallbacks[normalized]) {
    return t(`sessionWorkbench.rightPanel.decisionReasons.${normalized}`, fallbacks[normalized]);
  }
  if (normalized.startsWith('high_risk_axis:')) {
    const axis = normalized.slice('high_risk_axis:'.length);
    const axisFallbacks: Record<string, string> = {
      visibility: 'Externally visible action',
      representativeness: 'Acts on your behalf',
      reversibility: 'Difficult to reverse',
      judgment_density: 'Requires important judgment',
      domain_specialization: 'Requires specialist review',
    };
    const fallback = axisFallbacks[axis];
    return fallback
      ? t(`sessionWorkbench.rightPanel.decisionReasons.high_risk_${axis}`, fallback)
      : t('sessionWorkbench.rightPanel.decisionReasons.high_risk_generic', 'Higher-risk action');
  }
  if (normalized.startsWith('medium_risk_axis:')) {
    return t(
      'sessionWorkbench.rightPanel.decisionReasons.prepared_without_sending',
      'Action was prepared without sending',
    );
  }
  if (normalized.startsWith('owner_action_policy_invalid:')) {
    return t(
      'sessionWorkbench.rightPanel.decisionReasons.settings_attention',
      'Action settings need attention',
    );
  }
  return t(
    'sessionWorkbench.rightPanel.decisionReasons.governance_required',
    'Governance policy required this decision',
  );
}

export function SessionDecisionHistory({
  decisions,
  onFeedback,
}: {
  decisions: SessionDecisionTrace[];
  onFeedback?: (decision: SessionDecisionTrace, label: 'useful' | 'misleading') => void | Promise<unknown>;
}) {
  const { t } = useTranslation();
  if (decisions.length === 0) return null;
  return (
    <section
      data-testid="session-decision-history"
      className="session-runtime-decision-history"
      aria-label={t('sessionWorkbench.rightPanel.actionDecisions', 'Action decisions')}
    >
      <div className="session-runtime-section-header">
        <div>
          <div className="session-tui-kicker">{t('sessionWorkbench.rightPanel.governance', 'Governance')}</div>
          <h3>{t('sessionWorkbench.rightPanel.actionDecisions', 'Action decisions')}</h3>
        </div>
        <span>{decisions.length}</span>
      </div>
      <div className="session-runtime-list">
        {decisions.map((decision) => (
          <article key={decision.id} className="session-runtime-decision">
            <div className="session-runtime-row">
              <span className="session-runtime-row-main">
                <strong className="session-runtime-row-title">
                  {decisionActionLabel(decision.tool_name || decision.action, t)}
                </strong>
                <span className="session-runtime-row-meta">
                  {(decision.reason_codes || []).map((reason) => decisionReasonLabel(reason, t)).join(' · ')}
                </span>
              </span>
              <span className="session-runtime-status">{decisionOutcomeLabel(decision.outcome, t)}</span>
            </div>
            {onFeedback ? (
              <div
                className="session-runtime-actions"
                aria-label={t('sessionWorkbench.rightPanel.decisionFeedback', 'Decision feedback')}
              >
                <button
                  type="button"
                  className="session-runtime-action-button"
                  onClick={() => void onFeedback(decision, 'useful')}
                >
                  {t('sessionWorkbench.rightPanel.decisionHelpful', 'Helpful')}
                </button>
                <button
                  type="button"
                  className="session-runtime-action-button"
                  onClick={() => void onFeedback(decision, 'misleading')}
                >
                  {t('sessionWorkbench.rightPanel.decisionMisleading', 'Misleading')}
                </button>
                {decision.feedback_count > 0 ? (
                  <span className="session-runtime-row-meta">
                    {t('sessionWorkbench.rightPanel.feedbackCount', '{{count}} feedback', {
                      count: decision.feedback_count,
                    })}
                  </span>
                ) : null}
              </div>
            ) : null}
          </article>
        ))}
      </div>
    </section>
  );
}
