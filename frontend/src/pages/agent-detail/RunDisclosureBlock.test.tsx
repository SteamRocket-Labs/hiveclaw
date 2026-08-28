import React from 'react';
import { renderToStaticMarkup } from 'react-dom/server';
import { describe, expect, it, vi } from 'vitest';

import RunDisclosureBlock from './RunDisclosureBlock';
import type { RunStepSnapshot, RunTimelineSnapshot } from './chatDisclosureReducer';

// A turn owns one disclosure: live work starts open, while completed work
// collapses behind its processed summary and can be reopened by the user.

vi.mock('react-i18next', () => ({
  useTranslation: () => ({
    t: (_key: string, fallback: string, values?: Record<string, unknown>) => {
      if (typeof fallback !== 'string') return _key;
      return Object.entries(values || {}).reduce(
        (text, [name, value]) => text.replace(`{{${name}}}`, String(value)),
        fallback,
      );
    },
  }),
}));

function step(overrides: Partial<RunStepSnapshot>): RunStepSnapshot {
  return {
    id: 'step-1',
    kind: 'tool',
    title: 'Tool call',
    status: 'done',
    visibility: 'collapsed',
    ...overrides,
  };
}

function liveTimeline(): RunTimelineSnapshot {
  return {
    id: 'run-1',
    status: 'running',
    startedAt: '2026-07-17T08:00:00Z',
    steps: [
      step({
        id: 'commentary-1',
        kind: 'commentary' as RunStepSnapshot['kind'],
        title: 'Progress update',
        details: 'I found the **projection bug** and am checking the adjacent path.',
        visibility: 'visible',
      }),
      step({
        id: 'tool-search-1',
        kind: 'tool',
        title: 'Loading tools',
        summary: 'Checking available tools',
        details: {
          args: { query: 'select:read_file' },
        },
      }),
      step({
        id: 'tool-read-1',
        kind: 'file',
        title: 'Read file',
        summary: 'RunDisclosureBlock.tsx',
        details: {
          args: { path: 'frontend/src/pages/agent-detail/RunDisclosureBlock.tsx' },
          result: 'RAW TOOL RESULT THAT MUST STAY COLLAPSED',
        },
      }),
      step({
        id: 'tool-read-2',
        kind: 'file',
        title: 'Read file',
        summary: 'chatDisclosureReducer.ts',
        status: 'running',
        details: {
          args: { path: 'frontend/src/pages/agent-detail/chatDisclosureReducer.ts' },
        },
      }),
      step({
        id: 'compaction-1',
        kind: 'compaction',
        title: 'Context compaction',
        details: 'INTERNAL COMPACTION DETAILS',
      }),
      step({
        id: 'commentary-2',
        kind: 'commentary' as RunStepSnapshot['kind'],
        title: 'Progress update',
        details: 'The regression is now isolated.',
        visibility: 'visible',
      }),
    ],
  };
}

