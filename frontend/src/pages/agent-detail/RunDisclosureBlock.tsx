import { runtimeStatusLabel } from './runtimeStatus';
import React from 'react';
import ReactMarkdown from 'react-markdown';
import { useTranslation } from 'react-i18next';
import i18n, { type TFunction } from 'i18next';
import {
  IconChevronDown,
  IconChevronRight,
  IconCircleCheck,
  IconFileText,
  IconHelp,
  IconLoader2,
  IconSearch,
  IconTerminal2,
  IconTool,
} from '@tabler/icons-react';

import MarkdownRenderer from '../../components/MarkdownRenderer';
import { assistantReplyForDisplay } from './CanonicalCardAssistantSupplement';
import { approvalActionFor } from '../session-workbench/approvalActionPresentation';
import { toolStepLabel as localizedToolName } from './toolStepLabel';
import { toolOutputForDisplay } from './toolFailurePresentation';
import { PastedCredentialText } from '../session-workbench/PastedCredentialNotice';
import { isKeptResultStep, presentSystemStep } from './systemStepPresentation';
import type {
  RunStepKind,
  RunStepPresentation,
  RunAggregateSummary,
  RunStepSnapshot,
  RunTimelineSnapshot,
} from './chatDisclosureReducer';

// One Session turn owns one disclosure. Live or failed work opens for immediate
// inspection; terminal successful work folds behind its processed summary.

type RunRenderItem =
  | { kind: 'tool_group'; id: string; steps: RunStepSnapshot[] }
  | { kind: 'step'; id: string; step: RunStepSnapshot };

export function getRunStepPresentation(step: RunStepSnapshot): RunStepPresentation {
  if (step.presentation) return step.presentation;
  if (step.status === 'failed' || step.status === 'blocked' || step.status === 'cancelled') return 'surface';
  if (
    step.kind === 'reasoning'
    || step.kind === 'commentary'
    || step.kind === 'prose'
    || step.kind === 'compaction'
  ) return 'process';
  if (step.kind === 'tool' || step.kind === 'search' || step.kind === 'file') return 'tool_history';
  return 'surface';
}

function groupRunSteps(steps: RunStepSnapshot[]): RunRenderItem[] {
  const items: RunRenderItem[] = [];
  for (const step of steps) {
    if (getRunStepPresentation(step) !== 'tool_history') {
      items.push({ kind: 'step', id: step.id, step });
      continue;
    }
    const previous = items[items.length - 1];
    if (previous?.kind === 'tool_group') {
      previous.steps.push(step);
      continue;
    }
    items.push({ kind: 'tool_group', id: `tool-group:${step.id}`, steps: [step] });
  }
  return items;
}

function formatDuration(durationMs?: number): string {
  if (!durationMs || durationMs < 1000) return '';
  const seconds = Math.round(durationMs / 1000);
  if (seconds < 60) return i18n.t('agent.chat.disclosure.elapsedSeconds', '{{seconds}}s', { seconds }) || `${seconds}s`;
  const minutes = Math.floor(seconds / 60);
  const remainder = seconds % 60;
  return remainder > 0
    ? i18n.t('agent.chat.disclosure.elapsedMinutesSeconds', '{{minutes}}m {{seconds}}s', { minutes, seconds: remainder }) || `${minutes}m ${remainder}s`
    : i18n.t('agent.chat.disclosure.elapsedMinutes', '{{minutes}}m', { minutes }) || `${minutes}m`;
}

const OBSERVED_RUN_DURATION_STORAGE_PREFIX = 'hive.session.observed-run-duration.v1:';

function readObservedRunDuration(presentationKey: string | null): number {
  if (!presentationKey || typeof window === 'undefined') return 0;
  try {
    const value = Number(window.sessionStorage.getItem(`${OBSERVED_RUN_DURATION_STORAGE_PREFIX}${presentationKey}`));
    return Number.isFinite(value) && value > 0 ? value : 0;
  } catch {
    return 0;
  }
}

