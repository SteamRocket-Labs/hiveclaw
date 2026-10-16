export function blueprintStatusLabel(status: string, t: (key: string, fallback: string) => string): string {
  switch (status) {
    case 'awaiting_confirmation':
      return t('agent.chat.toolResults.statusNeedsConfirmation', 'Needs confirmation');
    case 'confirmed':
      return t('agent.chat.toolResults.statusConfirmed', 'Confirmed');
    case 'creating':
    case 'provisioning':
      return t('agent.chat.toolResults.statusProvisioning', 'Creating');
    case 'completed':
      return t('agent.chat.toolResults.statusCompleted', 'Created');
    case 'failed':
      return t('agent.chat.toolResults.statusFailed', 'Needs attention');
    case 'rejected':
      return t('agent.chat.toolResults.statusRejected', 'Rejected');
    case 'superseded':
      return t('agent.chat.toolResults.statusSuperseded', 'Revised');
    case 'expired':
      return t('agent.chat.toolResults.statusExpired', 'Expired');
    default:
      return t('agent.chat.toolResults.statusUnavailable', 'Status unavailable');
  }
}
