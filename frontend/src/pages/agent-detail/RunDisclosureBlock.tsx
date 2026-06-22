import React from 'react';
import { useTranslation } from 'react-i18next';
import {
  IconChevronDown,
  IconChevronRight,
  IconCircleCheck,
  IconCircleDot,
  IconFileText,
  IconHelp,
  IconSearch,
  IconTerminal2,
  IconTool,
} from '@tabler/icons-react';

import type { RunStepKind, RunStepSnapshot, RunTimelineSnapshot } from './chatDisclosureReducer';

function formatDuration(durationMs?: number): string {
  if (!durationMs || durationMs < 1000) return '';
  const seconds = Math.round(durationMs / 1000);
  if (seconds < 60) return `${seconds}s`;
  const minutes = Math.floor(seconds / 60);
  const remainder = seconds % 60;
  return remainder > 0 ? `${minutes}m ${remainder}s` : `${minutes}m`;
}

function StepIcon({ kind, running }: { kind: RunStepKind; running: boolean }) {
  const size = 14;
  if (running) return <IconCircleDot size={size} stroke={2.2} />;
  if (kind === 'search') return <IconSearch size={size} stroke={2.1} />;
  if (kind === 'file') return <IconFileText size={size} stroke={2.1} />;
  if (kind === 'command') return <IconTerminal2 size={size} stroke={2.1} />;
  if (kind === 'question' || kind === 'plan' || kind === 'permission') return <IconHelp size={size} stroke={2.1} />;
  if (kind === 'reasoning') return <IconCircleCheck size={size} stroke={2.1} />;
  return <IconTool size={size} stroke={2.1} />;
}

function RunStepDetails({ step }: { step: RunStepSnapshot }) {
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
  const [expanded, setExpanded] = React.useState(false);
  const running = step.status === 'running';
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

export default function RunDisclosureBlock({ timeline }: { timeline: RunTimelineSnapshot }) {
  const { t } = useTranslation();
  const [expanded, setExpanded] = React.useState(true);

  if (timeline.steps.length === 0) return null;
  const duration = formatDuration(timeline.durationMs);
  const title =
    timeline.status === 'running'
      ? t('agent.chat.disclosure.processing', 'Processing')
      : timeline.status === 'blocked'
        ? t('agent.chat.disclosure.waiting', 'Waiting for input')
        : t('agent.chat.disclosure.processed', 'Processed');
  const stepCount = t('agent.chat.disclosure.stepCount', '{{count}} steps', { count: timeline.steps.length });

  return (
    <div data-testid="run-disclosure-block" style={{ paddingLeft: '36px', marginBottom: '8px', maxWidth: '75%' }}>
      <div
        style={{
          border: '1px solid var(--border-subtle)',
          borderRadius: '8px',
          background: 'var(--bg-elevated)',
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
            padding: '8px 10px',
            display: 'flex',
            alignItems: 'center',
            gap: '8px',
            cursor: 'pointer',
            textAlign: 'left',
          }}
        >
          {expanded ? <IconChevronDown size={14} stroke={2.2} /> : <IconChevronRight size={14} stroke={2.2} />}
          <span style={{ fontSize: '12px', fontWeight: 700, color: 'var(--text-primary)' }}>{title}</span>
          {duration && <span style={{ fontSize: '12px', color: 'var(--text-tertiary)' }}>{duration}</span>}
          <span style={{ marginLeft: 'auto', fontSize: '11px', color: 'var(--text-tertiary)' }}>{stepCount}</span>
        </button>
        {expanded && (
          <div style={{ display: 'grid', gap: '7px', padding: '0 10px 10px' }}>
            {timeline.steps.map((step) => (
              <RunStepRow key={step.id} step={step} />
            ))}
          </div>
        )}
      </div>
    </div>
  );
}
