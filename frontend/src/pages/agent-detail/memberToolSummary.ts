import type { TFunction } from 'i18next';
import { toolFailureForDisplay } from './toolFailurePresentation';

/**
 * X3: expanded member steps for a fixed tool set show one plain sentence, not
 * the model-facing result bytes (memory management notes, colleague refusal
 * pages, whole web pages, capability manifests). The backend computes a typed
 * summary (`kind` + `state` + optional `host`) at member-view serialization
 * time; this module only turns that typed summary into a localized sentence.
 * It never reads or classifies result prose.
 */
export type MemberToolSummaryState = 'done' | 'not_done' | 'in_progress' | 'unknown';

export interface MemberToolSummary {
  kind: string;
  state: MemberToolSummaryState;
  host?: string;
  filename?: string;
  appLabel?: string;
  found?: boolean;
  errorClass?: string;
  status?: string;
  taskId?: string;
}

// X3c r2: one member-safe basename rule, mirroring the backend's
// member_safe_basename. A name that looks like a credential, or one carrying
// a query/fragment, is never shown.
const SECRET_NAME_MARKERS = ['secret', 'credential', 'api_key', 'password', 'token'];
const SECRET_NAME_PATTERNS = [
  /^\.env/i, /^id_rsa/i, /\.pem$/i, /\.key$/i,
  /(?:^|[^a-z0-9])(?:sk-|ghp_|xox[bp]-)/i, /AKIA[0-9A-Z]{16}/, /[A-Za-z0-9]{24,}/,
];