function persistObservedRunDuration(presentationKey: string | null, durationMs: number): void {
  if (!presentationKey || durationMs <= 0 || typeof window === 'undefined') return;
  try {
    const storageKey = `${OBSERVED_RUN_DURATION_STORAGE_PREFIX}${presentationKey}`;
    const previous = Number(window.sessionStorage.getItem(storageKey));
    if (!Number.isFinite(previous) || durationMs > previous) {
      window.sessionStorage.setItem(storageKey, String(durationMs));
    }
  } catch {
    // Session presentation persistence is best-effort; durable runtime
    // evidence remains the fallback when browser storage is unavailable.
  }
}

function useRunDuration(timeline: RunTimelineSnapshot, presentationKey: string | null): string {
  const running = timeline.status === 'running';
  const parsed = timeline.startedAt ? Date.parse(timeline.startedAt) : NaN;
  const computeLiveDuration = React.useCallback(() => {
    if (!running || Number.isNaN(parsed)) return 0;
    // A live clock can observe the terminal frame slightly after the durable
    // completion timestamp. Preserve only fully elapsed seconds so that
    // sub-second transport latency cannot add a second which disappears on
    // reload when the authoritative duration is rendered.
    return Math.floor(Math.max(0, Date.now() - parsed) / 1000) * 1000;
  }, [parsed, running]);
  const observationKey = presentationKey || timeline.id;
  const [observed, setObserved] = React.useState(() => ({
    observationKey,
    durationMs: Math.max(computeLiveDuration(), readObservedRunDuration(presentationKey)),
  }));
  // A delayed terminal projection may carry an execution duration shorter
  // than the elapsed time the user has already watched. Preserve the largest
  // observed value for this mounted run so completion never moves backwards.
  React.useEffect(() => {
    const observe = () => {
      const durationMs = running ? computeLiveDuration() : timeline.durationMs ?? 0;
      setObserved((previous) => previous.observationKey === observationKey
        ? { ...previous, durationMs: Math.max(previous.durationMs, durationMs) }
        : {
            observationKey,
            durationMs: Math.max(durationMs, readObservedRunDuration(presentationKey)),
          });
    };
    observe();
    if (!running || Number.isNaN(parsed)) return undefined;
    const timer = window.setInterval(observe, 1000);
    return () => window.clearInterval(timer);
  }, [computeLiveDuration, observationKey, parsed, presentationKey, running, timeline.durationMs]);
  const observedDurationMs = observed.observationKey === observationKey
    ? observed.durationMs
    : Math.max(
        running ? computeLiveDuration() : timeline.durationMs ?? 0,
        readObservedRunDuration(presentationKey),
      );
  React.useEffect(() => {
    if (!timeline.durationIsFinal) persistObservedRunDuration(presentationKey, observedDurationMs);
  }, [observedDurationMs, presentationKey, timeline.durationIsFinal]);
  if (timeline.durationIsFinal) return formatDuration(timeline.durationMs);
  const duration = formatDuration(Math.max(observedDurationMs, timeline.durationMs ?? 0));
  if (duration) return duration;
  return running && !Number.isNaN(parsed) ? i18n.t('agent.chat.disclosure.elapsedSeconds', '{{seconds}}s', { seconds: 0 }) : '';
}

function StepIcon({ kind, running }: { kind: RunStepKind; running: boolean }) {
  const size = 14;
  if (running) {
    return <IconLoader2 size={size} stroke={2.2} className="session-tui-step-spinner" />;
  }
  if (kind === 'search') return <IconSearch size={size} stroke={2.1} />;
  if (kind === 'file') return <IconFileText size={size} stroke={2.1} />;
  if (kind === 'command') return <IconTerminal2 size={size} stroke={2.1} />;
  if (kind === 'question' || kind === 'plan' || kind === 'permission') return <IconHelp size={size} stroke={2.1} />;
  if (kind === 'reasoning') return <IconCircleCheck size={size} stroke={2.1} />;
  return <IconTool size={size} stroke={2.1} />;
}

/** One plain line for raw provider text; never a wall of markdown. */
function compactStepText(text: string, limit: number): string {
  const normalized = text.replace(/\s+/g, ' ').trim();
  return normalized.length <= limit ? normalized : `${normalized.slice(0, Math.max(0, limit - 1))}…`;
}

