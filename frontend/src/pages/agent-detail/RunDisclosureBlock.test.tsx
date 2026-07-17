import React from 'react';
import { renderToStaticMarkup } from 'react-dom/server';
import { describe, expect, it, vi } from 'vitest';

import RunDisclosureBlock from './RunDisclosureBlock';
import type { RunTimelineSnapshot } from './chatDisclosureReducer';

// Session V2 contract: lifecycle rows remain anchored in the timeline. Tool
// payloads can fold independently, but Thinking/writing/progress stages cannot
// disappear behind a second turn-level disclosure.

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

  it('keeps completed run steps visible while tool payload details remain folded', () => {
    const markup = renderToStaticMarkup(<RunDisclosureBlock timeline={baseTimeline} />);

    expect(markup).toContain('Processed');
    expect(markup).toContain('read_file');
    expect(markup).toContain('AgentChatSection.tsx');
    expect(markup).not.toContain('RAW FILE CONTENT');
  });

  it('keeps Thinking and A2A progress anchored after completion', () => {
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

    expect(markup).toContain('Thinking');
    expect(markup).toContain('Verified the delegated artifact');
    expect(markup).toContain('Action Started');
    expect(markup).toContain('Delegated to Web3 researcher');
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
