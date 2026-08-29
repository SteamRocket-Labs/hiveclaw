import { useTranslation } from 'react-i18next';


export interface HrCreationHandoffReceipt {
  hrAgentId: string;
  hrSessionId: string;
  sourceAgentName: string | null;
}

export function parseHrCreationHandoffResult(rawResult: unknown): HrCreationHandoffReceipt | null {
  try {
    const parsed = typeof rawResult === 'string' ? JSON.parse(rawResult) : rawResult;
    if (
      parsed?.ok === true
      && (parsed.status === 'hr_handoff_started' || parsed.status === 'hr_handoff_queued')
      && typeof parsed.hr_agent_id === 'string' && parsed.hr_agent_id.trim()
      && typeof parsed.hr_session_id === 'string' && parsed.hr_session_id.trim()
    ) {
      return {
        hrAgentId: parsed.hr_agent_id.trim(),
        hrSessionId: parsed.hr_session_id.trim(),
        sourceAgentName: typeof parsed.source_agent_name === 'string' ? parsed.source_agent_name : null,
      };
    }
  } catch {
    // The card below presents a stable recovery message instead of raw tool output.
  }
  return null;
}


export default function HrCreationHandoffCard({ rawResult }: { rawResult?: string }) {
  const { t } = useTranslation();
  const meta = parseHrCreationHandoffResult(rawResult);
  if (!meta) {
    return (
      <div role="alert" style={{ fontSize: '12px', color: 'var(--text-secondary)' }}>
        {t('agent.chat.toolResults.handoffUnavailable', 'HR Agent handoff could not be opened. Ask the Agent to retry.')}
      </div>
    );
  }
  const continueHref = `/agents/${encodeURIComponent(meta.hrAgentId)}?session_id=${encodeURIComponent(meta.hrSessionId)}#chat`;
  return (
    <div style={{ display: 'grid', gap: '8px' }}>
      <div style={{ fontSize: '12px', fontWeight: 700, color: 'var(--text-primary)' }}>
        {t('agent.chat.toolResults.handoffTitle', 'Creation request handed off')}
      </div>
      <div style={{ fontSize: '12px', color: 'var(--text-secondary)' }}>
        {meta.sourceAgentName
          ? `${meta.sourceAgentName} ${t(
              'agent.chat.toolResults.handoffFrom',
              'handed this creation request to HR Agent.',
            )}`
          : t('agent.chat.toolResults.handoffGeneric', 'This creation request has been handed to HR Agent.')}
      </div>
      <a className="btn btn-primary" href={continueHref}>
        {t('agent.chat.toolResults.handoffContinue', 'Continue with HR Agent')}
      </a>
    </div>
  );
}
