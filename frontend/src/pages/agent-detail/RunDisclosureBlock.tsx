import React from 'react';
import { useTranslation } from 'react-i18next';
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

import type { RunStepKind, RunStepSnapshot, RunTimelineSnapshot } from './chatDisclosureReducer';

// Codex-parity disclosure semantics:
// - running: shimmering "Working" header + live elapsed seconds, expanded.
// - done: collapses to one line by default — finished process recedes, the
//   final answer is the star. blocked/failed stay expanded (they need eyes).
// - command steps render structured exec output (preview + recoverable complete
//   output + exit code), never a raw JSON blob or an irreversible truncation.

const EXEC_CLIP_LINES = 5;

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

function asExecDetails(details: unknown): ExecDetails | null {
  if (!details || typeof details !== 'object') return null;
  const record = details as Record<string, unknown>;
  const hasExecShape =
    typeof record.output === 'string' || typeof record.command === 'string' || typeof record.exit_code === 'number';
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
  const exec = asExecDetails(step.details);
  if (exec) return <ExecOutput exec={exec} />;
  const detailText = typeof step.details === 'string' ? step.details : JSON.stringify(step.details ?? {}, null, 2);
  if (!detailText || detailText === '{}') return null;
  return <pre className="run-detail-pre">{detailText}</pre>;
}

function RunStepRow({ step }: { step: RunStepSnapshot }) {
  const running = step.status === 'running';
  const exec = asExecDetails(step.details);
  // exec 步骤直接展示裁剪后的输出（Codex 语义：命令输出即摘要）；其它详情按需展开。
  const [expanded, setExpanded] = React.useState(Boolean(exec));
  const hasDetails = step.details != null;
  return (
    <div data-testid="run-disclosure-step" className="run-step">
      <span className={running ? 'run-step-icon is-running' : 'run-step-icon'}>
        <StepIcon kind={step.kind} running={running} />
      </span>
      <div className="run-step-body">
        <button
          type="button"
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

function shouldExpandTimeline(timeline: RunTimelineSnapshot): boolean {
  // Codex parity: only live/problem states stay open. A finished run always
  // recedes to one boundary line; the answer and delivery cards stay outside
  // the folded process.
  return timeline.status === 'running' || timeline.status === 'blocked' || timeline.status === 'failed';
}

function CompactStepSummary({ steps }: { steps: RunStepSnapshot[] }) {
  const visibleSteps = steps.slice(0, 5);
  const remaining = steps.length - visibleSteps.length;
  return (
    <div data-testid="run-disclosure-compact-summary" className="run-compact-steps">
      {visibleSteps.map((step) => (
        <span
          key={step.id}
          className="run-compact-step"
          title={step.summary ? `${step.title} · ${step.summary}` : step.title}
        >
          <StepIcon kind={step.kind} running={step.status === 'running'} />
          <span className="run-compact-step-title">{step.title}</span>
          {step.summary && <span className="run-compact-step-summary">{step.summary}</span>}
        </span>
      ))}
      {remaining > 0 && <span className="run-compact-more">+{remaining}</span>}
    </div>
  );
}

export default function RunDisclosureBlock({ timeline }: { timeline: RunTimelineSnapshot }) {
  const { t } = useTranslation();
  const defaultExpanded = shouldExpandTimeline(timeline);
  const [expanded, setExpanded] = React.useState(defaultExpanded);
  const running = timeline.status === 'running';
  const liveElapsed = useLiveElapsed(timeline.startedAt, running);

  React.useEffect(() => {
    setExpanded(defaultExpanded);
  }, [defaultExpanded, timeline.id]);

  if (timeline.steps.length === 0) return null;
  const duration = running ? liveElapsed : formatDuration(timeline.durationMs);
  const title = running
    ? t('agent.chat.disclosure.working', 'Working')
    : timeline.status === 'blocked'
      ? t('agent.chat.disclosure.waiting', 'Waiting for input')
      : t('agent.chat.disclosure.processed', 'Processed');
  const stepCount = t('agent.chat.disclosure.stepCount', '{{count}} steps', { count: timeline.steps.length });
  const live = timeline.status === 'running' || timeline.status === 'blocked';
  const processFullyFolded = timeline.status === 'done' && !expanded;

  return (
    <div data-testid="run-disclosure-block" data-status={timeline.status} className="run-disclosure">
      <div className={live ? 'run-disclosure-frame is-live' : 'run-disclosure-frame'}>
        <button
          type="button"
          aria-expanded={expanded}
          onClick={() => setExpanded((value) => !value)}
          className="run-disclosure-header"
        >
          {expanded ? <IconChevronDown size={14} stroke={2.2} /> : <IconChevronRight size={14} stroke={2.2} />}
          <span className={running ? 'session-tui-shimmer run-disclosure-title' : 'run-disclosure-title'}>
            {title}
          </span>
          {duration && <span className="run-disclosure-duration">{duration}</span>}
          {!processFullyFolded && timeline.summary && <span className="run-disclosure-summary">{timeline.summary}</span>}
          {!processFullyFolded && <span className="run-disclosure-count">{stepCount}</span>}
        </button>
        {expanded && (
          <div className="run-disclosure-steps">
            {timeline.steps.map((step) => (
              <RunStepRow key={step.id} step={step} />
            ))}
          </div>
        )}
        {!expanded && !processFullyFolded && <CompactStepSummary steps={timeline.steps} />}
      </div>
    </div>
  );
}
