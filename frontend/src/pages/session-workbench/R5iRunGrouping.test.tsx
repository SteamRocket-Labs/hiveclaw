// @vitest-environment jsdom
import { cleanup, render } from '@testing-library/react';
import { afterEach, expect, it, vi } from 'vitest';
import zh from '../../i18n/zh.json';
import { translateFromCatalog } from '../../test/i18nMock';
import RunDisclosureBlock from '../agent-detail/RunDisclosureBlock';
import type { AgentChatMessage } from '../agent-detail/chatRuntime';
import { buildThreadTimeline, type ThreadTimelineModel } from './timelineModel';
import { hydrateSessionTranscriptEvents } from '../agent-detail/sessionEventConsumer';
import type { ChatTranscriptEventPayload } from '../agent-detail/chatRuntime';
import creationTranscript from './fixtures/r5i-creation.transcript.json';

vi.mock('react-i18next', () => ({ useTranslation: () => ({
  t: (key: string, fallback: string) => translateFromCatalog(zh, key, fallback),
}) }));
afterEach(cleanup);

const scope = (runId: string, turnId = 'turn') => ({ level: 'run' as const,
  session_id: 'session', thread_id: 'session', turn_id: turnId, run_id: runId });
const item = (id: string, kind: string, runId: string, lifecycle = 'completed', turnId = 'turn') =>
  ({ id, kind, scope: scope(runId, turnId), lifecycle, terminal: true }) as AgentChatMessage['sessionItem'];
const user: AgentChatMessage = { id: 'input', role: 'user', content: 'Write the report.', timestamp: '2026-11-08T15:04:00Z' };
const tool: AgentChatMessage = { id: 'write', role: 'tool_call', content: '', toolName: 'write_file', toolStatus: 'done',
  timestamp: '2026-11-08T15:04:01Z', sessionItem: item('write', 'tool_call', 'run'), toolArgs: { path: 'workspace/report.md' } };
const answer: AgentChatMessage = { id: 'answer', role: 'assistant', content: 'The report is ready.',
  timestamp: '2026-11-08T15:04:03Z', sessionItem: item('answer', 'assistant_final', 'run') };
const terminal = (runId: string, lifecycle = 'completed', turnId = 'turn'): AgentChatMessage => ({
  id: runId, role: 'event', content: '', eventType: 'run', timestamp: '2026-11-08T15:04:04Z',
  sessionItem: item(runId, 'run', runId, lifecycle, turnId),
});
const build = (messages: AgentChatMessage[], runtimePhase = 'done') => buildThreadTimeline({ messages,
  activeSession: { id: 'session' }, isWaiting: false, isStreaming: false, runtimePhase });
const runs = (model: ThreadTimelineModel) => model.cells.filter(cell => cell.kind === 'active_run');
const mount = (model: ThreadTimelineModel) => render(<div>{model.cells.map(cell => cell.kind === 'active_run'
  ? <RunDisclosureBlock key={cell.id} timeline={cell.timeline} />
  : cell.kind === 'assistant_final' ? <p key={cell.id}>{cell.message.content}</p>
    : cell.kind === 'boundary' && cell.message.artifacts?.length ? <div key={cell.id} data-testid="file-card">report.md</div> : null)}</div>);