function commandOutcome(details: unknown): 'done' | 'failed' | 'unknown' | null {
  const record = details && typeof details === 'object' ? details as Record<string, unknown> : {};
  const execution = record.executionResult as { exit_code?: unknown; timed_out?: unknown } | undefined;
  if (execution) {
    if (execution.timed_out === true || typeof execution.exit_code !== 'number' || !Number.isFinite(execution.exit_code)) return 'unknown';
    return execution.exit_code === 0 ? 'done' : 'failed';
  }
  const values = [details, record.result, record.rawResult];
  const records = values.flatMap(value => {
    if (typeof value === 'string') {
      try { value = JSON.parse(value); } catch { return []; }
    }
    return value && typeof value === 'object' && !Array.isArray(value) ? [value as Record<string, unknown>] : [];
  });
  if (records.some(value => value.timed_out === true || value.ok === false || value.error || value.error_class)) return 'unknown';
  const exitCodes = records.flatMap(value => typeof value.exit_code === 'number' && Number.isFinite(value.exit_code)
    ? [value.exit_code] : []);
  if (exitCodes.some(code => code !== 0)) return 'failed';
  return exitCodes.includes(0) ? 'done' : null;
}

function RunStepDetails({ step }: { step: RunStepSnapshot }) {
  const { t } = useTranslation();
  if (toolNameOf(step) || ['tool', 'file', 'search', 'command'].includes(step.kind)) {
    const toolName = toolNameOf(step);
    const toolDetails = step.details as { rawResult?: unknown; result?: unknown } | null;
    const raw = toolDetails?.rawResult ?? toolDetails?.result;
    if (['delegate_to_agent', 'send_message_to_agent', 'check_async_task', 'list_async_tasks', 'task_output'].includes(toolName || '') && typeof raw === 'string') {
      try {
        const envelope = JSON.parse(raw);
        const tasks = toolName === 'list_async_tasks' ? envelope
          : toolName === 'task_output' ? [envelope?.task] : toolName === 'check_async_task' ? [envelope] : [];
        if ((envelope?.ok === true && envelope.operation === 'delegate'
          && ['queued', 'pending', 'running'].includes(envelope.status))
          || (Array.isArray(tasks) && tasks.some(task => task?.task_type === 'delegation' && ['pending', 'running'].includes(task.status)))) {
          return <p>{t('agent.chat.disclosure.colleagueWorking', 'The colleague is working. The result will return here.')}</p>;
        }
      } catch { /* Unstructured colleague replies retain their existing presentation. */ }
    }
    const command = step.kind === 'command' || ['run_command', 'execute_code', 'run_skill_tool'].includes(toolName || '');
    const outcome = command ? commandOutcome(step.details) : null;
    const summary = command && outcome !== null ? null : toolOutputForDisplay(step.details, t, step.status, !command);
    if (summary) return <p>{summary}</p>;
    const details = step.details as { args?: Record<string, unknown>; result?: unknown } | null;
    const result = typeof step.details === 'string' ? step.details : details?.result;
    // Raw non-command tool text can reach members as on production; backend per-tool member summaries will replace it.
    if (!command && typeof result === 'string' && result.trim()) {
      try {
        JSON.parse(result);
      } catch {
        // Provider dumps (page scrapes, feeds) are not member prose: long raw
        // text reads as one short plain line (X1 run 15 step 8c); short plain
        // results keep their line layout.
        const trimmed = result.trim();
        if (trimmed.length > 400) {
          return <p className="run-detail-summary">{compactStepText(trimmed, 200)}</p>;
        }
        const lines = trimmed.split(/\r?\n/);
        return <p>{lines.map((line, index) => (
          <React.Fragment key={index}>
            {index > 0 && <br />}
            <PastedCredentialText content={line} />
            {line && index < lines.length - 1 && ' '}
          </React.Fragment>
        ))}</p>;
      }
    }
    const path = step.kind === 'file' ? details?.args?.path ?? details?.args?.file_path : null;
    const file = typeof path === 'string' ? path.split(/[\\/]/).filter(Boolean).pop() : undefined;
    const status = outcome === 'unknown' ? t('agent.chat.disclosure.resultUnconfirmed', 'The outcome is unconfirmed.')
      : outcome === 'done' ? t('agent.chat.disclosure.resultSucceeded', 'Succeeded')
      : outcome === 'failed' || step.status === 'failed'
        ? command ? runtimeStatusLabel('failed', t)
          : t('agent.chat.disclosure.failure.generic', 'The operation did not complete; the outcome is unconfirmed.')
      : step.status === 'interrupted' ? t('agent.chat.disclosure.stepInterrupted', 'Interrupted; no result was returned.')
      : step.status !== 'done' ? runtimeStatusLabel(step.status, t)
          : command ? t('agent.chat.disclosure.resultUnconfirmed', 'The outcome is unconfirmed.')
            : t('agent.chat.disclosure.resultReturned', 'Result returned');
    const action = localizedToolTitle(step, t);
    return <p>{file
      ? t('agent.chat.disclosure.resultFile', '{{action}}: {{file}}. {{status}}', { action, file, status })
      : t('agent.chat.disclosure.resultAction', '{{action}}. {{status}}', { action, status })}</p>;
  }
  const detailText = typeof step.details === 'string' ? step.details : '';
  if (detailText.includes('[REDACTED_SECRET]')) return <pre className="run-detail-pre"><PastedCredentialText content={detailText} /></pre>;
  return detailText ? <MarkdownRenderer content={detailText} /> : null;
}

