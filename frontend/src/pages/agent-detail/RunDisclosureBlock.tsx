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
// - command steps render structured exec output (head/tail clip + exit code),
//   never a raw JSON blob.

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
  const output = exec.output ?? '';
  const { head, tail, clipped } = clipLines(output, EXEC_CLIP_LINES);
  const exitCode = exec.exit_code;
  const duration = formatDuration(exec.duration_ms);
  return (
    <div className="session-tui-exec-output" style={{ marginTop: '6px' }}>
      {(exec.command || exitCode != null || duration) && (
        <div
          style={{
            display: 'flex',
            alignItems: 'center',
            gap: '8px',
            fontSize: '11px',
            fontFamily: 'var(--font-mono)',
            color: 'var(--text-secondary)',
            marginBottom: output ? '4px' : 0,
            minWidth: 0,
          }}
        >
          {exec.command && (
            <span style={{ overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap', minWidth: 0 }}>
              $ {exec.command}
            </span>
          )}
          {exitCode != null && (
            <span
              style={{
                flexShrink: 0,
                color: exitCode === 0 ? 'var(--success, #16a34a)' : 'var(--danger, #ef4444)',
                fontWeight: 600,
              }}
            >
              exit {exitCode}
            </span>
          )}
          {duration && <span style={{ flexShrink: 0, color: 'var(--text-tertiary)' }}>{duration}</span>}
        </div>
      )}
      {output && (
        <pre
          style={{
            margin: 0,
            padding: '8px 10px',
            borderRadius: '6px',
            background: 'var(--bg-secondary)',
            color: 'var(--text-secondary)',
            whiteSpace: 'pre-wrap',
            wordBreak: 'break-word',
            fontSize: '11px',
            fontFamily: 'var(--font-mono)',
          }}
        >
          {head.join('\n')}
          {clipped > 0 && (
            <span style={{ color: 'var(--text-tertiary)' }}>{`\n… ${clipped} lines …\n`}</span>
          )}
          {tail.length > 0 && tail.join('\n')}
        </pre>
      )}
    </div>
  );
}

function RunStepDetails({ step }: { step: RunStepSnapshot }) {
  const exec = asExecDetails(step.details);
  if (exec) return <ExecOutput exec={exec} />;
  const detailText = typeof step.details === 'string' ? step.details : JSON.stringify(step.details ?? {}, null, 2);
  if (!detailText || detailText === '{}') return null;
  return (
    <pre
      style={{
        margin: '6px 0 0',
        padding: '8px 10px',
        borderRadius: '6px',
        background: 'var(--bg-secondary)',
        color: 'var(--text-secondary)',
        whiteSpace: 'pre-wrap',
        wordBreak: 'break-word',
        fontSize: '11px',
        fontFamily: 'var(--font-mono)',
        maxHeight: '240px',
        overflow: 'auto',
      }}
    >
      {detailText}
    </pre>
  );
}

function RunStepRow({ step }: { step: RunStepSnapshot }) {
  const running = step.status === 'running';
  const exec = asExecDetails(step.details);
  // exec 步骤直接展示裁剪后的输出（Codex 语义：命令输出即摘要）；其它详情按需展开。
  const [expanded, setExpanded] = React.useState(Boolean(exec));
  const hasDetails = step.details != null;
  return (
    <div
      data-testid="run-disclosure-step"
      style={{
        display: 'grid',
        gridTemplateColumns: '18px minmax(0, 1fr)',
        gap: '8px',
        alignItems: 'start',
      }}
    >
      <span
        style={{
          color: running ? 'var(--accent-primary)' : 'var(--text-tertiary)',
          display: 'inline-flex',
          alignItems: 'center',
          justifyContent: 'center',
          paddingTop: '2px',
        }}
      >
        <StepIcon kind={step.kind} running={running} />
      </span>
      <div style={{ minWidth: 0 }}>
        <button
          type="button"
          disabled={!hasDetails}
          onClick={() => setExpanded((value) => !value)}
          style={{
            border: 'none',
            background: 'transparent',
            padding: 0,
            width: '100%',
            display: 'flex',
            alignItems: 'center',
            gap: '6px',
            color: 'inherit',
            cursor: hasDetails ? 'pointer' : 'default',
            textAlign: 'left',
          }}
        >
          {hasDetails ? (
            expanded ? <IconChevronDown size={13} stroke={2.2} /> : <IconChevronRight size={13} stroke={2.2} />
          ) : (
            <span style={{ width: '13px' }} />
          )}
          <span style={{ fontSize: '12px', fontWeight: 650, color: 'var(--text-secondary)', whiteSpace: 'nowrap' }}>
            {step.title}
          </span>
          {step.summary && (
            <span
              style={{
                fontSize: '11px',
                color: 'var(--text-tertiary)',
                minWidth: 0,
                overflow: 'hidden',
                textOverflow: 'ellipsis',
                whiteSpace: 'nowrap',
              }}
            >
              {step.summary}
            </span>
          )}
        </button>
        {expanded && <RunStepDetails step={step} />}
      </div>
    </div>
  );
}

function shouldExpandTimeline(timeline: RunTimelineSnapshot): boolean {
  // Codex parity: only live/problem states stay open. A finished run always
  // recedes to one line — the compact chips still show what happened.
  return timeline.status === 'running' || timeline.status === 'blocked' || timeline.status === 'failed';
}

function CompactStepSummary({ steps }: { steps: RunStepSnapshot[] }) {
  const visibleSteps = steps.slice(0, 5);
  const remaining = steps.length - visibleSteps.length;
  return (
    <div
      data-testid="run-disclosure-compact-summary"
      style={{
        display: 'flex',
        flexWrap: 'wrap',
        gap: '5px',
        padding: '0 10px 10px 32px',
      }}
    >
      {visibleSteps.map((step) => (
        <span
          key={step.id}
          style={{
            display: 'inline-flex',
            alignItems: 'center',
            gap: '5px',
            minWidth: 0,
            maxWidth: '100%',
            border: '1px solid var(--border-subtle)',
            borderRadius: '6px',
            background: 'var(--bg-secondary)',
            color: 'var(--text-secondary)',
            padding: '3px 6px',
            fontSize: '11px',
          }}
          title={step.summary ? `${step.title} · ${step.summary}` : step.title}
        >
          <StepIcon kind={step.kind} running={step.status === 'running'} />
          <strong style={{ color: 'var(--text-primary)', fontWeight: 600, whiteSpace: 'nowrap' }}>{step.title}</strong>
          {step.summary && (
            <span style={{ minWidth: 0, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>
              {step.summary}
            </span>
          )}
        </span>
      ))}
      {remaining > 0 && (
        <span style={{ color: 'var(--text-tertiary)', fontSize: '11px', alignSelf: 'center' }}>
          +{remaining}
        </span>
      )}
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

  return (
    <div data-testid="run-disclosure-block" data-status={timeline.status} style={{ marginBottom: '8px', maxWidth: '100%' }}>
      <div
        style={{
          borderLeft: `2px solid ${timeline.status === 'running' || timeline.status === 'blocked' ? 'var(--accent-primary)' : 'var(--border-subtle)'}`,
          background: 'transparent',
          overflow: 'hidden',
        }}
      >
        <button
          type="button"
          aria-expanded={expanded}
          onClick={() => setExpanded((value) => !value)}
          style={{
            width: '100%',
            border: 'none',
            background: 'transparent',
            color: 'var(--text-secondary)',
            padding: '7px 10px',
            display: 'flex',
            alignItems: 'center',
            gap: '8px',
            cursor: 'pointer',
            textAlign: 'left',
          }}
        >
          {expanded ? <IconChevronDown size={14} stroke={2.2} /> : <IconChevronRight size={14} stroke={2.2} />}
          <span
            className={running ? 'session-tui-shimmer' : undefined}
            style={{ fontSize: '12px', fontWeight: 700, color: running ? undefined : 'var(--text-primary)' }}
          >
            {title}
          </span>
          {duration && (
            <span style={{ fontSize: '12px', color: 'var(--text-tertiary)', fontVariantNumeric: 'tabular-nums' }}>
              {duration}
            </span>
          )}
          {timeline.summary && (
            <span
              style={{
                fontSize: '11px',
                color: 'var(--text-tertiary)',
                minWidth: 0,
                overflow: 'hidden',
                textOverflow: 'ellipsis',
                whiteSpace: 'nowrap',
              }}
            >
              {timeline.summary}
            </span>
          )}
          <span style={{ marginLeft: 'auto', fontSize: '11px', color: 'var(--text-tertiary)' }}>{stepCount}</span>
        </button>
        {expanded && (
          <div style={{ display: 'grid', gap: '7px', padding: '0 10px 10px' }}>
            {timeline.steps.map((step) => (
              <RunStepRow key={step.id} step={step} />
            ))}
          </div>
        )}
        {!expanded && <CompactStepSummary steps={timeline.steps} />}
      </div>
    </div>
  );
}