export function memberFilename(value: unknown): string | undefined {
  if (typeof value !== 'string' || !value || /[/\\]$/.test(value)) return undefined;
  const name = value.split(/[/\\]/).pop();
  if (!name || ['.', '..'].includes(name)) return undefined;
  if (/[?#]/.test(name)) return undefined;
  if (/[0-9a-f]{8}-?[0-9a-f]{4}-?[0-9a-f]{4}-?[0-9a-f]{4}-?[0-9a-f]{12}/i.test(name)) return undefined;
  if (SECRET_NAME_MARKERS.some(marker => name.toLowerCase().includes(marker))) return undefined;
  if (SECRET_NAME_PATTERNS.some(pattern => pattern.test(name))) return undefined;
  return name;
}

/** A host is a host (X3c r2): hostname characters only — no `%`, whitespace,
 * `@`, `:`, `/`, `?` or `#` — and it must survive re-parsing `https://{host}/`.
 * The WHATWG URL punycodes IDN labels, so a non-ASCII host that passed the
 * charset check is accepted on its punycode round-trip. */
export function memberHost(value: unknown): string | undefined {
  if (typeof value !== 'string') return undefined;
  const host = value.trim().toLowerCase();
  if (!host || /[\s%@:/?#\\]/.test(host)) return undefined;
  try {
    const parsed = new URL(`https://${host}/`);
    return parsed.hostname === host || /[^\x00-\x7f]/.test(host) ? host : undefined;
  } catch { return undefined; }
}

/** Compatibility-only display fallback when no backend summary exists. */
export function memberHostFromUrl(value: unknown): string | undefined {
  if (typeof value !== 'string' || /[\s\\]/.test(value.trim())) return undefined;
  try {
    const url = new URL(value);
    return ['http:', 'https:'].includes(url.protocol) ? memberHost(url.hostname) : undefined;
  } catch { return undefined; }
}

/** Native result rows keep arguments on the paired call, not on the result. */
export function memberSummaryWithArgs(summary: MemberToolSummary, args?: Record<string, unknown>): MemberToolSummary {
  const result = { ...summary };
  if (summary.kind === 'file_read' || summary.kind === 'file_write' || summary.kind === 'file_edit') {
    result.filename = memberFilename(summary.filename || args?.path || args?.file_path);
  }
  return result;
}

/** The exact backend tool set lives in MEMBER_TOOL_SUMMARY_KINDS; the
 * frontend holds no second copy (X3 r3): "is summarized" is observable as
 * "the result payload carries member_summary". */

const MEMBER_SUMMARY_STATES: ReadonlySet<string> = new Set(['done', 'not_done', 'in_progress', 'unknown']);

export function memberToolSummaryFromRecord(value: unknown): MemberToolSummary | null {
  if (!value || typeof value !== 'object' || Array.isArray(value)) return null;
  const record = value as Record<string, unknown>;
  if (typeof record.kind !== 'string' || !record.kind.trim()) return null;
  if (typeof record.state !== 'string' || !MEMBER_SUMMARY_STATES.has(record.state)) return null;
  const summary: MemberToolSummary = { kind: record.kind, state: record.state as MemberToolSummaryState };
  summary.host = memberHost(record.host);
  summary.filename = memberFilename(record.filename);
  // X3c: the trusted connection label a company MCP row names (backend-resolved).
  const appLabel = record.app_label ?? record.appLabel;
  if (typeof appLabel === 'string' && appLabel.trim()) summary.appLabel = appLabel.trim();
  if (typeof record.found === 'boolean') summary.found = record.found;
  for (const [field, source] of [['errorClass', 'error_class'], ['status', 'status'], ['taskId', 'task_id']] as const) {
    const value = record[source] ?? record[field];
    if (typeof value === 'string') summary[field] = value;
  }
  return summary;
}

type SummarySentenceKey =
  | 'memorySearch' | 'memorySearchFailed'
  | 'memoryRead' | 'memoryReadFailed'
  | 'colleagueResult' | 'colleagueResultFailed'
  | 'taskList' | 'taskListFailed'
  | 'webFetch' | 'webFetchHost' | 'webFetchFailed'
  | 'webSearch' | 'webSearchFailed'
  | 'capabilityLookup' | 'capabilityLookupFailed'
  | 'skillLoad' | 'skillLoadFailed'
  | 'contextRead' | 'contextReadFailed';

const SUMMARY_KEY: Record<string, { done: SummarySentenceKey; notDone: SummarySentenceKey; hostKey?: SummarySentenceKey }> = {
  memory_search: { done: 'memorySearch', notDone: 'memorySearchFailed' },
  memory_read: { done: 'memoryRead', notDone: 'memoryReadFailed' },
  colleague_result: { done: 'colleagueResult', notDone: 'colleagueResultFailed' },
  task_list: { done: 'taskList', notDone: 'taskListFailed' },
  web_fetch: { done: 'webFetch', notDone: 'webFetchFailed', hostKey: 'webFetchHost' },
  web_search: { done: 'webSearch', notDone: 'webSearchFailed' },
  capability_lookup: { done: 'capabilityLookup', notDone: 'capabilityLookupFailed' },
  skill_load: { done: 'skillLoad', notDone: 'skillLoadFailed' },
  context_read: { done: 'contextRead', notDone: 'contextReadFailed' },
};

const FALLBACK: Record<SummarySentenceKey, string> = {
  memorySearch: 'Searched memory',
  memorySearchFailed: 'Memory could not be read this time',
  memoryRead: 'Read memory',
  memoryReadFailed: 'Memory could not be read this time',
  colleagueResult: "Read a colleague's result",
  colleagueResultFailed: "The colleague's result could not be read this time",
  taskList: 'Checked the running tasks',
  taskListFailed: 'The task list could not be read this time',
  webFetch: 'Opened a web page',
  webFetchHost: 'Opened the web page {{host}}',
  webFetchFailed: 'The web page did not open',
  webSearch: 'Searched the web',
  webSearchFailed: 'The web search did not complete',
  capabilityLookup: 'Looked up available capabilities',
  capabilityLookupFailed: 'The capability lookup did not complete',
  skillLoad: 'Loaded a skill',
  skillLoadFailed: 'The skill did not load',
  contextRead: 'Read work context',
  contextReadFailed: "Couldn't read work context this time",
};

/**
 * The one plain sentence a member sees for an expanded step of these tools.
 * An unknown kind returns null so the step falls back to the existing
 * display instead of rendering an empty sentence.
 */
export function memberToolSummarySentence(summary: MemberToolSummary, t: TFunction): string | null {
  const extra = (key: string, fallback: string, values?: Record<string, unknown>) => t(`agent.chat.toolResults.memberSummary.${key}`, fallback, values);
  if (summary.state === 'unknown') return t('agent.chat.disclosure.resultUnconfirmed', 'The outcome is unconfirmed.');
  if (summary.kind === 'delegation') {
    if (summary.errorClass && !summary.taskId) {
      return toolFailureForDisplay({ ok: false, error_class: summary.errorClass }, t, 'failed');
    }
    if (summary.status === 'cancelled') return extra('delegationStopped', 'Stopped; the colleague did not finish this work.');
    if (summary.state === 'done') return extra('delegationDone', 'The colleague has finished.');
    if (summary.state === 'not_done') return extra('delegationFailed', 'The colleague did not finish this work.');
    if (summary.state === 'in_progress') return t('agent.chat.disclosure.colleagueWorking', 'The colleague is working. The result will return here.');
    return t('agent.chat.disclosure.resultUnconfirmed', 'The outcome is unconfirmed.');
  }
  if (['personal_kb_search', 'company_kb_search', 'record_search'].includes(summary.kind)) {
    const action = summary.kind === 'personal_kb_search' ? extra('personalSearch', 'Searched personal materials')
      : summary.kind === 'company_kb_search' ? extra('companySearch', 'Searched company materials') : extra('recordSearch', 'Searched materials');
    return summary.state === 'not_done' ? extra('searchFailed', '{{action}}; the search did not complete.', { action })
      : summary.found === false ? extra('searchEmpty', '{{action}}; no related content was found.', { action })
        : summary.found === true ? extra('searchFound', '{{action}}; found related content.', { action })
          : t('agent.chat.disclosure.resultUnconfirmed', 'The outcome is unconfirmed.');
  }
  if (summary.kind === 'file_list') {
    if (summary.state !== 'done') return extra('fileListFailed', 'The folder could not be listed.');
    return extra('fileList', 'Listed the files in the folder');
  }
  if (summary.kind === 'file_read') {
    if (summary.errorClass === 'auth_or_permission') return t('agent.chat.disclosure.failure.permission', 'You do not have permission to complete this operation.');
    if (summary.state !== 'done') return extra('fileReadFailed', 'The file was not read this time.');
    return summary.filename ? extra('fileReadNamed', 'Read the file “{{filename}}”', { filename: summary.filename })
      : extra('fileRead', 'Read a file');
  }
  // X3c: a successful write/edit row names the file (base name only).
  if (summary.kind === 'file_write') {
    if (summary.state !== 'done') return extra('fileWriteFailed', 'The file was not written this time.');
    return summary.filename ? extra('fileWriteNamed', 'Wrote the file “{{filename}}”', { filename: summary.filename })
      : extra('fileWrite', 'Wrote a file');
  }
  if (summary.kind === 'file_edit') {
    if (summary.state !== 'done') return extra('fileEditFailed', 'The file was not edited this time.');
    return summary.filename ? extra('fileEditNamed', 'Edited the file “{{filename}}”', { filename: summary.filename })
      : extra('fileEdit', 'Edited a file');
  }
  // X3c: a company MCP row names the trusted connection label — never the
  // remote tool name or description.
  if (summary.kind === 'mcp_app') {
    if (summary.state === 'not_done') return summary.appLabel
      ? extra('mcpAppFailedNamed', 'The connected app “{{app}}” did not complete this time', { app: summary.appLabel })
      : extra('mcpAppFailed', 'The connected app did not complete this time');
    return summary.appLabel
      ? extra('mcpAppNamed', 'Used the connected app “{{app}}”', { app: summary.appLabel })
      : extra('mcpApp', 'Used a connected app');
  }
  const entry = SUMMARY_KEY[summary.kind];
  if (!entry) return null;
  if (summary.state === 'in_progress') {
    // R15b's existing sentence — but only a colleague task can truthfully
    // say the colleague is working; any other kind still in flight reads
    // as unconfirmed (X3 r3).
    if (summary.kind !== 'colleague_result') {
      return t('agent.chat.disclosure.resultUnconfirmed', 'The outcome is unconfirmed.');
    }
    return t('agent.chat.disclosure.colleagueWorking', 'The colleague is working. The result will return here.');
  }
  if (summary.state === 'not_done') {
    if (summary.kind === 'web_fetch' && summary.host) return extra('webFetchFailedHost', 'The web page {{host}} did not open', { host: summary.host });
    return t(`agent.chat.toolResults.memberSummary.${entry.notDone}`, FALLBACK[entry.notDone]);
  }
  if (summary.found === false) return extra('searchEmpty', '{{action}}; no related content was found.', {
    action: t(`agent.chat.toolResults.memberSummary.${entry.done}`, FALLBACK[entry.done]),
  });
  if (entry.hostKey && summary.host) {
    return t(`agent.chat.toolResults.memberSummary.${entry.hostKey}`, FALLBACK[entry.hostKey], { host: summary.host });
  }
  return t(`agent.chat.toolResults.memberSummary.${entry.done}`, FALLBACK[entry.done]);
}