function RunStepRow({ step, resultPreview }: { step: RunStepSnapshot; resultPreview: boolean }) {
  const { t } = useTranslation();
  const running = step.status === 'running';
  // Commands, arguments and output wait behind this toggle: the default layer
  // shows the localized action and its state only (B2 r2).
  const [expanded, setExpanded] = React.useState(false);
  const hasDetails = hasStepDetails(step);
  const preview = resultPreview && step.systemEvent?.type === 'child_session' && typeof step.details === 'string'
    ? step.details : step.summary;
  return (
    <div
      data-testid="run-disclosure-step"
      data-presentation={getRunStepPresentation(step)}
      className={getRunStepPresentation(step) === 'surface' ? 'run-step is-surfaced' : 'run-step'}
    >
      <span className={running ? 'run-step-icon is-running' : 'run-step-icon'}>
        <StepIcon kind={step.kind} running={running} />
      </span>
      <div className="run-step-body">
        <button
          type="button"
          aria-expanded={hasDetails ? expanded : undefined}
          disabled={!hasDetails}
          onClick={() => setExpanded((value) => !value)}
          className={hasDetails ? 'run-step-toggle has-details' : 'run-step-toggle'}
        >
          {hasDetails ? (
            expanded ? <IconChevronDown size={13} stroke={2.2} /> : <IconChevronRight size={13} stroke={2.2} />
          ) : (
            <span className="run-step-caret" />
          )}
          <span className="run-step-title">{localizedToolTitle(step, t)}</span>
          {preview && !(expanded && step.status === 'failed') && <span className="run-step-summary">{
            preview.includes('[REDACTED_SECRET]')
              ? <PastedCredentialText content={preview} />
              : <ReactMarkdown allowedElements={[]} unwrapDisallowed skipHtml>{preview}</ReactMarkdown>
          }</span>}
        </button>
        {expanded && <RunStepDetails step={step} />}
      </div>
    </div>
  );
}

function proseText(step: RunStepSnapshot): string {
  if (typeof step.details === 'string' && step.details.trim()) return step.details.trim();
  return step.summary?.trim() || '';
}

function RunProse({ step }: { step: RunStepSnapshot }) {
  const content = assistantReplyForDisplay(proseText(step));
  if (!content) return null;
  return (
    <article
      data-testid={step.kind === 'commentary' ? 'run-disclosure-commentary' : 'run-disclosure-prose'}
      data-kind={step.kind}
      className="run-commentary"
    >
      <MarkdownRenderer content={content} className="run-commentary-content" />
    </article>
  );
}

