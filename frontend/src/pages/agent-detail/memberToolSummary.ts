import type { TFunction } from 'i18next';

/**
 * X3: expanded member steps for a fixed tool set show one plain sentence, not
 * the model-facing result bytes (memory management notes, colleague refusal
 * pages, whole web pages, capability manifests). The backend computes a typed
 * summary (`kind` + optional `host`/`count` + `failed`) at member-view
 * serialization time; this module only turns that typed summary into a
 * localized sentence. It never reads or classifies result prose.
 */
export interface MemberToolSummary {
  kind: string;
  failed: boolean;
  host?: string;
  count?: number;
}

export function memberToolSummaryFromRecord(value: unknown): MemberToolSummary | null {
  if (!value || typeof value !== 'object' || Array.isArray(value)) return null;
  const record = value as Record<string, unknown>;
  if (typeof record.kind !== 'string' || !record.kind.trim()) return null;
  if (typeof record.failed !== 'boolean') return null;
  const summary: MemberToolSummary = { kind: record.kind, failed: record.failed };
  if (typeof record.host === 'string' && record.host.trim()) summary.host = record.host.trim();
  if (typeof record.count === 'number' && Number.isFinite(record.count) && record.count >= 0) {
    summary.count = record.count;
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
  | 'skillLoad' | 'skillLoadFailed';

const SUMMARY_KEY: Record<string, { done: SummarySentenceKey; failed: SummarySentenceKey; hostKey?: SummarySentenceKey }> = {
  memory_search: { done: 'memorySearch', failed: 'memorySearchFailed' },
  memory_read: { done: 'memoryRead', failed: 'memoryReadFailed' },
  colleague_result: { done: 'colleagueResult', failed: 'colleagueResultFailed' },
  task_list: { done: 'taskList', failed: 'taskListFailed' },
  web_fetch: { done: 'webFetch', failed: 'webFetchFailed', hostKey: 'webFetchHost' },
  web_search: { done: 'webSearch', failed: 'webSearchFailed' },
  capability_lookup: { done: 'capabilityLookup', failed: 'capabilityLookupFailed' },
  skill_load: { done: 'skillLoad', failed: 'skillLoadFailed' },
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
};

/** The one plain sentence a member sees for an expanded step of these tools. */
export function memberToolSummarySentence(summary: MemberToolSummary, t: TFunction): string {
  const entry = SUMMARY_KEY[summary.kind];
  if (!entry) return '';
  if (summary.failed) return t(`agent.chat.toolResults.memberSummary.${entry.failed}`, FALLBACK[entry.failed]);
  if (entry.hostKey && summary.host) {
    return t(`agent.chat.toolResults.memberSummary.${entry.hostKey}`, FALLBACK[entry.hostKey], { host: summary.host });
  }
  return t(`agent.chat.toolResults.memberSummary.${entry.done}`, FALLBACK[entry.done]);
}
