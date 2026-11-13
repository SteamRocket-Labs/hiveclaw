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

const READ_FAILURE_KIND: Record<string, string> = {
  auth_or_permission: 'permission', auth_required: 'permission', forbidden: 'permission',
  authz_policy_violation: 'permission', content_source_access_denied: 'permission',
  not_found: 'readMissing',
  workspace_input_capture_unavailable: 'readInputUnavailable', provider_unavailable: 'readInputUnavailable',
  content_source_changed: 'readChanged', workspace_input_revision_changed: 'readChanged',
};

/** Only typed refusal reasons/classes decide the sentence; never error prose. */
export function fileReadFailureForDisplay(reason: unknown, t: TFunction): string {
  const kind = typeof reason === 'string' && Object.prototype.hasOwnProperty.call(READ_FAILURE_KIND, reason)
    ? READ_FAILURE_KIND[reason] : undefined;
  return kind ? t(`agent.chat.disclosure.failure.${kind}`, 'The file could not be read this time.')
    : t('agent.chat.toolResults.memberSummary.fileReadFailed', 'The file was not read this time.');
}

/** Only tool failure envelopes are projected; assistant prose is never scanned. */
export function toolFailureForDisplay(details: unknown, t: TFunction, status: RunStepStatus): string | null {
  const record = details && typeof details === 'object' ? details as Record<string, unknown> : {};
  const raw = typeof details === 'string' ? details : record.result ?? record.output ?? record.rawResult;
  if (typeof raw === 'string') {
    try {
      const result = JSON.parse(raw);
      if (result?.ok === true) return null;
    } catch { /* Native failure envelopes include text before their JSON. */ }
  }
  if (status !== 'failed' && !(typeof raw === 'string' && (raw.trimStart().startsWith('<tool_error>')
    || (raw.startsWith('❌') && raw.includes('<tool_error>'))))) return null;
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
  if (['read_file', 'fs_read', 'read_document'].includes(String(error.tool_name || record.toolName))) {
    return fileReadFailureForDisplay(error.reason_code || error.error_class, t);
  }
  if (error.provider === 'mcp' && ['auth_required', 'configuration_required'].includes(errorClass)
    && error.diagnostic_hint === '请管理员检查应用连接地址。') {
    return t('agent.chat.disclosure.failure.connectionAdmin', 'The app connection is unavailable. Ask an administrator to check its settings.');
  }
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

/** Project known platform envelopes, including the JSON lines in memory results. */
export function toolOutputForDisplay(details: unknown, t: TFunction, status: RunStepStatus, projectLines = true): string | null {
  const failure = toolFailureForDisplay(details, t, status);
  if (failure) return failure;
  const record = details && typeof details === 'object' ? details as Record<string, unknown> : {};
  const raw = typeof details === 'string' ? details : record.result ?? record.output ?? record.rawResult;
  const summary = (value: unknown): string | null => {
    if (!value || typeof value !== 'object' || Array.isArray(value)) return null;
    const envelope = value as Record<string, unknown>;
    if (envelope.schema === 'hive.session_recall_error.v1') {
      return t('agent.chat.disclosure.memoryUnavailable', 'This part of the conversation history is temporarily unavailable. Please try again later.');
    }
    if (envelope.schema === 'hive.explicit_memory_availability.v1') {
      return t('agent.chat.disclosure.memoryPartial', 'Only some memories are available. Unavailable memories cannot support conclusions; please try again later.');
    }
    return null;
  };
  if (typeof raw !== 'string') return summary(raw);
  try { return summary(JSON.parse(raw)); } catch { /* Memory producers also return headings followed by JSON lines. */ }
  if (!projectLines) return null;
  const notices = raw.split(/\r?\n/).flatMap(line => {
    try {
      const text = summary(JSON.parse(line));
      if (text) return [text];
    } catch { /* Only known typed notices reach the member disclosure. */ }
    return [];
  });
  return notices.length ? notices.join('\n') : null;
}