function RunCompactionBoundary({ step }: { step: RunStepSnapshot }) {
  const { t } = useTranslation();
  const running = step.status === 'running';
  const label = running
    ? t('agent.chat.disclosure.contextCompacting', 'Automatically compacting context')
    : t('agent.chat.disclosure.contextCompacted', 'Context was automatically compacted');
  return (
    <div
      data-testid="run-disclosure-compaction"
      data-status={step.status}
      className={running ? 'run-compaction is-running' : 'run-compaction'}
    >
      <span className="run-compaction-icon">
        {running
          ? <IconLoader2 size={14} stroke={2.1} className="session-tui-step-spinner" aria-hidden="true" />
          : <IconFileText size={14} stroke={2.1} aria-hidden="true" />}
      </span>
      <span className="run-compaction-label">{label}</span>
    </div>
  );
}

/** The original tool id kept in the step's details (operators' evidence). */
function toolNameOf(step: RunStepSnapshot): string | null {
  const details = step.details;
  if (!details || typeof details !== 'object' || Array.isArray(details)) return null;
  const toolName = (details as { toolName?: unknown; name?: unknown }).toolName
    || (details as { name?: unknown }).name;
  return typeof toolName === 'string' && toolName.trim() ? toolName : null;
}

function hasStepDetails(step: RunStepSnapshot): boolean {
  return Boolean(toolNameOf(step) || ['tool', 'file', 'search', 'command'].includes(step.kind)
    || (typeof step.details === 'string' && step.details.trim()));
}

function localizedToolTitle(step: RunStepSnapshot, t: TFunction): string {
  if (step.titleIsLocalized) return step.title;
  if (step.kind === 'reasoning' && step.title === 'Thinking' && step.status !== 'running' && step.status !== 'blocked') {
    return t('agent.chat.disclosure.thought', 'Thought');
  }
  if (step.kind === 'permission' && step.title === 'permission_resolved') {
    return t('sessionWorkbench.threadItem.type.approval_decision', 'Approval decision');
  }
  return localizedToolName(toolNameOf(step), t, step.title);
}

// Keep generic tool steps in history; their shared mapping supplies the label.
function isGenericToolStep(step: RunStepSnapshot): boolean {
  return (step.title === 'Tool call' || step.title === 'Runtime event')
    && !step.summary
    && !approvalActionFor(toolNameOf(step));
}

/** Compose the locale sentence for the run's folded process aggregate. */
function localizedAggregateSummary(
  summary: RunAggregateSummary,
  t: TFunction,
): string[] {
  const parts: string[] = [];
  if (summary.files > 0) {
    parts.push(t('agent.chat.disclosure.aggregate.files', 'Read {{count}} files', { count: summary.files }));
  }
  if (summary.searches > 0) {
    parts.push(t('agent.chat.disclosure.aggregate.searches', 'Searched the web {{count}} times', { count: summary.searches }));
  }
  if (summary.commands > 0) {
    parts.push(t('agent.chat.disclosure.aggregate.commands', 'Ran {{count}} commands', { count: summary.commands }));
  }
  return parts;
}

function toolStepLabel(step: RunStepSnapshot, t: TFunction): string {
  const title = localizedToolTitle(step, t);
  return step.summary ? `${title} · ${step.summary}` : title;
}

function toolGroupStatus(steps: RunStepSnapshot[]): RunStepSnapshot['status'] {
  if (steps.some((step) => step.status === 'running')) return 'running';
  if (steps.some((step) => step.status === 'failed')) return 'failed';
  if (steps.some((step) => step.status === 'blocked')) return 'blocked';
  if (steps.some((step) => step.status === 'cancelled')) return 'cancelled';
  return 'done';
}

