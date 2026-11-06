// @vitest-environment jsdom
import React from 'react';
import { act, cleanup, fireEvent, render, screen } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import { afterEach, expect, it, vi } from 'vitest';
import RunDisclosureBlock from '../agent-detail/RunDisclosureBlock';
import { runtimeStatusKey, runtimeStatusLabel } from '../agent-detail/runtimeStatus';
import type { AgentChatMessage } from '../agent-detail/chatRuntime';
import { buildSessionRightPanelModel, buildThreadTimeline } from './timelineModel';
import { SessionWorkbenchHeader } from './SessionWorkbenchChrome';
import { shouldRenderThreadItemInConversation } from './ThreadItemRenderer';
import { normalizeThreadItemPayload } from './threadItemReducer';
import i18n from 'i18next';
import zh from '../../i18n/zh.json';
import { SessionRuntimePanel } from '../agent-detail/SessionRuntimePanel';

vi.mock('react-i18next', () => ({ useTranslation: () => ({
  t: (_: string, fallback: string) => fallback, i18n: { language: 'en' },
}) }));
afterEach(() => { cleanup(); vi.useRealTimers(); });

const session = { id: 'session-1', title: 'Work', status: 'running' };
function model(status: string) {
  const messages = [
    { id: 'input', role: 'user', content: 'Do this', timestamp: '2026-11-06T00:00:00Z' },
    { id: 'thinking', role: 'thinking', content: 'Thinking', timestamp: '2026-11-06T00:00:01Z',
      sessionItem: { kind: 'assistant_reasoning', lifecycle: 'delta', scope: { level: 'round',
        session_id: session.id, run_id: 'run-1' } } },
    { id: 'run-state', role: 'event', timestamp: '2026-11-06T00:00:03Z',
      sessionItem: { kind: 'run', lifecycle: status, scope: { level: 'run',
        session_id: session.id, run_id: 'run-1' } } },
  ] as AgentChatMessage[];
  const timeline = buildThreadTimeline({ messages, activeSession: session, activeRunId: 'run-1',
    activeRunStatus: status, runtimePhase: 'thinking', isWaiting: true, isStreaming: false });
  return { messages, timeline };
}

it.each([
  ['running', 'Running'], ['suspended', 'Waiting for you'], ['needs_reconciliation', 'Failed'],
  ['killed', 'Stopped'], ['completed', 'Done'], ['failed', 'Failed'],
])('uses the backend %s state across the timeline, header, panel and list', (status, label) => {
  const { messages, timeline } = model(status);
  const runs = timeline.cells.filter(cell => cell.kind === 'active_run');
  expect(runs).toHaveLength(1);
  const run = runs[0];
  expect(run?.kind).toBe('active_run');
  if (run?.kind !== 'active_run') throw Error('Missing run');
  expect(runtimeStatusKey(run.timeline.status)).toBe(runtimeStatusKey(status));
  expect(runtimeStatusKey(timeline.header.status)).toBe(runtimeStatusKey(status));
  const panel = buildSessionRightPanelModel({ messages, activeSession: session,
    activeRunStatus: 'running', presentationStatus: timeline.header.status });
  expect(runtimeStatusKey(panel.sessionWindow?.status)).toBe(runtimeStatusKey(status));
  render(<MemoryRouter><SessionWorkbenchHeader model={timeline.header}
    history={{ agentId: 'agent-1', sessions: [{ ...session, status }], activeSessionId: session.id }} />
    <RunDisclosureBlock timeline={run.timeline} />
    <SessionRuntimePanel messages={messages} sessionWorkbench={null} activeSession={session}
      activeRunStatus={status} presentationStatus={timeline.header.status} /></MemoryRouter>);
  expect(screen.getByTestId('session-workbench-header').textContent).toContain(label);
  expect(screen.getByTestId('run-disclosure-block').textContent).toContain(label);
  expect(screen.getByTestId('session-runtime-run-status').textContent).toContain(label);
  fireEvent.click(screen.getByRole('button', { name: 'History' }));
  expect(screen.getByRole('option').textContent).toContain(label);
});

it('keeps a needs-reconciliation timer frozen despite stale live transport flags', () => {
  vi.useFakeTimers(); vi.setSystemTime(new Date('2026-11-06T00:00:05Z'));
  const { timeline } = model('needs_reconciliation');
  const run = timeline.cells.find(cell => cell.kind === 'active_run');
  if (run?.kind !== 'active_run') throw Error('Missing run');
  render(<RunDisclosureBlock timeline={run.timeline} />);
  const before = screen.getByTestId('run-disclosure-block').textContent;
  act(() => vi.advanceTimersByTime(5000));
  expect(screen.getByTestId('run-disclosure-block').textContent).toBe(before);
  expect(runtimeStatusLabel(timeline.header.status, (_, fallback) => fallback)).toBe('Failed');
});

it('keeps the cancelled run visible after the active-run registry clears, even without a model step', () => {
  const { messages } = model('cancelled');
  const timeline = buildThreadTimeline({ messages: [messages[0], messages[2]], activeSession: session,
    isWaiting: false, isStreaming: false, activeRunId: null, activeRunStatus: null, runtimePhase: 'failed' });
  expect(timeline.header.status).toBe('cancelled');
  expect(timeline.cells).toContainEqual(expect.objectContaining({ kind: 'active_run', runId: 'run-1',
    timeline: expect.objectContaining({ status: 'cancelled' }) }));
});

