import React from 'react';
import { renderToStaticMarkup } from 'react-dom/server';
import { describe, expect, it, vi } from 'vitest';

import RunDisclosureBlock from './RunDisclosureBlock';
import type { RunTimelineSnapshot } from './chatDisclosureReducer';

// Codex-parity contract:
// - running: shimmering "Working" header + live elapsed seconds
// - done: ALWAYS collapses by default to a single boundary row (process
//   recedes; the answer is the star) — including runs that contain reasoning/a2a steps
// - command details render a concise preview plus a recoverable complete output,
//   not a raw JSON blob or an irreversible middle-section deletion

vi.mock('react-i18next', () => ({
  useTranslation: () => ({
    t: (_key: string, fallback?: string, options?: Record<string, unknown>) => {
      if (fallback?.includes('{{count}}')) return fallback.replace('{{count}}', String(options?.count ?? ''));
      return fallback || _key;
    },
  }),
}));

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

  it('collapses completed runs by default to a single boundary row', () => {
    const markup = renderToStaticMarkup(<RunDisclosureBlock timeline={baseTimeline} />);

    expect(markup).toContain('aria-expanded="false"');
    expect(markup).toContain('Processed');
    expect(markup).not.toContain('read_file');
    expect(markup).not.toContain('AgentChatSection.tsx');
    expect(markup).not.toContain('RAW FILE CONTENT');
    expect(markup).not.toContain('run-disclosure-compact-summary');
  });

  it('collapses completed runs even when they contain reasoning or A2A steps', () => {
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
            },
            {
              id: 'a2a-1',
              kind: 'a2a',
              title: 'Action Started',
              status: 'done',
              summary: 'Delegated to Web3 researcher.',
              visibility: 'collapsed',
            },
          ],
        }}
      />,
    );

    // Codex parity: finished work recedes into one line; details come back
    // only when the user expands the same ordered step stream.
    expect(markup).toContain('aria-expanded="false"');
    expect(markup).not.toContain('Verified the delegated artifact');
    expect(markup).not.toContain('Delegated to Web3 researcher');
    expect(markup).not.toContain('run-disclosure-compact-summary');
  });

  it('expands active runs and shows a shimmering Working header with live elapsed', () => {
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

    expect(markup).toContain('aria-expanded="true"');
    expect(markup).toContain('session-tui-shimmer');
    expect(markup).toContain('Working');
    expect(markup).toMatch(/1[0-9]s/); // live elapsed derived from startedAt
  });

  it('renders command details with a preview, complete recoverable output, and an exit code badge', () => {
    const longOutput = Array.from({ length: 20 }, (_, index) => `line-${index + 1}`).join('\n');
    const markup = renderToStaticMarkup(
      <RunDisclosureBlock
        timeline={{
          ...baseTimeline,
          status: 'failed',
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

    // failed runs stay expanded; the command detail is structured
    expect(markup).toContain('aria-expanded="true"');
    expect(markup).toContain('session-tui-exec-output');
    expect(markup).toContain('line-1');
    expect(markup).toContain('line-20');
    expect(markup).toContain('…'); // concise preview
    expect(markup).toContain('Show complete output');
    expect(markup).toContain('line-10'); // full evidence remains recoverable
    expect(markup).toContain('exit 1');
  });
});
