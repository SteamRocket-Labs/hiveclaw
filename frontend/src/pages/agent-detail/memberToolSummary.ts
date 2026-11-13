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
    return t(`agent.chat.toolResults.memberSummary.${entry.notDone}`, FALLBACK[entry.notDone]);
  }
  if (entry.hostKey && summary.host) {
    return t(`agent.chat.toolResults.memberSummary.${entry.hostKey}`, FALLBACK[entry.hostKey], { host: summary.host });
  }
  return t(`agent.chat.toolResults.memberSummary.${entry.done}`, FALLBACK[entry.done]);
}
