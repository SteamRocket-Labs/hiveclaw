import React from 'react';
import { renderToStaticMarkup } from 'react-dom/server';
import { describe, expect, it, vi } from 'vitest';

import RunDisclosureBlock from './RunDisclosureBlock';
import type { RunTimelineSnapshot } from './chatDisclosureReducer';

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

  it('collapses completed runs by default while keeping a compact readable summary', () => {
    const markup = renderToStaticMarkup(<RunDisclosureBlock timeline={baseTimeline} />);

    expect(markup).toContain('aria-expanded="false"');
    expect(markup).toContain('read_file');
    expect(markup).toContain('AgentChatSection.tsx');
    expect(markup).not.toContain('RAW FILE CONTENT');
  });

  it('expands active runs by default so the running step remains in the thread', () => {
    const markup = renderToStaticMarkup(
      <RunDisclosureBlock
        timeline={{
          ...baseTimeline,
          status: 'running',
          steps: [{ ...baseTimeline.steps[0], status: 'running' }],
        }}
      />,
    );

    expect(markup).toContain('aria-expanded="true"');
    expect(markup).toContain('read_file');
  });
});
