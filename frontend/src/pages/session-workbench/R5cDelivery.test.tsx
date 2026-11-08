// @vitest-environment jsdom
import { render, cleanup } from '@testing-library/react';
import { afterEach, expect, it } from 'vitest';
import { buildThreadTimeline } from './timelineModel';
import type { AgentChatMessage } from '../agent-detail/chatRuntime';

afterEach(cleanup);
const scope = { level: 'run' as const, session_id: 'session', thread_id: 'session', turn_id: 'turn', run_id: 'run' };
const input: AgentChatMessage = { id: 'input', role: 'user', content: 'Continue the report.' };
const started: AgentChatMessage = { id: 'run', role: 'event', content: '', eventType: 'run', timestamp: '2026-11-08T00:00:00Z',
  sessionItem: { id: 'run', kind: 'run', lifecycle: 'running', scope, terminal: false } as AgentChatMessage['sessionItem'] };
const completed: AgentChatMessage = { ...started, timestamp: '2026-11-08T00:00:01Z',
  sessionItem: { ...started.sessionItem!, lifecycle: 'completed', terminal: true } };
const final: AgentChatMessage = { id: 'final', role: 'assistant', content: 'The edited report is ready.',
  sessionItem: { id: 'final', kind: 'assistant_final', lifecycle: 'completed', scope, terminal: true } as AgentChatMessage['sessionItem'] };

it.each(['rest-first', 'terminal-first', 'reply-first'] as const)('renders the reply before done across %s delivery', order => {
  const messages = [input, started];
  const build = () => buildThreadTimeline({ messages: [...messages], activeSession: { id: 'session' },
    activeRunId: 'run', activeRunStatus: 'completed', runtimePhase: 'done', isWaiting: false, isStreaming: false,
    terminalTranscriptPending: true,
    sessionWorkbench: { runtime_tasks: [{ id: 'run', status: 'completed' }] } as never });
  if (order === 'terminal-first') messages[1] = completed;
  if (order !== 'reply-first') {
    const waiting = build();
    expect(waiting.header.status).toBe('running');
    expect(waiting.cells.filter(cell => cell.kind === 'active_run').every(cell => cell.timeline.status === 'running')).toBe(true);
  }
  messages.push(final);
  messages[1] = completed;
  const settled = build();
  expect(settled.header.status).toBe('complete');
  const answer = settled.cells.find(cell => cell.kind === 'assistant_final');
  expect(answer).toBeDefined();
  const view = render(<div>{answer?.kind === 'assistant_final' && answer.message.content}</div>);
  expect(view.getByText('The edited report is ready.')).toBeTruthy();
});

it.each(['tool-only', 'file-card', 'empty-final'] as const)('settles %s after the fresh read and after reload', kind => {
  const message: AgentChatMessage = kind === 'empty-final' ? { ...final, content: '' }
    : { id: 'tool', role: 'tool_call', content: 'File saved', toolName: 'write_file', eventType: 'tool_result',
      sessionItem: { id: 'tool', kind: 'tool_result', lifecycle: 'completed', scope, terminal: true } as AgentChatMessage['sessionItem'],
      ...(kind === 'file-card' ? { artifacts: [{ id: 'report', name: 'report.md', path: 'workspace/report.md' }] } : {}) };
  for (const runtimePhase of ['done', 'idle']) {
    const timelineInput = { messages: [input, completed, message], activeSession: { id: 'session' },
      activeRunId: 'run', activeRunStatus: 'completed', runtimePhase, isWaiting: false, isStreaming: false,
      sessionWorkbench: { runtime_tasks: [{ id: 'run', status: 'completed' }] } as never };
    const pending = buildThreadTimeline({ ...timelineInput, terminalTranscriptPending: true });
    expect(pending.header.status).toBe('running');
    for (const settled of [buildThreadTimeline({ ...timelineInput, terminalTranscriptPending: false }), buildThreadTimeline(timelineInput)]) {
      expect(settled.header.status).toBe('complete');
      expect(settled.cells.filter(cell => cell.kind === 'active_run').map(cell => cell.timeline.status))
        .toEqual(kind === 'empty-final' ? [] : ['done']);
    }
  }
});

it.each([['killed', 'cancelled'], ['failed', 'failed']] as const)('preserves %s while the fresh read is pending', (lifecycle, status) => {
  const model = buildThreadTimeline({ messages: [input, { ...completed,
    sessionItem: { ...completed.sessionItem!, lifecycle } }], activeSession: { id: 'session' },
    activeRunId: 'run', activeRunStatus: lifecycle, runtimePhase: status, isWaiting: false, isStreaming: false,
    terminalTranscriptPending: true });
  expect(model.header.status).toBe(status);
});

it.each(['idle', 'done'])('does not infer a run completion from finished steps while its own status is missing (%s)', runtimePhase => {
  const accepted = { ...input, sessionItem: { id: 'input', kind: 'human_input', lifecycle: 'accepted', terminal: true,
    scope: { level: 'session', session_id: 'session', thread_id: 'session' } } as AgentChatMessage['sessionItem'] };
  const model = buildThreadTimeline({ messages: [accepted, { id: 'tool', role: 'tool_call', content: 'Step finished',
    toolName: 'write_file', toolStatus: 'done' }], activeSession: { id: 'session' },
    runtimePhase, isWaiting: false, isStreaming: false, terminalTranscriptPending: false });
  expect(model.header.status).toBe('running');
  expect(model.cells.filter(cell => cell.kind === 'active_run').map(cell => cell.timeline.status)).toEqual(['running']);
});
