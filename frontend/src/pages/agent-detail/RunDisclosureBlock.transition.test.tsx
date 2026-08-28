// @vitest-environment jsdom

import React from 'react';
import { cleanup, render, screen } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';

import RunDisclosureBlock from './RunDisclosureBlock';
import type { RunTimelineSnapshot } from './chatDisclosureReducer';

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

afterEach(() => {
  cleanup();
  vi.useRealTimers();
});

describe('RunDisclosureBlock live-to-terminal presentation', () => {
  it('keeps the mounted and reloaded duration stable across terminal delivery latency', () => {
    vi.useFakeTimers();
    vi.setSystemTime(new Date('2026-08-29T00:00:16.800Z'));
    const running: RunTimelineSnapshot = {
      id: 'run-delivery-latency',
      status: 'running',
      startedAt: '2026-08-29T00:00:00Z',
      steps: [{
        id: 'prose-1',
        kind: 'prose',
        title: 'Assistant update',
        status: 'running',
        details: 'VISIBLE ANSWER',
        visibility: 'visible',
      }],
    };
    const terminal: RunTimelineSnapshot = {
      ...running,
      status: 'done',
      completedAt: '2026-08-29T00:00:15.588Z',
      durationMs: 15_588,
      steps: running.steps.map((step) => ({ ...step, status: 'done' })),
    };
    const view = render(<RunDisclosureBlock timeline={running} />);

    expect(screen.getByText('16s')).toBeTruthy();
    view.rerender(<RunDisclosureBlock timeline={terminal} />);
    expect(screen.getByText('16s')).toBeTruthy();

    view.unmount();
    render(<RunDisclosureBlock timeline={terminal} />);
    expect(screen.getByText('16s')).toBeTruthy();
  });

  it('never moves the elapsed time backwards when delayed terminal evidence arrives', () => {
    vi.useFakeTimers();
    vi.setSystemTime(new Date('2026-08-29T00:00:14Z'));
    const running: RunTimelineSnapshot = {
      id: 'run-1',
      status: 'running',
      startedAt: '2026-08-29T00:00:00Z',
      steps: [{
        id: 'prose-1',
        kind: 'prose',
        title: 'Assistant update',
        status: 'running',
        details: 'VISIBLE ANSWER',
        visibility: 'visible',
      }],
    };
    const view = render(<RunDisclosureBlock timeline={running} />);

    expect(screen.getByText('14s')).toBeTruthy();

    view.rerender(
      <RunDisclosureBlock
        timeline={{
          ...running,
          status: 'done',
          completedAt: '2026-08-29T00:00:02Z',
          durationMs: 2_000,
          steps: running.steps.map((step) => ({ ...step, status: 'done' })),
        }}
      />,
    );

    expect(screen.getByText('14s')).toBeTruthy();
    expect(screen.queryByText('2s')).toBeNull();
  });
});
