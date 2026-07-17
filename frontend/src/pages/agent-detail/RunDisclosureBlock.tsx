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
import type {
  RunStepKind,
  RunStepPresentation,
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
  if (step.kind === 'reasoning' || step.kind === 'commentary' || step.kind === 'compaction') return 'process';
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

function useLiveElapsed(startedAt: string | undefined, running: boolean): string {
  const parsed = startedAt ? Date.parse(startedAt) : NaN;
  const compute = React.useCallback(() => {
    if (!running || Number.isNaN(parsed)) return '';
    const seconds = Math.max(0, Math.floor((Date.now() - parsed) / 1000));
    return formatDuration(seconds * 1000) || `${seconds}s`;
  }, [parsed, running]);
  const [elapsed, setElapsed] = React.useState<string>(compute);
  React.useEffect(() => {
    if (!running || Number.isNaN(parsed)) return undefined;
    setElapsed(compute());
    const timer = window.setInterval(() => setElapsed(compute()), 1000);
    return () => window.clearInterval(timer);
  }, [compute, parsed, running]);
  return elapsed;
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
  const exec = asExecDetails(step.details, step.kind);
  if (exec) return <ExecOutput exec={exec} />;
  const detailText = typeof step.details === 'string' ? step.details : JSON.stringify(step.details ?? {}, null, 2);
  if (!detailText || detailText === '{}') return null;
  return <pre className="run-detail-pre">{detailText}</pre>;
}

function RunStepRow({ step }: { step: RunStepSnapshot }) {
  const running = step.status === 'running';
  const exec = asExecDetails(step.details, step.kind);
  const [expanded, setExpanded] = React.useState(
    (Boolean(exec) && (running || step.status === 'failed'))
      || step.blocking === true
      || (step.visibility === 'visible' && step.status !== 'done'),
  );
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
          <span className="run-step-title">{step.title}</span>
          {step.summary && <span className="run-step-summary">{step.summary}</span>}
        </button>
        {expanded && <RunStepDetails step={step} />}
      </div>
    </div>
  );
}

function commentaryText(step: RunStepSnapshot): string {
  if (typeof step.details === 'string' && step.details.trim()) return step.details.trim();
  return step.summary?.trim() || '';
}

