import { runtimeStatusLabel } from './runtimeStatus';
import React from 'react';
import { useTranslation } from 'react-i18next';
import type { TFunction } from 'i18next';
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
import { toolFailureForDisplay } from './toolFailurePresentation';
import { PastedCredentialText } from '../session-workbench/PastedCredentialNotice';
import type {
  RunStepKind,
  RunStepPresentation,
  RunAggregateSummary,
  RunStepSnapshot,
  RunTimelineSnapshot,
} from './chatDisclosureReducer';

// One Session turn owns one disclosure. Live or failed work opens for immediate
// inspection; terminal successful work folds behind its processed summary.

const EXEC_CLIP_LINES = 5;
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
  if (seconds < 60) return `${seconds}s`;
  const minutes = Math.floor(seconds / 60);
  const remainder = seconds % 60;
  return remainder > 0 ? `${minutes}m ${remainder}s` : `${minutes}m`;
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
    persistObservedRunDuration(presentationKey, observedDurationMs);
  }, [observedDurationMs, presentationKey]);
  const duration = formatDuration(Math.max(observedDurationMs, timeline.durationMs ?? 0));
  if (duration) return duration;
  return running && !Number.isNaN(parsed) ? '0s' : '';
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

type ExecDetails = {
  command?: string;
  output?: string;
  exit_code?: number;
  duration_ms?: number;
};

function asExecDetails(details: unknown, kind?: RunStepKind): ExecDetails | null {
  if (!details || typeof details !== 'object') return null;
  const record = details as Record<string, unknown>;
  const hasExecShape =
    typeof record.output === 'string' || typeof record.command === 'string' || typeof record.exit_code === 'number';
  if (!hasExecShape && kind === 'command') {
    const args = record.args && typeof record.args === 'object'
      ? record.args as Record<string, unknown>
      : {};
    const command = typeof args.cmd === 'string'
      ? args.cmd
      : typeof args.command === 'string'
        ? args.command
        : undefined;
    const output = typeof record.result === 'string'
      ? record.result
      : typeof record.rawResult === 'string'
        ? record.rawResult
        : undefined;
    if (!command && !output) return null;
    return { command, output };
  }
  if (!hasExecShape) return null;
  return {
    command: typeof record.command === 'string' ? record.command : undefined,
    output: typeof record.output === 'string' ? record.output : undefined,
    exit_code: typeof record.exit_code === 'number' ? record.exit_code : undefined,
    duration_ms: typeof record.duration_ms === 'number' ? record.duration_ms : undefined,
  };
}

function clipLines(text: string, limit: number): { head: string[]; tail: string[]; clipped: number } {
  const lines = text.split('\n');
  if (lines.length <= limit * 2) return { head: lines, tail: [], clipped: 0 };
  return {
    head: lines.slice(0, limit),
    tail: lines.slice(lines.length - limit),
    clipped: lines.length - limit * 2,
  };
}

function ExecOutput({ exec }: { exec: ExecDetails }) {
  const { t } = useTranslation();
  const output = exec.output ?? '';
  const { head, tail, clipped } = clipLines(output, EXEC_CLIP_LINES);
  const exitCode = exec.exit_code;
  const duration = formatDuration(exec.duration_ms);
  return (
    <div className="session-tui-exec-output">
      {(exec.command || exitCode != null || duration) && (
        <div className="exec-meta">
          {exec.command && <span className="exec-command">$ {exec.command}</span>}
          {exitCode != null && (
            <span className={exitCode === 0 ? 'exec-exit-ok' : 'exec-exit-bad'}>exit {exitCode}</span>
          )}
          {duration && <span className="exec-duration">{duration}</span>}
        </div>
      )}
      {output && (
        <>
          <pre className="exec-pre">
            {head.join('\n')}
            {clipped > 0 && <span className="exec-clip">{`\n… ${clipped} lines …\n`}</span>}
            {tail.length > 0 && tail.join('\n')}
          </pre>
          {clipped > 0 && (
            <details className="exec-complete">
              <summary>{t('agent.chat.disclosure.showCompleteOutput', 'Show complete output')}</summary>
              <pre className="exec-pre exec-complete-pre">{output}</pre>
            </details>
          )}
        </>
      )}
    </div>
  );
}

function RunStepDetails({ step }: { step: RunStepSnapshot }) {
  const { t } = useTranslation();
  const failure = toolFailureForDisplay(step.details, t, step.status);
  if (failure) return <p>{failure}</p>;
  const exec = step.kind === 'command' ? asExecDetails(step.details, step.kind) : null;
  if (exec) return <ExecOutput exec={exec} />;
  if (step.kind === 'tool' || step.kind === 'file' || step.kind === 'search' || typeof step.details !== 'string') {
    const result = typeof step.details === 'string' ? step.details : (step.details as { result?: unknown } | null)?.result;
    if (typeof result !== 'string' || !result.trim()) return null;
    try {
      JSON.parse(result);
      return null;
    } catch {
      return <p><PastedCredentialText content={result.trim().replace(/\s+/g, ' ')} /></p>;
    }
  }
  const detailText = step.details;
  if (!detailText) return null;
  return <pre className="run-detail-pre"><PastedCredentialText content={detailText} /></pre>;
}

