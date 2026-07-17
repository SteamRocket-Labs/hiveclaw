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

  it('collapses completed Thinking while keeping A2A lifecycle progress visible', () => {
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
    expect(markup).toContain('Action Started');
    expect(markup).toContain('Delegated to Web3 researcher');
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

  it('keeps failed commands and other surfaced tool outcomes visible after the process disclosure closes', () => {
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
    expect(markup).toContain('Run command');
    expect(markup).toContain('npm test');
    expect(markup).toContain('1 failed');
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

  it('collapses the whole successful turn after the final answer settles it', () => {
    const timeline = { ...liveTimeline(), status: 'done' as const };
    const markup = renderToStaticMarkup(<RunDisclosureBlock timeline={timeline} />);

    expect(markup).toContain('Processed');
    expect(markup).toContain('aria-expanded="false"');
    expect(markup).not.toContain('run-disclosure-commentary');
    expect(markup).not.toContain('Read file');
    expect(markup).not.toContain('Context was automatically compacted');
  });
});