it('uses the recorded waiting lifecycle over an earlier active-run poll', () => {
  const { messages } = model('waiting');
  const timeline = buildThreadTimeline({ messages, activeSession: session, activeRunId: 'run-1',
    activeRunStatus: 'running', isWaiting: true, isStreaming: false, runtimePhase: 'thinking' });
  expect(timeline.header.status).toBe('waiting');
  expect(timeline.cells).toContainEqual(expect.objectContaining({ kind: 'active_run',
    timeline: expect.objectContaining({ status: 'blocked' }) }));
});

it('uses the run-scoped failure record over its earlier queued run item', () => {
  const { messages } = model('queued');
  const failure = { id: 'failure', role: 'event', content: '', sessionItem: { kind: 'runtime_failure',
    lifecycle: 'recorded', scope: { level: 'run', session_id: session.id, run_id: 'run-1' } } } as AgentChatMessage;
  const timeline = buildThreadTimeline({ messages: [...messages, failure], activeSession: session,
    activeRunId: null, activeRunStatus: null, isWaiting: false, isStreaming: false, runtimePhase: 'failed' });
  expect(timeline.header.status).toBe('failed');
  expect(timeline.cells.some(cell => cell.kind === 'active_run' && cell.timeline.status === 'running')).toBe(false);
});

it('shows the recorded next action for an unconfirmed run without changing its failed state', () => {
  const { messages } = model('needs_reconciliation');
  const timeline = buildThreadTimeline({ messages, activeSession: session, isWaiting: false,
    isStreaming: false, runtimePhase: 'failed', sessionWorkbench: { runtime_tasks: [{ id: 'run-1',
      status: 'needs_reconciliation', user_blocker: { next_action: 'You can continue other work.' } }] } as never });
  render(<SessionWorkbenchHeader model={timeline.header} />);
  expect(timeline.header.status).toBe('failed');
  expect(screen.getByTestId('session-workbench-header').textContent).toContain('You can continue other work.');
});

it('keeps a stopped main run distinct from a later approval-result notification in the panel', () => {
  const { messages } = model('cancelled');
  const panel = buildSessionRightPanelModel({ messages: [...messages, { role: 'event', content: '',
    eventType: 'runtime_action_blocked', eventRuntimeTaskId: 'approval-task' }], activeSession: session,
    presentationStatus: 'cancelled', sessionWorkbench: { runtime_sections: {
      runs: [{ id: 'run-1', status: 'running' }],
    } } });
  expect(panel.runtimeSections.runs[0].status).toBe('cancelled');
});

it('keeps legacy member fallbacks in the existing Chinese wording', async () => {
  await i18n.init({ lng: 'zh', resources: { zh: { translation: zh } }, initImmediate: false });
  expect(normalizeThreadItemPayload({ type: 'permission_resolved' })?.user_summary)
    .toBe(zh.sessionWorkbench.rightPanel.decisionOutcomes.recorded);
  expect(normalizeThreadItemPayload({ type: 'runtime_action_failed' })?.user_summary)
    .toBe(zh.sessionWorkbench.threadItem.failure.unavailable);
  expect(normalizeThreadItemPayload({ type: 'thinking' })?.user_summary).toBe(zh.agent.chat.thinking);
});

it('keeps a historical approval result on its existing card without a second run error', () => {
  const item = normalizeThreadItemPayload({ type: 'runtime_action_blocked', status: 'needs_reconciliation',
    metadata: { runtime_action_from_result_integration: true, notification_source: 'approval' } });
  expect(item).not.toBeNull();
  expect(shouldRenderThreadItemInConversation(item!, false)).toBe(false);
});

it('reuses the stopped process group after reload with no active registry', () => {
  const { messages } = model('cancelled');
  const timeline = buildThreadTimeline({ messages, activeSession: session, activeRunId: null,
    activeRunStatus: null, runtimePhase: 'cancelled', isWaiting: false, isStreaming: false });
  const runs = timeline.cells.filter(cell => cell.kind === 'active_run');
  expect(runs.map(run => run.runId)).toEqual(['run-1']);
  expect(runs.map(run => run.timeline.status)).toEqual(['cancelled']);
});

it('does not apply the current run presentation to an older stopped run', () => {
  const { messages } = model('killed');
  const panel = buildSessionRightPanelModel({ messages, activeSession: session,
    activeRunId: 'run-2', activeRunStatus: 'running', presentationStatus: 'running',
    sessionWorkbench: { runtime_sections: { runs: [
      { id: 'run-1', status: 'killed' }, { id: 'run-2', status: 'running' },
    ] } } });
  expect(panel.runtimeSections.runs.map(run => [run.id, run.status])).toEqual([
    ['run-1', 'killed'], ['run-2', 'running'],
  ]);
});

it('leaves company approval display to its existing approval card', () => {
  const item = normalizeThreadItemPayload({ type: 'permission_request', status: 'approval_required',
    approval_id: 'approval-1', tool_name: 'write_file' });
  expect(item?.item_status).toBe('waiting_user');
  expect(shouldRenderThreadItemInConversation(item!, false)).toBe(false);
  expect(shouldRenderThreadItemInConversation({ ...item!, metadata: { status: 'waiting_user' } }, false)).toBe(false);
  const permission = normalizeThreadItemPayload({ type: 'permission_request',
    status: 'session_permission_required', permission_request_id: 'permission-1' });
  expect(shouldRenderThreadItemInConversation(permission!, false)).toBe(true);
});