describe('RunDisclosureBlock', () => {
  const baseTimeline: RunTimelineSnapshot = {
    id: 'timeline-1',
    status: 'done',
    steps: [
      {
        id: 'step-1',
        kind: 'file',
        title: 'read_file',
        status: 'done',
        summary: 'path: frontend/src/pages/agent-detail/AgentChatSection.tsx',
        details: { result: 'RAW FILE CONTENT' },
        visibility: 'collapsed',
      },
    ],
  };

  it('collapses a completed run behind one turn-level disclosure', () => {
    const markup = renderToStaticMarkup(<RunDisclosureBlock timeline={baseTimeline} />);

    expect(markup).toContain('Processed');
    expect(markup).toContain('aria-expanded="false"');
    expect(markup).not.toContain('read_file');
    expect(markup).not.toContain('AgentChatSection.tsx');
    expect(markup).not.toContain('RAW FILE CONTENT');
  });

  it('renders an interrupted historical turn with a frozen duration and an explicit next step, never as live processing', () => {
    // UI-004 regression: a turn whose transcript never reached a terminal mark
    // must show the typed interrupted state with the frozen duration — no live
    // shimmer, no growing stopwatch, and a concrete recovery affordance.
    const timeline: RunTimelineSnapshot = {
      id: 'run-historical',
      status: 'interrupted',
      startedAt: '2026-07-17T08:00:00Z',
      durationMs: 1_200_000,
      steps: [
        step({ id: 'reasoning-1', kind: 'reasoning' as RunStepSnapshot['kind'], title: 'Thinking', status: 'interrupted', visibility: 'collapsed' }),
        step({ id: 'tool-1', kind: 'file' as RunStepSnapshot['kind'], title: 'Read file', status: 'interrupted', summary: 'notes.md', visibility: 'collapsed' }),
      ],
    };

    const markup = renderToStaticMarkup(<RunDisclosureBlock timeline={timeline} />);

    expect(markup).toContain('Interrupted');
    expect(markup).toContain('send a new message to continue');
    // 1_200_000ms = 20m — the frozen authoritative duration renders once.
    expect(markup).toContain('20m');
    expect(markup).not.toContain('shimmer');
    expect(markup).not.toContain('Working');
  });

  it('keeps a copied cancellation visible when the run has no displayable process steps', () => {
    const markup = renderToStaticMarkup(
      <RunDisclosureBlock
        timeline={{
          id: 'copied-cancelled-run',
          status: 'interrupted',
          startedAt: '2026-08-29T04:37:00Z',
          steps: [],
        }}
      />,
    );

    expect(markup).toContain('data-status="interrupted"');
    expect(markup).toContain('Interrupted');
    expect(markup).toContain('send a new message to continue');
    expect(markup).not.toContain('Working');
    expect(markup).not.toContain('0 steps');
  });

  it('folds model-authored public commentary with completed process history', () => {
    const markup = renderToStaticMarkup(
      <RunDisclosureBlock
        timeline={{
          ...baseTimeline,
          steps: [
            step({
              id: 'commentary-1',
              kind: 'commentary',
              title: 'Progress update',
              details: 'The durable Session event is committed; I am validating the consumer now.',
              visibility: 'visible',
              presentation: 'process',
            }),
            step({
              id: 'file-1',
              kind: 'file',
              title: 'Read file',
              summary: 'sessionEventConsumer.ts',
              details: { result: 'RAW FILE CONTENT' },
              presentation: 'tool_history',
            }),
          ],
        }}
      />,
    );

    expect(markup).toContain('Processed');
    expect(markup).toContain('aria-expanded="false"');
    expect(markup).not.toContain('data-testid="run-disclosure-commentary"');
    expect(markup).not.toContain('The durable Session event is committed; I am validating the consumer now.');
    expect(markup).not.toContain('Read file');
    expect(markup).not.toContain('sessionEventConsumer.ts');
    expect(markup).not.toContain('RAW FILE CONTENT');
  });

  it('renders canonical assistant prose verbatim instead of labeling it as Thinking', () => {
    const markup = renderToStaticMarkup(<RunDisclosureBlock timeline={{
      id: 'run-prose',
      status: 'running',
      steps: [step({
        id: 'assistant-text-1',
        kind: 'prose',
        title: 'Assistant update',
        status: 'done',
        details: 'I found the projection gap and am validating live delivery.',
        visibility: 'visible',
        presentation: 'process',
      })],
    }} />);

    expect(markup).toContain('data-testid="run-disclosure-prose"');
    expect(markup).toContain('I found the projection gap and am validating live delivery.');
    expect(markup).not.toContain('Thinking');
    expect(markup).not.toContain('Progress update');
  });

  it('folds completed Thinking and A2A lifecycle progress behind the process summary', () => {
    const markup = renderToStaticMarkup(
      <RunDisclosureBlock
        timeline={{
          ...baseTimeline,
          steps: [
            {
              id: 'reasoning-1',
              kind: 'reasoning',
              title: 'Thinking',
              status: 'done',
              summary: 'Verified the delegated artifact and prepared the handoff.',
              visibility: 'collapsed',
              presentation: 'process',
            },
            {
              id: 'a2a-1',
              kind: 'a2a',
              title: 'Action Started',
              status: 'done',
              summary: 'Delegated to Web3 researcher.',
              visibility: 'collapsed',
              presentation: 'surface',
            },
          ],
        }}
      />,
    );

    expect(markup).not.toContain('Thinking');
    expect(markup).not.toContain('Verified the delegated artifact');
    expect(markup).not.toContain('Action Started');
    expect(markup).not.toContain('Delegated to Web3 researcher');
  });

  it('never duplicates an externally rendered Ask User Question card inside the process disclosure', () => {
    const markup = renderToStaticMarkup(
      <RunDisclosureBlock
        timeline={{
          ...baseTimeline,
          status: 'blocked',
          steps: [
            {
              id: 'question-1',
              kind: 'question',
              title: 'Ask User Question',
              status: 'blocked',
              summary: '1 question',
              details: { questions: [{ question: 'Which scope?' }] },
              visibility: 'visible',
              blocking: true,
              presentation: 'external',
            },
          ],
        }}
      />,
    );

    expect(markup).not.toContain('Ask User Question');
    expect(markup).not.toContain('Which scope?');
  });

  it('folds intermediate command failures when the overall run completes successfully', () => {
    const markup = renderToStaticMarkup(
      <RunDisclosureBlock
        timeline={{
          ...baseTimeline,
          steps: [
            step({
              id: 'reasoning-1',
              kind: 'reasoning',
              title: 'Thinking',
              summary: 'Private process detail',
              presentation: 'process',
            }),
            step({
              id: 'command-1',
              kind: 'command',
              title: 'Run command',
              status: 'failed',
              summary: 'npm test',
              details: { command: 'npm test', output: '1 failed', exit_code: 1 },
              presentation: 'surface',
            }),
          ],
        }}
      />,
    );

    expect(markup).toContain('Processed');
    expect(markup).not.toContain('Private process detail');
    expect(markup).not.toContain('Run command');
    expect(markup).not.toContain('npm test');
    expect(markup).not.toContain('1 failed');
  });

  it('expands active runs and keeps raw non-command payloads behind the tool history surface', () => {
    const startedAt = new Date(Date.now() - 12000).toISOString();
    const markup = renderToStaticMarkup(
      <RunDisclosureBlock
        timeline={{
          ...baseTimeline,
          status: 'running',
          startedAt,
          steps: [{ ...baseTimeline.steps[0], status: 'running' }],
        }}
      />,
    );

    expect(markup).toContain('session-tui-shimmer');
    expect(markup).toContain('Working');
    expect(markup).toContain('aria-expanded="true"');
    expect(markup).toContain('Using read_file · path: frontend/src/pages/agent-detail/AgentChatSection.tsx');
    expect(markup).toContain('Tool call history');
    expect(markup).not.toContain('RAW FILE CONTENT');
    expect(markup).toMatch(/1[0-9]s/); // live elapsed derived from startedAt
  });

  it('renders command details with a preview, complete recoverable output, and an exit code badge', () => {
    const longOutput = Array.from({ length: 20 }, (_, index) => `line-${index + 1}`).join('\n');
    const markup = renderToStaticMarkup(
      <RunDisclosureBlock
        timeline={{
          ...baseTimeline,
          status: 'running',
          steps: [
            {
              id: 'cmd-1',
              kind: 'command',
              title: 'pytest tests/',
              status: 'failed',
              summary: '',
              details: { command: 'pytest tests/', output: longOutput, exit_code: 1, duration_ms: 2300 },
              visibility: 'collapsed',
            },
          ],
        }}
      />,
    );

    expect(markup).toContain('aria-expanded="true"');
    expect(markup).toContain('session-tui-exec-output');
    expect(markup).toContain('line-1');
    expect(markup).toContain('line-20');
    expect(markup).toContain('…'); // concise preview
    expect(markup).toContain('Show complete output');
    expect(markup).toContain('line-10'); // full evidence remains recoverable
    expect(markup).toContain('exit 1');
  });

  it('renders a live turn as one expanded chronological stream of prose, tool rows, and compaction boundaries', () => {
    const markup = renderToStaticMarkup(<RunDisclosureBlock timeline={liveTimeline()} />);

    expect(markup).toContain('data-testid="run-disclosure-block"');
    expect(markup).toContain('aria-expanded="true"');
    expect(markup.match(/data-testid="run-disclosure-commentary"/g)).toHaveLength(2);
    expect(markup).toContain('I found the <strong>projection bug</strong>');
    expect(markup).not.toContain('Progress update');
    expect(markup.match(/data-testid="run-disclosure-tool-group"/g)).toHaveLength(1);
    expect(markup).toContain('data-testid="run-disclosure-tool-group-toggle"');
    expect(markup).toContain('data-status="running"');
    expect(markup).toContain('Using Read file · chatDisclosureReducer.ts');
    expect(markup).toContain('RunDisclosureBlock.tsx');
    expect(markup).toContain('chatDisclosureReducer.ts');
    expect(markup).toContain('data-testid="run-disclosure-compaction"');
    expect(markup).toContain('Context was automatically compacted');
    expect(markup).not.toContain('INTERNAL COMPACTION DETAILS');
    expect(markup).not.toContain('RAW TOOL RESULT THAT MUST STAY COLLAPSED');

    const firstCommentary = markup.indexOf('I found the <strong>projection bug</strong>');
    const tool = markup.indexOf('Using Read file · chatDisclosureReducer.ts');
    const compaction = markup.indexOf('Context was automatically compacted');
    const secondCommentary = markup.indexOf('The regression is now isolated.');
    expect(firstCommentary).toBeLessThan(tool);
    expect(tool).toBeLessThan(compaction);
    expect(compaction).toBeLessThan(secondCommentary);
  });

  it('folds the complete successful process after final while preserving only the summary toggle', () => {
    const timeline = { ...liveTimeline(), status: 'done' as const };
    const markup = renderToStaticMarkup(<RunDisclosureBlock timeline={timeline} />);

    expect(markup).toContain('Processed');
    expect(markup).toContain('aria-expanded="false"');
    expect(markup).not.toContain('data-testid="run-disclosure-commentary"');
    expect(markup).not.toContain('I found the <strong>projection bug</strong>');
    expect(markup).not.toContain('The regression is now isolated.');
    expect(markup).not.toContain('Read file');
    expect(markup).not.toContain('Context was automatically compacted');
  });
});
