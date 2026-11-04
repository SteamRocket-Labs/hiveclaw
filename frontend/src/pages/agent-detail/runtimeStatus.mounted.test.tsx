// @vitest-environment jsdom
import React from 'react';
import { cleanup, render, screen } from '@testing-library/react';
import { afterEach, expect, it, vi } from 'vitest';
import { ActiveTailStatusLine } from './SessionRuntimePanel';
import { runtimeStatusKey, runtimeStatusLabel } from './runtimeStatus';
import { SessionWorkbenchHeader } from '../session-workbench/SessionWorkbenchChrome';
import { buildThreadTimeline } from '../session-workbench/timelineModel';

vi.mock('react-i18next', () => ({ useTranslation: () => ({ t: (_: string, fallback: string) => fallback }) }));
afterEach(cleanup);

it.each([
  ['pending', 'Running'], ['running', 'Running'], ['resumable', 'Running'], ['resuming', 'Running'],
  ['suspended', 'Waiting for you'], ['waiting_budget_approval', 'Waiting for you'],
  ['completed', 'Done'], ['failed', 'Failed'], ['skipped', 'Failed'], ['needs_reconciliation', 'Failed'],
  ['killed', 'Stopped'],
])('renders %s in the shared five-state presentation', (status, label) => {
  render(<ActiveTailStatusLine phase={status} />);
  expect(screen.getByText(label)).toBeTruthy();
  expect(screen.getByTestId('active-tail-status').textContent).not.toMatch(/resum|reconcil|pending/i);
});

it.each([
  ['pending', 'queued', 'Running'], ['running', 'thinking', 'Running'],
  ['suspended', 'awaiting_approval', 'Waiting for you'],
  ['failed', 'failed', 'Failed'], ['killed', 'cancelled', 'Stopped'],
])('mounts the real header for a %s run', (status, phase, label) => {
  const model = buildThreadTimeline({ messages: [{ role: 'user', content: 'Please do this', id: 'input' }],
    activeSession: { id: 'session', title: 'Work' }, activeRunId: 'run', activeRunStatus: status,
    runtimePhase: phase, isWaiting: status === 'pending', isStreaming: status === 'running' });
  render(<SessionWorkbenchHeader model={model.header} />);
  expect(screen.getByTestId('session-workbench-header').textContent).toContain(label);
});

it.each([
  ['pending', 'running'], ['processing', 'running'], ['delivered', 'done'],
  ['dead_letter', 'failed'], ['needs_reconciliation', 'failed'], ['denied', 'failed'],
  ['unavailable', 'failed'], ['unknown', 'failed'],
])('maps IM delivery %s without claiming delivery on failure', (source, display) => {
  expect(runtimeStatusKey(source)).toBe(display);
  expect(runtimeStatusLabel(source, (_, fallback) => fallback)).not.toBe(source);
});
