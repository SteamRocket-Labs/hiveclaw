import type { TFunction } from 'i18next';
import type { RunStepStatus } from './chatDisclosureReducer';

const FAILURE_GROUPS: Array<[string, string[]]> = [
  ['arguments', ['bad_arguments', 'invalid_arguments', 'invalid_input', 'invalid_tool_arguments', 'invalid_approval_form', 'provider_bad_request', 'wrong_tool']],
  ['permission', ['auth_or_permission', 'auth_required', 'forbidden', 'authz_policy_violation', 'unauthorized_secret_bytes', 'plan_mode_readonly_violation']],
  ['authority', ['authority_context_unavailable', 'missing_handoff_authority']],
  ['approval', ['approval_required']],
  ['changed', ['approval_asset_revision_drift', 'approval_payload_mutation', 'approval_hook_block']],
  ['quota', ['quota_or_billing']], ['rate', ['rate_limited']], ['missing', ['not_found']],
  ['timeout', ['timeout']], ['stopped', ['cancelled']],
  ['configuration', ['configuration_required', 'not_configured', 'provider_not_configured', 'extension_disabled']],
  ['unavailable', ['provider_unavailable', 'backend_unavailable', 'transport_failure', 'governance_dependency_unavailable', 'runtime_budget_service_unavailable', 'approval_ticket_unavailable']],
  ['empty', ['empty_content', 'provider_empty_response']], ['incomplete', ['incomplete_content', 'provider_bad_response']],
  ['file', ['unsupported_file_type', 'unreadable_pdf']], ['state', ['bad_state']],
];
const FAILURE_KIND = Object.fromEntries(FAILURE_GROUPS.flatMap(([kind, names]) => names.map(name => [name, kind])));

/** Only tool failure envelopes are projected; assistant prose is never scanned. */
export function toolFailureForDisplay(details: unknown, t: TFunction, status: RunStepStatus): string | null {
  if (status !== 'failed') return null;
  const record = details && typeof details === 'object' ? details as Record<string, unknown> : {};
  const raw = typeof details === 'string' ? details : record.result ?? record.output ?? record.rawResult;
  if (typeof raw === 'string') {
    try {
      const result = JSON.parse(raw);
      if (result?.ok === true) return null;
    } catch { /* Native failure envelopes include text before their JSON. */ }
  }
  let error: Record<string, unknown>;
  if (typeof raw === 'string' && raw.includes('<tool_error>')) {
    const body = raw.split('<tool_error>', 2)[1]?.split('</tool_error>', 1)[0];
    try {
      const parsed = JSON.parse(body || '');
      error = parsed && typeof parsed === 'object' && !Array.isArray(parsed) ? parsed : {};
    } catch { error = {}; }
  } else if (record.ok === false && typeof record.error_class === 'string') {
    error = record;
  } else {
    return null;
  }
  const errorClass = String(error?.error_class);
  const kind = Object.prototype.hasOwnProperty.call(FAILURE_KIND, errorClass) ? FAILURE_KIND[errorClass] : 'generic';
  if (kind === 'timeout') {
    // Read the exact historical native timeout format to preserve its duration.
    const legacySeconds = typeof error.message === 'string'
      ? error.message.match(/ exceeded the (\d+) second time limit\.$/)?.[1] : undefined;
    const seconds = error.timeout_seconds ?? legacySeconds;
    if (seconds != null && Number.isFinite(Number(seconds))) {
      return t('agent.chat.disclosure.failure.timeoutDuration', 'The operation did not finish within {{seconds}} seconds; the outcome is unknown.', { seconds });
    }
  }
  return t(`agent.chat.disclosure.failure.${kind}`, 'The operation did not complete; the outcome is unconfirmed.');
}
