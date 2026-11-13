import type { TFunction } from 'i18next';

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
  found?: boolean;
  count?: number;
  errorClass?: string;
  status?: string;
  taskId?: string;
}

export function memberFilename(value: unknown): string | undefined {
  if (typeof value !== 'string' || !value || /[/\\]$/.test(value)) return undefined;
  const name = value.split(/[/\\]/).pop();
  return name && !['.', '..'].includes(name) && !/[0-9a-f]{8}-?[0-9a-f]{4}-?[0-9a-f]{4}-?[0-9a-f]{4}-?[0-9a-f]{12}/i.test(name)
    ? name : undefined;
}

/** Native result rows keep arguments on the paired call, not on the result. */
export function memberSummaryWithArgs(summary: MemberToolSummary, args?: Record<string, unknown>): MemberToolSummary {
  const result = { ...summary };
  if (summary.kind === 'web_fetch' && typeof args?.url === 'string') {
    delete result.host;
    try {
      const url = new URL(args.url);
      if (['http:', 'https:'].includes(url.protocol) && !/[\s\\]/.test(args.url.trim())) result.host = url.hostname;
    } catch { /* Missing or invalid typed URLs have no host. */ }
  }
  if (summary.kind === 'file_read') result.filename = memberFilename(summary.filename || args?.path || args?.file_path);
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
  if (typeof record.host === 'string' && record.host.trim()) summary.host = record.host.trim();
  summary.filename = memberFilename(record.filename);
  if (typeof record.found === 'boolean') summary.found = record.found;
  if (typeof record.count === 'number' && Number.isInteger(record.count) && record.count >= 0) summary.count = record.count;
  for (const [field, source] of [['errorClass', 'error_class'], ['status', 'status'], ['taskId', 'task_id']] as const) {
    if (typeof record[source] === 'string') summary[field] = record[source];
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
  if (summary.kind === 'delegation') {
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
    return summary.count === undefined ? extra('fileList', 'Listed the files in the folder')
      : extra('fileListCount', 'Listed {{count}} files in the folder', { count: summary.count });
  }
  if (summary.kind === 'file_read') {
    if (summary.errorClass === 'auth_or_permission') return t('agent.chat.disclosure.failure.permission', 'You do not have permission to complete this operation.');
    if (summary.state !== 'done') return extra('fileReadFailed', 'The file was not read this time.');
    return summary.filename ? extra('fileReadNamed', 'Read the file “{{filename}}”', { filename: summary.filename })
      : extra('fileRead', 'Read a file');
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
  if (summary.state === 'unknown') {
    // The existing unconfirmed-outcome sentence (R17b/R19b tool output).
    return t('agent.chat.disclosure.resultUnconfirmed', 'The outcome is unconfirmed.');
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