function ToolHistoryItem({ step }: { step: RunStepSnapshot }) {
  const { t } = useTranslation();
  const [expanded, setExpanded] = React.useState(false);
  const hasDetails = hasStepDetails(step);
  const row = (
    <>
      <span className={step.status === 'running' ? 'run-step-icon is-running' : 'run-step-icon'}>
        <StepIcon kind={step.kind} running={step.status === 'running'} />
      </span>
      <span className="run-tool-history-title">
        {localizedToolTitle(step, t)}
        {step.status === 'interrupted' && (
          <span className="run-tool-history-status">{t('agent.chat.disclosure.interruptedBadge', 'Interrupted')}</span>
        )}
      </span>
      {step.summary && <span className="run-tool-history-summary">{step.summary}</span>}
    </>
  );

  return (
    <div className="run-tool-history-item" role="listitem" data-status={step.status}>
      <button
        type="button"
        aria-expanded={hasDetails ? expanded : undefined}
        disabled={!hasDetails}
        onClick={() => setExpanded((value) => !value)}
        className={hasDetails ? 'run-tool-history-toggle has-details' : 'run-tool-history-toggle'}
      >
        {row}
      </button>
      {expanded && <RunStepDetails step={step} />}
    </div>
  );
}

function ToolActivityGroup({ steps }: { steps: RunStepSnapshot[] }) {
  const { t } = useTranslation();
  const genericTitle = localizedToolName(null, t);
  const named = steps.filter((step) => localizedToolTitle(step, t) !== genericTitle);
  const status = toolGroupStatus(steps);
  const currentStep = [...steps].reverse().find((step) => step.status === 'running') || steps[steps.length - 1];
  // A call cut off by the member's stop never completed: when every call in
  // the group is interrupted the collapsed row must say so instead of reading
  // as a completed action.
  const allInterrupted = steps.length > 0 && steps.every((step) => step.status === 'interrupted');
  const namedTools = Array.from(new Set(named.map((step) => localizedToolTitle(step, t)))).join(' · ');
  const label = named.length === 0
    ? t('agent.chat.disclosure.otherAction', 'Other action')
    : status === 'running'
      ? t('agent.chat.disclosure.toolRunning', 'Using {{tool}}', { tool: toolStepLabel(currentStep, t) })
      : allInterrupted
        ? t('agent.chat.disclosure.toolInterrupted', 'Interrupted: {{tools}}', { tools: namedTools })
        : t('agent.chat.disclosure.toolCompleted', 'Used tools: {{tools}}', { tools: namedTools });
  const historyLabel = t('agent.chat.disclosure.toolHistory', 'Tool call history');
  return (
    <details data-testid="run-disclosure-tool-group" data-status={status} className="run-tool-group">
      <summary
        data-testid="run-disclosure-tool-group-toggle"
        className="run-tool-group-toggle"
        aria-label={historyLabel}
      >
        <span className={status === 'running' ? 'run-step-icon is-running' : 'run-step-icon'}>
          <StepIcon kind={currentStep.kind} running={status === 'running'} />
        </span>
        <span className="run-tool-group-label">{label}</span>
        <IconChevronRight className="run-tool-group-chevron" size={13} stroke={2.2} aria-hidden="true" />
      </summary>
      <div className="run-tool-history" role="list" aria-label={historyLabel}>
        {steps.map((step) => <ToolHistoryItem key={step.id} step={step} />)}
      </div>
    </details>
  );
}

function RunTimelineItem({ item, resultPreview }: { item: RunRenderItem; resultPreview: boolean }) {
  if (item.kind === 'tool_group') return <ToolActivityGroup steps={item.steps} />;
  if (item.step.kind === 'commentary' || item.step.kind === 'prose') return <RunProse step={item.step} />;
  if (item.step.kind === 'compaction') return <RunCompactionBoundary step={item.step} />;
  return <RunStepRow step={item.step} resultPreview={resultPreview} />;
}