function RunStepRow({ step }: { step: RunStepSnapshot }) {
  const { t } = useTranslation();
  const running = step.status === 'running';
  // Commands, arguments and output wait behind this toggle: the default layer
  // shows the localized action and its state only (B2 r2).
  const [expanded, setExpanded] = React.useState(false);
  const hasDetails = step.details != null;
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
          {step.summary && <span className="run-step-summary"><PastedCredentialText content={step.summary} /></span>}
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
  const toolName = (details as { toolName?: unknown }).toolName;
  return typeof toolName === 'string' && toolName.trim() ? toolName : null;
}

function localizedToolTitle(step: RunStepSnapshot, t: TFunction): string {
  if (step.kind === 'permission' && step.title === 'permission_resolved') {
    return t('sessionWorkbench.threadItem.type.approval_decision', 'Approval decision');
  }
  return localizedToolName(toolNameOf(step), t, step.title);
}

/**
 * A row that names nothing is not a row on the default layer (B2 r2): a
 * generic-class title with no summary and no known action adds no
 * information. The step still counts in the "{{count}} steps" total and
 * stays reachable in the opened tool history with a localized label.
 */
function isNamelessStep(step: RunStepSnapshot): boolean {
  return (step.title === 'Tool call' || step.title === 'Runtime event')
    && !step.summary
    && !approvalActionFor(toolNameOf(step));
}

/** Compose the locale sentence for the run's folded process aggregate. */
function localizedAggregateSummary(
  summary: RunAggregateSummary,
  t: TFunction,
): string {
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
  return parts.join(' · ');
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
  const exec = step.kind === 'command' ? asExecDetails(step.details, step.kind) : null;
  const hasDetails = step.details != null;
  const row = (
    <>
      <span className={step.status === 'running' ? 'run-step-icon is-running' : 'run-step-icon'}>
        <StepIcon kind={step.kind} running={step.status === 'running'} />
      </span>
      <span className="run-tool-history-title">{localizedToolTitle(step, t)}</span>
      {step.summary && <span className="run-tool-history-summary">{step.summary}</span>}
    </>
  );

  if (exec) {
    return (
      <div className="run-tool-history-item has-exec" role="listitem" data-status={step.status}>
        <details className="run-tool-history-exec" open={step.status === 'running' || step.status === 'failed'}>
          <summary className="run-tool-history-exec-summary">
            {row}
            <IconChevronRight className="run-tool-history-exec-chevron" size={12} stroke={2.2} aria-hidden="true" />
          </summary>
          <ExecOutput exec={exec} />
        </details>
      </div>
    );
  }

  // Generic tools expose plain-text results without dumping structured payloads.
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
  // The one display decision (isNamelessStep) decides the label: the summary
  // names the work the member can recognize. A group whose every step names
  // nothing keeps no label of its own — its "…" affordance carries nothing
  // on the default layer; the "{{count}} steps" header already counts them.
  const named = steps.filter((step) => !isNamelessStep(step));
  const status = toolGroupStatus(steps);
  const currentStep = [...steps].reverse().find((step) => step.status === 'running') || steps[steps.length - 1];
  const label = named.length === 0
    ? '…'
    : status === 'running'
      ? t('agent.chat.disclosure.toolRunning', 'Using {{tool}}', { tool: toolStepLabel(
        [...named].reverse().find((step) => step.status === 'running') || named[named.length - 1], t,
      ) })
      : t('agent.chat.disclosure.toolCompleted', 'Used tools: {{tools}}', {
        tools: Array.from(new Set(named.map((step) => localizedToolTitle(step, t)))).join(' · '),
      });
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

function RunTimelineItem({ item }: { item: RunRenderItem }) {
  if (item.kind === 'tool_group') return <ToolActivityGroup steps={item.steps} />;
  if (item.step.kind === 'commentary' || item.step.kind === 'prose') return <RunProse step={item.step} />;
  if (item.step.kind === 'compaction') return <RunCompactionBoundary step={item.step} />;
  return <RunStepRow step={item.step} />;
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
  const duration = useRunDuration(timeline, presentationKey);
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

  const title = runtimeStatusLabel(timeline.status, t);
  const interruptedHint = t(
    'agent.chat.disclosure.interruptedHint',
    'This turn has no completion record; send a new message to continue.',
  );
  const visibleSteps = timeline.steps.filter((step) => getRunStepPresentation(step) !== 'external');
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
  // Nameless steps fold into the count and into the tool history instead of
  // owning rows: nothing that names nothing renders on the default layer,
  // and every step stays reachable once the history opens (B2 r3).
  const renderItems = groupRunSteps(visibleSteps.map((step) => (
    isNamelessStep(step) ? { ...step, presentation: 'tool_history' as const } : step
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
              <span className="run-disclosure-summary">{localizedAggregateSummary(timeline.summary, t)}</span>
            )
          )}
          <span className="run-disclosure-count">{stepCount}</span>
        </button>
        {expanded && (
          <div className="run-disclosure-steps">
            {renderItems.map((item) => <RunTimelineItem key={item.id} item={item} />)}
          </div>
        )}
      </div>
      {!expanded && visibleSteps
        .filter((step) => step.kind === 'commentary' && getRunStepPresentation(step) === 'surface')
        .map((step) => <RunProse key={step.id} step={step} />)}
    </div>
  );
}