// Literal mixed-plane shapes: canonical tool/final and legacy remote_a2a_artifacts.
// A compatibility answer can arrive before the canonical final for the same run.
it.each(['done', 'idle'])('keeps the file card after one run across live delivery and reload (%s)', phase => {
  const file: AgentChatMessage = { id: 'file', role: 'event', content: '', eventType: 'remote_a2a_artifacts',
    artifacts: [{ id: 'report', name: 'report.md', path: 'workspace/report.md' }] };
  const model = build([user, tool, file, { id: 'compat-answer', role: 'assistant', content: answer.content },
    { ...tool }, answer, terminal('run')], phase);
  expect(runs(model)).toHaveLength(1);
  expect(runs(model)[0].timeline.steps.map(step => step.id)).toEqual(['write']);
  const view = mount(model);
  expect(view.getAllByTestId('run-disclosure-block')).toHaveLength(1);
  expect(view.getByTestId('run-disclosure-block').compareDocumentPosition(view.getByTestId('file-card')) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
});

it('merges a duplicate live/transcript delivery by normalized run identity', () => {
  const runId = '123e4567-e89b-12d3-a456-426614174000';
  const liveAnswer = { ...answer, sessionItem: item('answer-live', 'assistant_final', runId.replaceAll('-', '')) };
  const model = build([user, { ...tool, sessionItem: item('write', 'tool_call', runId) }, liveAnswer,
    { ...answer, sessionItem: item('answer', 'assistant_final', runId) }, terminal(runId)]);
  expect(runs(model)).toHaveLength(1);
  expect(runs(model)[0].runId).toBe(runId);
  expect(runs(model)[0].timeline.steps.map(step => step.id)).toEqual(['write']);
});

// Literal creation sequence: HR tool card, first-task completion, then separate
// integration turns with assistant_final/run.completed and no process steps.
it.each(['done', 'idle'])('omits empty completed follow-up disclosures after creation (%s)', phase => {
  const creation = { ...tool, id: 'create', toolName: 'create_employee_from_request',
    sessionItem: item('create', 'tool_call', 'run'), toolMeta: { kind: 'hr_preview', blueprintId: 'blueprint' } } as AgentChatMessage;
  const model = build([user, creation, answer, terminal('run'),
    { id: 'first-task', role: 'event', content: 'The first task is done.', eventType: 'runtime_action_completed',
      eventRuntimeTaskId: 'child-run', eventStatus: 'completed' },
    { ...answer, id: 'followup-answer', sessionItem: item('followup-answer', 'assistant_final', 'followup', 'completed', 'followup-turn') },
    terminal('followup', 'completed', 'followup-turn'),
    { id: 'empty-final', role: 'assistant', content: '', sessionItem: item('empty-final', 'assistant_final', 'card-followup', 'completed', 'card-turn') },
    terminal('card-followup', 'completed', 'card-turn')], phase);
  expect(runs(model).filter(cell => cell.timeline.status === 'done' && cell.timeline.steps.length === 0)).toEqual([]);
  expect(model.cells.some(cell => cell.kind === 'assistant_final' && cell.id === 'followup-answer')).toBe(true);
  expect(mount(model).getAllByTestId('run-disclosure-block')).toHaveLength(2);
});

it('keeps distinct runs distinct when they have member-visible work after an answer', () => {
  const model = build([user, tool, answer, terminal('run'), { ...tool, id: 'second-write',
    sessionItem: item('second-write', 'tool_call', 'second-run', 'completed', 'second-turn') }, terminal('second-run', 'completed', 'second-turn')]);
  expect(runs(model).map(cell => cell.runId)).toEqual(['run', 'second-run']);
});

it.each([['killed', 'cancelled', '已停止'], ['failed', 'failed', '失败']] as const)('keeps a zero-step %s run visible', (lifecycle, status, label) => {
  const model = build([user, terminal('run', lifecycle)], 'idle');
  expect(runs(model)).toHaveLength(1);
  expect(runs(model)[0].timeline).toMatchObject({ status, steps: [] });
  const view = mount(model);
  expect(view.getAllByText(label)).toHaveLength(1);
});

it.each([false, true])('keeps same-run work after the member answer (earlier final: %s)', earlierFinal => {
  const question: AgentChatMessage = { ...tool, id: 'question', toolName: 'ask_user_question',
    sessionItem: item('question', 'tool_call', 'run'), toolArgs: {}, toolMeta: { kind: 'user_clarification',
      blocking: false, answered: true, nextAction: null, questions: [{ header: 'Report', question: 'Write the file?',
        options: [{ label: 'Yes, write the file', description: '' }], multiSelect: false }] } };
  const memberAnswer: AgentChatMessage = { ...user, id: 'member-answer', content: 'Yes, write the file',
    timestamp: '2026-11-08T15:04:02Z' };
  const write = { ...tool, artifacts: [{ id: 'report', name: 'report.md', path: 'workspace/report.md' }] };
  const model = build([user, question, ...(earlierFinal ? [{ ...answer, id: 'question-final', content: 'Please answer the question.' }] : []),
    memberAnswer, write, answer, terminal('run')], 'idle');
  const memberAt = model.cells.findIndex(cell => cell.kind === 'user_turn' && cell.id === 'member-answer');
  const writeAt = model.cells.findIndex(cell => cell.kind === 'active_run' && cell.sourceMessages.some(s => s.message.id === 'write'));
  expect(writeAt).toBeGreaterThan(memberAt);
  expect(runs(model)).toHaveLength(2);
  expect(runs(model)[0].sourceMessages.map(s => s.message.id)).toEqual(['question']);
  expect(runs(model)[1].sourceMessages).toEqual([expect.objectContaining({ message: write })]);
});

it.each(['done', 'idle'])('keeps zero-step needs_reconciliation visibly failed (%s)', phase => {
  const model = build([user, terminal('run', 'needs_reconciliation')], phase);
  expect(runs(model)).toHaveLength(1);
  expect(runs(model)[0].timeline).toMatchObject({ status: 'failed', steps: [] });
  expect(mount(model).getAllByText('失败')).toHaveLength(1);
});

it.each(['done', 'idle'])('projects the captured creation transcript without split or empty completed runs (%s)', phase => {
  expect(creationTranscript.capture.revision).toBe('d061d97c35e123001901efae10054d14161fec44');
  const { messages } = hydrateSessionTranscriptEvents(creationTranscript.events as ChatTranscriptEventPayload[]);
  expect(messages.some(message => message.toolMeta?.kind === 'hr_preview')).toBe(true);
  const model = buildThreadTimeline({ messages, activeSession: { id: messages[0].sessionItem?.scope.session_id },
    isWaiting: false, isStreaming: false, runtimePhase: phase });
  const knownRunIds = runs(model).map(cell => cell.runId).filter(Boolean);
  expect(knownRunIds.length).toBeGreaterThan(0);
  expect(new Set(knownRunIds).size).toBe(knownRunIds.length);
  expect(runs(model).filter(cell => cell.timeline.status === 'done' && cell.timeline.steps.length === 0)).toEqual([]);
  const memberIndex = model.cells.findIndex(cell => cell.kind === 'user_turn');
  expect(runs(model).every(cell => model.cells.indexOf(cell) > memberIndex)).toBe(true);
  expect(model.cells.filter(cell => cell.kind === 'assistant_final').map(cell => cell.index))
    .toEqual(messages.flatMap((message, index) => message.sessionItem?.kind === 'assistant_final' && message.content.trim() ? [index] : []));
  expect(model.cells.filter(cell => cell.kind === 'assistant_final').map(cell => cell.message.content)).toEqual([
    '正在创建数字员工，完成后可在卡片中和它说话。',
    '已收到，你可以继续交代任务。',
    '已收到，你可以继续交代任务。',
  ]);
});

it.each([false, true])('keeps the richer duplicate tool content (thin first: %s)', thinFirst => {
  const rich: AgentChatMessage = { ...tool, id: 'live-write', sessionItem: item('write-item', 'tool_call', 'run'),
    toolResult: 'Report saved.', toolRawResult: '{"path":"workspace/report.md"}',
    artifacts: [{ id: 'report', name: 'report.md', path: 'workspace/report.md' }] };
  const thin: AgentChatMessage = { ...tool, id: 'transcript-write', sessionItem: item('write-item', 'tool_call', 'run'), toolArgs: undefined };
  const card: AgentChatMessage = { id: 'file', role: 'event', content: '', eventType: 'remote_a2a_artifacts', artifacts: rich.artifacts };
  const [first, second] = thinFirst ? [thin, rich] : [rich, thin];
  const model = build([user, first, card, { id: 'compat-answer', role: 'assistant', content: answer.content }, second, answer]);
  expect(runs(model)).toHaveLength(1);
  expect(runs(model)[0].sourceMessages).toEqual([expect.objectContaining({ message: rich })]);
  expect(runs(model)[0].timeline.steps).toHaveLength(1);
});