function RunCommentary({ step }: { step: RunStepSnapshot }) {
  const content = commentaryText(step);
  if (!content) return null;
  return (
    <article data-testid="run-disclosure-commentary" className="run-commentary">
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

const TOOL_TITLE_KEYS: Record<string, [string, string]> = {
  'Loading tools': ['agent.chat.disclosure.toolTitle.loading', 'Loading tools'],
  'Read file': ['agent.chat.disclosure.toolTitle.readFile', 'Read file'],
  'Write file': ['agent.chat.disclosure.toolTitle.writeFile', 'Write file'],
  'Edit file': ['agent.chat.disclosure.toolTitle.editFile', 'Edit file'],
  'Delete file': ['agent.chat.disclosure.toolTitle.deleteFile', 'Delete file'],
  'List files': ['agent.chat.disclosure.toolTitle.listFiles', 'List files'],
  'Search web': ['agent.chat.disclosure.toolTitle.searchWeb', 'Search web'],
  'Fetch web page': ['agent.chat.disclosure.toolTitle.fetchWebPage', 'Fetch web page'],
  'Run command': ['agent.chat.disclosure.toolTitle.runCommand', 'Run command'],
  'Edit document': ['agent.chat.disclosure.toolTitle.editDocument', 'Edit document'],
  'Tool call': ['agent.chat.disclosure.toolTitle.toolCall', 'Tool call'],
};

function localizedToolTitle(step: RunStepSnapshot, t: TFunction): string {
  const translated = TOOL_TITLE_KEYS[step.title];
  return translated ? t(translated[0], translated[1]) : step.title;
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
  const exec = asExecDetails(step.details);
  const row = (
    <>
      <span className={step.status === 'running' ? 'run-step-icon is-running' : 'run-step-icon'}>
        <StepIcon kind={step.kind} running={step.status === 'running'} />
      </span>
      <span className="run-tool-history-title">{localizedToolTitle(step, t)}</span>
      {step.summary && <span className="run-tool-history-summary">{step.summary}</span>}
    </>
  );

  if (!exec) {
    return (
      <div className="run-tool-history-item" role="listitem" data-status={step.status}>
        {row}
      </div>
    );
  }

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

function ToolActivityGroup({ steps }: { steps: RunStepSnapshot[] }) {
  const { t } = useTranslation();
  const status = toolGroupStatus(steps);
  const currentStep = [...steps].reverse().find((step) => step.status === 'running') || steps[steps.length - 1];
  const completedActions = Array.from(new Set(steps.map((step) => localizedToolTitle(step, t)))).join(' · ');
  const label = status === 'running'
    ? t('agent.chat.disclosure.toolRunning', 'Using {{tool}}', { tool: toolStepLabel(currentStep, t) })
    : t('agent.chat.disclosure.toolCompleted', 'Used tools: {{tools}}', { tools: completedActions });
  return (
    <details data-testid="run-disclosure-tool-group" data-status={status} className="run-tool-group">
      <summary data-testid="run-disclosure-tool-group-toggle" className="run-tool-group-toggle">
        <span className={status === 'running' ? 'run-step-icon is-running' : 'run-step-icon'}>
          <StepIcon kind={currentStep.kind} running={status === 'running'} />
        </span>
        <span className="run-tool-group-label">{label}</span>
        <IconChevronRight className="run-tool-group-chevron" size={13} stroke={2.2} aria-hidden="true" />
      </summary>
      <div
        className="run-tool-history"
        role="list"
        aria-label={t('agent.chat.disclosure.toolHistory', 'Tool call history')}
      >
        {steps.map((step) => <ToolHistoryItem key={step.id} step={step} />)}
      </div>
    </details>
  );
}

function RunTimelineItem({ item }: { item: RunRenderItem }) {
  if (item.kind === 'tool_group') return <ToolActivityGroup steps={item.steps} />;
  if (item.step.kind === 'commentary') return <RunCommentary step={item.step} />;
  if (item.step.kind === 'compaction') return <RunCompactionBoundary step={item.step} />;
  return <RunStepRow step={item.step} />;
}

export default function RunDisclosureBlock({ timeline }: { timeline: RunTimelineSnapshot }) {
  const { t } = useTranslation();
  const running = timeline.status === 'running';
  const liveElapsed = useLiveElapsed(timeline.startedAt, running);
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

  const visibleSteps = timeline.steps.filter((step) => getRunStepPresentation(step) !== 'external');
  if (visibleSteps.length === 0) return null;
  const duration = running ? liveElapsed : formatDuration(timeline.durationMs);
  const title = running
    ? t('agent.chat.disclosure.working', 'Working')
    : timeline.status === 'blocked'
      ? t('agent.chat.disclosure.waiting', 'Waiting for input')
      : timeline.status === 'failed'
        ? t('agent.chat.disclosure.needsAttention', 'Needs attention')
        : timeline.status === 'cancelled'
          ? t('agent.chat.disclosure.stopped', 'Stopped')
          : t('agent.chat.disclosure.processed', 'Processed');
  const collapsibleStepCount = visibleSteps.filter((step) => getRunStepPresentation(step) !== 'surface').length;
  const stepCount = t('agent.chat.disclosure.stepCount', '{{count}} steps', { count: collapsibleStepCount });
  const live = timeline.status === 'running' || timeline.status === 'blocked';
  const renderItems = groupRunSteps(visibleSteps);
  const hasCollapsibleSteps = collapsibleStepCount > 0;
  const hasSurfacedSteps = visibleSteps.some((step) => getRunStepPresentation(step) === 'surface');

  return (
    <div data-testid="run-disclosure-block" data-status={timeline.status} className="run-disclosure">
      <div className={live ? 'run-disclosure-frame is-live' : 'run-disclosure-frame'}>
        {hasCollapsibleSteps && (
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
            {timeline.summary && <span className="run-disclosure-summary">{timeline.summary}</span>}
            <span className="run-disclosure-count">{stepCount}</span>
          </button>
        )}
        {(expanded || hasSurfacedSteps) && (
          <div className="run-disclosure-steps">
            {renderItems.map((item) => {
              const surfaced = item.kind === 'step' && getRunStepPresentation(item.step) === 'surface';
              if (!expanded && !surfaced) return null;
              return <RunTimelineItem key={item.id} item={item} />;
            })}
          </div>
        )}
      </div>
    </div>
  );
}