export default function RunDisclosureBlock({
  timeline,
  presentationKey = null,
}: {
  timeline: RunTimelineSnapshot;
  presentationKey?: string | null;
}) {
  const { t } = useTranslation();
  const running = timeline.status === 'running';
  const resultsKept = timeline.status === 'done' && !timeline.answerMessageId && timeline.steps.some(isKeptResultStep);
  const runDuration = useRunDuration(timeline, presentationKey);
  const duration = resultsKept ? '' : runDuration;
  const opensForAttention = running || timeline.status === 'blocked' || timeline.status === 'failed';
  const [expanded, setExpanded] = React.useState(opensForAttention);
  const previousTimeline = React.useRef({ id: timeline.id, status: timeline.status });

  React.useEffect(() => {
    const previous = previousTimeline.current;
    if (previous.id !== timeline.id || previous.status !== timeline.status) {
      setExpanded(opensForAttention);
      previousTimeline.current = { id: timeline.id, status: timeline.status };
    }
  }, [opensForAttention, timeline.id, timeline.status]);

  const title = resultsKept ? t('agent.chat.disclosure.system.resultsAvailable', 'Task results received')
    : runtimeStatusLabel(timeline.status, t, timeline.phase);
  const interruptedHint = t(
    'agent.chat.disclosure.interruptedHint',
    'This turn has no completion record; send a new message to continue.',
  );
  const visibleSteps = timeline.steps
    .filter((step) => getRunStepPresentation(step) !== 'external')
    .filter((step, _index, steps) => {
      const event = step.systemEvent;
      const taskId = event?.metadata.runtime_task_id;
      if (event?.type !== 'child_session' || step.status !== 'failed' || !taskId) return true;
      // The runtime status already carries this child's exact failure sentence.
      return !steps.some(other => {
        const source = other.systemEvent;
        return other.status === 'failed' && source?.type === 'runtime_action_failed'
          && source.metadata.runtime_task_id === taskId && source.metadata.action_kind === 'a2a_delegation'
          && typeof source.metadata.target_agent_name === 'string' && typeof step.details === 'string'
          && other.details === `${source.metadata.target_agent_name} 的委派任务失败：${step.details}`;
      });
    })
    .map((step) => presentSystemStep(step, t));
  if (visibleSteps.length === 0) {
    if (timeline.status === 'idle') return null;
    return (
      <div data-testid="run-disclosure-block" data-status={timeline.status} className="run-disclosure">
        <div className="run-disclosure-frame">
          <div className="run-disclosure-header is-static" role="status">
            <span className="run-disclosure-title">{title}</span>
            {timeline.durationMs != null && duration && (
              <span className="run-disclosure-duration">{duration}</span>
            )}
            {timeline.status === 'interrupted' && <span className="run-disclosure-summary">{interruptedHint}</span>}
          </div>
        </div>
      </div>
    );
  }
  const stepCount = t('agent.chat.disclosure.stepCount', '{{count}} steps', { count: visibleSteps.length });
  const live = timeline.status === 'running' || timeline.status === 'blocked';
  const renderItems = groupRunSteps(visibleSteps.map((step) => (
    isGenericToolStep(step) ? { ...step, presentation: 'tool_history' as const } : step
  )));

  return (
    <div data-testid="run-disclosure-block" data-status={timeline.status} className="run-disclosure">
      <div className={live ? 'run-disclosure-frame is-live' : 'run-disclosure-frame'}>
        <button
          type="button"
          className="run-disclosure-header"
          aria-expanded={expanded}
          onClick={() => setExpanded((value) => !value)}
        >
          {expanded ? <IconChevronDown size={13} stroke={2.2} /> : <IconChevronRight size={13} stroke={2.2} />}
          <span className={running ? 'session-tui-shimmer run-disclosure-title' : 'run-disclosure-title'}>
            {title}
          </span>
          {duration && <span className="run-disclosure-duration">{duration}</span>}
          {timeline.status === 'interrupted' ? (
            <span className="run-disclosure-summary">{interruptedHint}</span>
          ) : (
            timeline.summary && (
              <span className="run-disclosure-summary is-aggregate">{localizedAggregateSummary(timeline.summary, t).map((part, index) => (
                <span className="run-disclosure-stat" key={index}>{index > 0 && ' · '}{part}</span>
              ))}</span>
            )
          )}
          <span className="run-disclosure-count">{stepCount}</span>
        </button>
        {expanded && (
          <div className="run-disclosure-steps">
            {renderItems.map((item) => <RunTimelineItem key={item.id} item={item} resultPreview={resultsKept} />)}
          </div>
        )}
      </div>
      {!expanded && visibleSteps
        .filter((step) => step.kind === 'commentary' && getRunStepPresentation(step) === 'surface')
        .map((step) => <RunProse key={step.id} step={step} />)}
    </div>
  );
}
