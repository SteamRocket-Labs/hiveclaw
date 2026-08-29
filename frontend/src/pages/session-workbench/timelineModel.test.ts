import { describe, expect, it } from 'vitest';

import {
  buildWorkspaceDocumentsModel,
  buildCheckpointTimelineNodes,
  buildCompletionWakeModel,
  buildRuntimeSectionsModel,
  buildSessionRightPanelModel,
  buildSessionWindowModel,
  buildThreadTimeline,
  buildThreadTimelineCached,
  createThreadTimelineCache,
  buildWorkflowRunWindowModel,
} from './timelineModel';
import type { AgentChatMessage } from '../agent-detail/chatRuntime';
import type { SessionIndex } from '../../api/domains/chat';
import type { SessionWorkbench } from '../../api/domains/ccParity';

describe('session workbench timeline model', () => {
  it('restores the user-observed run duration from the accepted prompt through the final answer', () => {
    const messages: AgentChatMessage[] = [
      {
        id: 'u1',
        role: 'user',
        content: 'Return the exact acceptance marker.',
        timestamp: '2026-08-29T00:00:00Z',
      },
      {
        id: 'r1',
        role: 'assistant',
        content: '',
        thinking: 'Preparing the response.',
        timestamp: '2026-08-29T00:00:12Z',
      },
      {
        id: 'a1',
        role: 'assistant',
        content: 'SESSION-PRESENTATION-PERSISTED',
        timestamp: '2026-08-29T00:00:14Z',
      },
    ];

    const model = buildThreadTimeline({
      messages,
      activeSession: { id: 'session-1', title: 'Persisted duration' },
      isWaiting: false,
      isStreaming: false,
      activeRunStatus: null,
    });

    const run = model.cells.find((cell) => cell.kind === 'active_run');
    expect(run).toBeDefined();
    if (!run || run.kind !== 'active_run') return;
    expect(run.timeline.startedAt).toBe('2026-08-29T00:00:00.000Z');
    expect(run.timeline.completedAt).toBe('2026-08-29T00:00:14.000Z');
    expect(run.timeline.durationMs).toBe(14_000);
  });

  it('restores the run duration through the durable run terminal instead of stopping at the first answer snapshot', () => {
    const runScope = {
      level: 'run' as const,
      session_id: 'session-1',
      thread_id: 'session-1',
      turn_id: 'turn-1',
      run_id: 'run-1',
    };
    const messages = [
      {
        id: 'u1',
        role: 'user',
        content: 'Return the exact acceptance marker.',
        timestamp: '2026-08-29T00:00:00Z',
      },
      {
        id: 'r1',
        role: 'assistant',
        content: '',
        thinking: 'Preparing the response.',
        timestamp: '2026-08-29T00:00:12Z',
        sessionItem: { id: 'reasoning-1', kind: 'assistant_reasoning_summary', scope: runScope, terminal: true },
      },
      {
        id: 'a1',
        role: 'assistant',
        content: 'SESSION-PRESENTATION-PERSISTED',
        timestamp: '2026-08-29T00:00:14Z',
        sessionItem: { id: 'final-1', kind: 'assistant_final', scope: runScope, terminal: true },
      },
      {
        id: 'run-1',
        role: 'event',
        content: '',
        timestamp: '2026-08-29T00:00:25Z',
        eventType: 'run',
        eventStatus: 'completed',
        sessionItem: { id: 'run-1', kind: 'run', lifecycle: 'completed', scope: runScope, terminal: true },
      },
    ] as unknown as AgentChatMessage[];

    const model = buildThreadTimeline({
      messages,
      activeSession: { id: 'session-1', title: 'Persisted terminal duration' },
      isWaiting: false,
      isStreaming: false,
      activeRunStatus: null,
    });

    const run = model.cells.find((cell) => cell.kind === 'active_run');
    expect(run).toBeDefined();
    if (!run || run.kind !== 'active_run') return;
    expect(run.timeline.startedAt).toBe('2026-08-29T00:00:00.000Z');
    expect(run.timeline.completedAt).toBe('2026-08-29T00:00:25.000Z');
    expect(run.timeline.durationMs).toBe(25_000);

    const cache = createThreadTimelineCache();
    const beforeTerminal = buildThreadTimelineCached({
      messages: messages.slice(0, -1),
      activeSession: { id: 'session-1', title: 'Persisted terminal duration' },
      isWaiting: false,
      isStreaming: false,
      activeRunStatus: null,
    }, cache);
    const afterTerminal = buildThreadTimelineCached({
      messages,
      activeSession: { id: 'session-1', title: 'Persisted terminal duration' },
      isWaiting: false,
      isStreaming: false,
      activeRunStatus: null,
    }, cache);
    const beforeTerminalRun = beforeTerminal.cells.find((cell) => cell.kind === 'active_run');
    const afterTerminalRun = afterTerminal.cells.find((cell) => cell.kind === 'active_run');
    expect(afterTerminalRun).not.toBe(beforeTerminalRun);
    expect(afterTerminalRun?.kind === 'active_run' ? afterTerminalRun.timeline.durationMs : null).toBe(25_000);
  });

  it('starts an empty live run stopwatch at the accepted prompt timestamp', () => {
    const model = buildThreadTimeline({
      messages: [{
        id: 'u1',
        role: 'user',
        content: 'Start the run.',
        timestamp: '2026-08-29T00:00:00Z',
      }],
      activeSession: { id: 'session-1', title: 'Live duration' },
      isWaiting: true,
      isStreaming: false,
      activeRunStatus: 'running',
    });

    const run = model.cells.find((cell) => cell.kind === 'active_run');
    expect(run).toBeDefined();
    if (!run || run.kind !== 'active_run') return;
    expect(run.timeline.startedAt).toBe('2026-08-29T00:00:00Z');
    expect(run.timeline.steps[0]).toMatchObject({
      kind: 'reasoning',
      title: 'Thinking',
    });
    expect(run.timeline.steps[0]?.summary).toBeUndefined();
    expect(JSON.stringify(run.timeline)).not.toContain('Active run:');
    expect(JSON.stringify(run.timeline)).not.toContain('continuing this turn');
  });

  it('renders a historical non-terminal turn as interrupted with a frozen duration when no run is active', () => {
    // UI-004 regression: replaying a session whose last turn never wrote a
    // terminal lifecycle mark must not fabricate a live processing run with an
    // ever-growing stopwatch; duration freezes at the last durable step.
    const messages: AgentChatMessage[] = [
      { id: 'u1', role: 'user', content: 'Draft the Q2 summary.' },
      {
        id: 'r1',
        role: 'assistant',
        content: '',
        thinking: 'I need the Q2 notes.',
        timestamp: '2026-07-17T08:00:00Z',
        sessionItem: { id: 'r1', kind: 'assistant_reasoning_summary', terminal: false },
      },
      {
        id: 't1',
        role: 'tool_call',
        content: '',
        toolName: 'read_file',
        toolArgs: { path: 'notes/q2.md' },
        toolStatus: 'running',
        timestamp: '2026-07-17T08:00:20Z',
      },
    ] as unknown as AgentChatMessage[];

    const model = buildThreadTimeline({
      messages,
      activeSession: { id: 'session-1', title: 'Historical session' },
      isWaiting: false,
      isStreaming: false,
      activeRunStatus: null,
    });

    const trailingRun = model.cells[model.cells.length - 1];
    expect(trailingRun).toMatchObject({ kind: 'active_run' });
    if (trailingRun.kind !== 'active_run') return;
    expect(trailingRun.timeline.status).toBe('interrupted');
    expect(trailingRun.timeline.completedAt).toBeUndefined();
    expect(trailingRun.timeline.durationMs).toBe(20_000);
    expect(model.header.status).toBe('idle');
  });

  it('renders a copied cancelled branch as interrupted instead of fabricating a current run', () => {
    const model = buildThreadTimeline({
      messages: [{
        id: 'copied-user-1',
        role: 'user',
        content: 'Continue the interrupted request.',
        timestamp: '2026-08-29T04:18:34Z',
      }],
      activeSession: { id: 'branch-session-1', title: 'Interrupted branch' },
      isWaiting: false,
      isStreaming: false,
      activeRunStatus: null,
      runtimePhase: 'cancelled',
    });

    expect(model.header.status).toBe('idle');
    expect(model.cells.at(-1)).toMatchObject({
      kind: 'active_run',
      timeline: { status: 'interrupted' },
    });
    expect(model.cells.some((cell) => cell.kind === 'active_run' && cell.timeline.status === 'running')).toBe(false);
  });

  it('upgrades the trailing run to live running when the authoritative runtime reports an active run', () => {
    const messages: AgentChatMessage[] = [
      { id: 'u1', role: 'user', content: 'Draft the Q2 summary.' },
      {
        id: 'r1',
        role: 'assistant',
        content: '',
        thinking: 'I need the Q2 notes.',
        timestamp: '2026-07-17T08:00:00Z',
        sessionItem: { id: 'r1', kind: 'assistant_reasoning_summary', terminal: false },
      },
    ] as unknown as AgentChatMessage[];

    const model = buildThreadTimeline({
      messages,
      activeSession: { id: 'session-1', title: 'Live session' },
      isWaiting: false,
      isStreaming: false,
      activeRunStatus: 'running',
    });

    const trailingRun = model.cells[model.cells.length - 1];
    expect(trailingRun).toMatchObject({ kind: 'active_run' });
    if (trailingRun.kind !== 'active_run') return;
    expect(trailingRun.timeline.status).toBe('running');
    expect(trailingRun.timeline.completedAt).toBeUndefined();
    expect(model.header.status).toBe('running');
  });

  it('does not expose round result-commit bookkeeping as a second user process disclosure', () => {
    const runScope = {
      level: 'round' as const,
      session_id: 'session-1',
      thread_id: 'session-1',
      turn_id: 'turn-1',
      run_id: 'run-1',
      round_id: 'round-1',
    };
    const model = buildThreadTimeline({
      messages: [
        {
          id: 'user-1',
          role: 'user',
          content: 'Create the employee preview.',
          timestamp: '2026-08-29T00:00:00Z',
        },
        {
          id: 'reasoning-1',
          role: 'assistant',
          content: '',
          timestamp: '2026-08-29T00:01:40Z',
          sessionItem: {
            id: 'reasoning-1',
            kind: 'assistant_reasoning_summary',
            lifecycle: 'completed',
            scope: runScope,
            terminal: true,
          },
        },
        {
          id: 'answer-1',
          role: 'assistant',
          content: 'The preview is ready.',
          timestamp: '2026-08-29T00:01:44Z',
          sessionItem: {
            id: 'answer-1',
            kind: 'assistant_final',
            lifecycle: 'completed',
            scope: runScope,
            terminal: true,
          },
        },
        {
          id: 'result-commit-1',
          role: 'event',
          content: '',
          timestamp: '2026-08-29T00:01:45Z',
          sessionItem: {
            id: 'result-commit-1',
            kind: 'result_commit',
            lifecycle: 'round_committed',
            scope: runScope,
            terminal: true,
          },
        },
        {
          id: 'file-changes-1',
          role: 'event',
          content: 'file_changes',
          timestamp: '2026-08-29T00:01:46Z',
          eventType: 'file_changes',
          eventStatus: 'succeeded',
          eventRuntimeTaskId: 'run-1',
        },
      ] as AgentChatMessage[],
      activeSession: { id: 'session-1', title: 'HR preview' },
      isWaiting: false,
      isStreaming: false,
      activeRunStatus: null,
    });

    const runCells = model.cells.filter((cell) => cell.kind === 'active_run');
    expect(runCells).toHaveLength(1);
    expect(runCells[0]).toMatchObject({
      runId: 'run-1',
      timeline: {
        status: 'done',
        durationMs: 104_000,
      },
    });
    if (runCells[0]?.kind === 'active_run') {
      expect(runCells[0].sourceMessages.some((entry) => entry.message.id === 'file-changes-1')).toBe(true);
      expect(runCells[0].timeline.steps.some((step) => step.kind === 'artifact')).toBe(true);
    }
    expect(model.cells.some((cell) => (
      cell.kind === 'boundary' && cell.message.sessionItem?.kind === 'result_commit'
    ))).toBe(false);
  });

  it('keeps a newly active run separate while its accepted input has not reached the transcript projection', () => {
    const runOneScope = {
      level: 'round' as const,
      session_id: 'session-1',
      thread_id: 'session-1',
      turn_id: 'turn-1',
      run_id: 'run-1',
      round_id: 'round-1',
    };
    const model = buildThreadTimeline({
      messages: [
        {
          id: 'user-1',
          role: 'user',
          content: 'Create the employee preview.',
          timestamp: '2026-08-29T00:00:00Z',
        },
        {
          id: 'reasoning-1',
          role: 'assistant',
          content: '',
          timestamp: '2026-08-29T00:01:40Z',
          sessionItem: {
            id: 'reasoning-1',
            kind: 'assistant_reasoning_summary',
            lifecycle: 'completed',
            scope: runOneScope,
            terminal: true,
          },
        },
        {
          id: 'answer-1',
          role: 'assistant',
          content: 'The preview is ready.',
          timestamp: '2026-08-29T00:01:44Z',
          sessionItem: {
            id: 'answer-1',
            kind: 'assistant_final',
            lifecycle: 'completed',
            scope: runOneScope,
            terminal: true,
          },
        },
        {
          id: 'result-commit-1',
          role: 'event',
          content: '',
          timestamp: '2026-08-29T00:01:45Z',
          sessionItem: {
            id: 'result-commit-1',
            kind: 'result_commit',
            lifecycle: 'round_committed',
            scope: runOneScope,
            terminal: true,
          },
        },
        {
          id: 'file-changes-1',
          role: 'event',
          content: 'file_changes',
          timestamp: '2026-08-29T00:01:46Z',
          eventType: 'file_changes',
          eventStatus: 'succeeded',
          eventRuntimeTaskId: 'run-1',
        },
      ] as AgentChatMessage[],
      activeSession: { id: 'session-1', title: 'HR revision' },
      isWaiting: true,
      isStreaming: false,
      activeRunStatus: 'running',
      activeRunId: 'run-2',
    });

    const runCells = model.cells.filter((cell) => cell.kind === 'active_run');
    expect(runCells).toHaveLength(2);
    expect(runCells[0]).toMatchObject({
      runId: 'run-1',
      timeline: { status: 'done', durationMs: 104_000 },
    });
    expect(runCells[1]).toMatchObject({
      runId: 'run-2',
      timeline: { status: 'running' },
    });
    if (runCells[1]?.kind === 'active_run') {
      expect(runCells[1].timeline.startedAt).toBeUndefined();
    }
  });

  it('never upgrades a differently identified unresolved process run', () => {
    const model = buildThreadTimeline({
      messages: [
        {
          id: 'user-1',
          role: 'user',
          content: 'Finish the first run.',
          timestamp: '2026-08-29T00:00:00Z',
        },
        {
          id: 'reasoning-1',
          role: 'assistant',
          content: '',
          timestamp: '2026-08-29T00:00:20Z',
          sessionItem: {
            id: 'reasoning-1',
            kind: 'assistant_reasoning_summary',
            lifecycle: 'started',
            terminal: false,
            scope: {
              level: 'round',
              session_id: 'session-1',
              thread_id: 'session-1',
              turn_id: 'turn-1',
              run_id: 'run-1',
              round_id: 'round-1',
            },
          },
        },
      ] as AgentChatMessage[],
      activeSession: { id: 'session-1', title: 'Run handoff' },
      isWaiting: true,
      isStreaming: false,
      activeRunStatus: 'running',
      activeRunId: 'run-2',
    });

    const runCells = model.cells.filter((cell) => cell.kind === 'active_run');
    expect(runCells).toHaveLength(2);
    expect(runCells[0]).toMatchObject({
      runId: 'run-1',
      timeline: { status: 'interrupted', durationMs: 20_000 },
    });
    expect(runCells[1]).toMatchObject({
      runId: 'run-2',
      timeline: { status: 'running' },
    });
  });

  it('starts the new run at its own accepted input after the second turn is projected', () => {
    const runOneScope = {
      level: 'round' as const,
      session_id: 'session-1',
      thread_id: 'session-1',
      turn_id: 'turn-1',
      run_id: 'run-1',
      round_id: 'round-1',
    };
    const model = buildThreadTimeline({
      messages: [
        {
          id: 'user-1',
          role: 'user',
          content: 'Create the employee preview.',
          timestamp: '2026-08-29T00:00:00Z',
        },
        {
          id: 'reasoning-1',
          role: 'assistant',
          content: '',
          timestamp: '2026-08-29T00:01:40Z',
          sessionItem: {
            id: 'reasoning-1',
            kind: 'assistant_reasoning_summary',
            lifecycle: 'completed',
            scope: runOneScope,
            terminal: true,
          },
        },
        {
          id: 'answer-1',
          role: 'assistant',
          content: 'The preview is ready.',
          timestamp: '2026-08-29T00:01:44Z',
          sessionItem: {
            id: 'answer-1',
            kind: 'assistant_final',
            lifecycle: 'completed',
            scope: runOneScope,
            terminal: true,
          },
        },
        {
          id: 'result-commit-1',
          role: 'event',
          content: '',
          timestamp: '2026-08-29T00:01:45Z',
          sessionItem: {
            id: 'result-commit-1',
            kind: 'result_commit',
            lifecycle: 'round_committed',
            scope: runOneScope,
            terminal: true,
          },
        },
        {
          id: 'file-changes-1',
          role: 'event',
          content: 'file_changes',
          timestamp: '2026-08-29T00:01:46Z',
          eventType: 'file_changes',
          eventStatus: 'succeeded',
          eventRuntimeTaskId: 'run-1',
        },
        {
          id: 'user-2',
          role: 'user',
          content: 'Change only the employee name.',
          timestamp: '2026-08-29T00:20:34Z',
        },
      ] as AgentChatMessage[],
      activeSession: { id: 'session-1', title: 'HR revision' },
      isWaiting: true,
      isStreaming: false,
      activeRunStatus: 'running',
      activeRunId: 'run-2',
    });

    const runCells = model.cells.filter((cell) => cell.kind === 'active_run');
    expect(runCells).toHaveLength(2);
    expect(runCells[0]).toMatchObject({
      runId: 'run-1',
      timeline: { status: 'done', durationMs: 104_000 },
    });
    expect(runCells[1]).toMatchObject({
      runId: 'run-2',
      timeline: {
        status: 'running',
        startedAt: '2026-08-29T00:20:34Z',
      },
    });
  });

  it('does not append a second pending disclosure after the latest turn has a terminal answer', () => {
    const runScope = {
      level: 'round' as const,
      session_id: 'session-1',
      thread_id: 'session-1',
      turn_id: 'turn-1',
      run_id: 'run-1',
      round_id: 'round-1',
    };
    const model = buildThreadTimeline({
      messages: [
        {
          id: 'user-1',
          role: 'user',
          content: 'Return the terminal marker.',
          timestamp: '2026-08-29T00:00:00Z',
        },
        {
          id: 'reasoning-1',
          role: 'assistant',
          content: '',
          timestamp: '2026-08-29T00:00:10Z',
          sessionItem: {
            id: 'reasoning-1',
            kind: 'assistant_reasoning_summary',
            lifecycle: 'completed',
            scope: runScope,
            terminal: true,
          },
        },
        {
          id: 'answer-1',
          role: 'assistant',
          content: 'SESSION-TERMINAL-TAIL',
          timestamp: '2026-08-29T00:00:13Z',
          sessionItem: {
            id: 'answer-1',
            kind: 'assistant_final',
            lifecycle: 'completed',
            scope: runScope,
            terminal: true,
          },
        },
      ] as AgentChatMessage[],
      activeSession: { id: 'session-1', title: 'Terminal tail' },
      isWaiting: true,
      isStreaming: false,
      activeRunStatus: 'running',
      activeRunId: 'run-1',
      runtimePhase: 'thinking',
    });

    expect(model.cells.filter((cell) => cell.kind === 'active_run')).toHaveLength(1);
    expect(model.cells.some((cell) => cell.kind === 'assistant_final')).toBe(true);
    expect(model.cells).not.toContainEqual(expect.objectContaining({ id: 'active-run-pending' }));
  });

  it('groups delayed evidence by exact run identity even after the next user turn is visible', () => {
    const runScope = {
      level: 'round' as const,
      session_id: 'session-1',
      thread_id: 'session-1',
      turn_id: 'turn-1',
      run_id: 'run-1',
      round_id: 'round-1',
    };
    const model = buildThreadTimeline({
      messages: [
        {
          id: 'user-1',
          role: 'user',
          content: 'Finish run one.',
          timestamp: '2026-08-29T00:00:00Z',
        },
        {
          id: 'reasoning-1',
          role: 'assistant',
          content: '',
          timestamp: '2026-08-29T00:00:10Z',
          sessionItem: {
            id: 'reasoning-1',
            kind: 'assistant_reasoning_summary',
            lifecycle: 'completed',
            scope: runScope,
            terminal: true,
          },
        },
        {
          id: 'answer-1',
          role: 'assistant',
          content: 'Run one is complete.',
          timestamp: '2026-08-29T00:00:12Z',
          sessionItem: {
            id: 'answer-1',
            kind: 'assistant_final',
            lifecycle: 'completed',
            scope: runScope,
            terminal: true,
          },
        },
        {
          id: 'user-2',
          role: 'user',
          content: 'Start run two.',
          timestamp: '2026-08-29T00:01:00Z',
        },
        {
          id: 'late-file-changes-1',
          role: 'event',
          content: 'file_changes',
          timestamp: '2026-08-29T00:01:01Z',
          eventType: 'file_changes',
          eventStatus: 'succeeded',
          eventRuntimeTaskId: 'run-1',
        },
      ] as AgentChatMessage[],
      activeSession: { id: 'session-1', title: 'Delayed evidence' },
      isWaiting: true,
      isStreaming: false,
      activeRunStatus: 'running',
      activeRunId: 'run-2',
    });

    const runCells = model.cells.filter((cell) => cell.kind === 'active_run');
    expect(runCells).toHaveLength(2);
    expect(runCells[0]).toMatchObject({ runId: 'run-1', timeline: { status: 'done', durationMs: 12_000 } });
    expect(runCells[1]).toMatchObject({
      runId: 'run-2',
      timeline: { status: 'running', startedAt: '2026-08-29T00:01:00Z' },
    });
    if (runCells[0]?.kind === 'active_run') {
      expect(runCells[0].sourceMessages.some((entry) => entry.message.id === 'late-file-changes-1')).toBe(true);
    }
  });

  it('reuses the previous timeline model when streaming state did not change', () => {
    const messages: AgentChatMessage[] = [
      { id: 'u1', role: 'user', content: 'Profile the session renderer.' },
      { id: 'a1', role: 'assistant', content: 'The baseline is ready.' },
    ];
    const cache = createThreadTimelineCache();

    const first = buildThreadTimelineCached({
      messages,
      activeSession: { id: 'session-1', title: 'Perf baseline' },
      isWaiting: false,
      isStreaming: false,
      activeRunStatus: null,
    }, cache);
    const second = buildThreadTimelineCached({
      messages,
      activeSession: { id: 'session-1', title: 'Perf baseline' },
      isWaiting: false,
      isStreaming: false,
      activeRunStatus: null,
    }, cache);

    expect(second).toBe(first);
  });

  it('keeps stable cell identities for the unchanged static history prefix', () => {
    const messages: AgentChatMessage[] = [
      { id: 'u1', role: 'user', content: 'First prompt.' },
      { id: 'a1', role: 'assistant', content: 'First answer.' },
    ];
    const cache = createThreadTimelineCache();
    const first = buildThreadTimelineCached({
      messages,
      activeSession: { id: 'session-1', title: 'Incremental timeline' },
      isWaiting: false,
      isStreaming: false,
      activeRunStatus: null,
    }, cache);

    const appended = [...messages, { id: 'u2', role: 'user', content: 'Second prompt.' } satisfies AgentChatMessage];
    const second = buildThreadTimelineCached({
      messages: appended,
      activeSession: { id: 'session-1', title: 'Incremental timeline' },
      isWaiting: false,
      isStreaming: false,
      activeRunStatus: null,
    }, cache);

    expect(second).not.toBe(first);
    expect(second.cells[0]).toBe(first.cells[0]);
    expect(second.cells[1]).toBe(first.cells[1]);
    expect(second.cells.at(-2)).toMatchObject({ kind: 'user_turn', id: 'u2' });
    expect(second.cells.at(-1)).toMatchObject({ kind: 'active_run', id: 'active-run-pending' });
  });

  it('does not scan signature-only inputs when the message reference changed', () => {
    const messages: AgentChatMessage[] = [
      { id: 'u1', role: 'user', content: 'First prompt.' },
    ];
    const cache = createThreadTimelineCache();
    buildThreadTimelineCached({
      messages,
      activeSession: { id: 'session-1', title: 'Streaming timeline' },
      isWaiting: false,
      isStreaming: true,
      activeRunStatus: null,
    }, cache);

    let signatureReads = 0;
    const sessionIndex = {} as SessionIndex & { expensive_signature_only_field: string };
    Object.defineProperty(sessionIndex, 'expensive_signature_only_field', {
      enumerable: true,
      get() {
        signatureReads += 1;
        return 'large-derived-index';
      },
    });

    buildThreadTimelineCached({
      messages: [...messages, { id: 'a1', role: 'assistant', content: 'Streaming answer.' }],
      activeSession: { id: 'session-1', title: 'Streaming timeline' },
      sessionIndex,
      isWaiting: false,
      isStreaming: true,
      activeRunStatus: null,
    }, cache);

    expect(signatureReads).toBe(0);
  });

  it('projects a turn as a run process cell followed by a final answer cell', () => {
    const messages: AgentChatMessage[] = [
      { id: 'u1', role: 'user', content: 'Check the current frontend state.' },
      { id: 'r1', role: 'assistant', content: '', thinking: 'Need to inspect chat code.' },
      { id: 't1', role: 'tool_call', content: '', toolName: 'read_file', toolArgs: { path: 'AgentChatSection.tsx' }, toolStatus: 'done' },
      { id: 'a1', role: 'assistant', content: 'The root issue is the chat-tab presentation model.' },
    ];

    const model = buildThreadTimeline({
      messages,
      activeSession: { id: 'session-1', title: 'Frontend refactor' },
      isWaiting: false,
      isStreaming: false,
      activeRunStatus: null,
    });

    expect(model.cells.map((cell) => cell.kind)).toEqual(['user_turn', 'active_run', 'assistant_final']);
    const runCell = model.cells[1];
    expect(runCell.kind).toBe('active_run');
    if (runCell.kind !== 'active_run') throw new Error('expected active run cell');
    expect(runCell.timeline.steps.map((step) => step.kind)).toEqual(['reasoning', 'file']);
    expect(runCell.timeline.steps.some((step) => step.title === 'Writing response')).toBe(false);
    const answerCell = model.cells[2];
    expect(answerCell.kind).toBe('assistant_final');
    if (answerCell.kind !== 'assistant_final') throw new Error('expected assistant final cell');
    expect(answerCell.message.content).toContain('presentation model');
    expect(model.header.status).toBe('complete');
  });

  it('uses Session V2 item kinds instead of assistant text content to decide finality', () => {
    const messages: AgentChatMessage[] = [
      { id: 'u1', role: 'user', content: 'Inspect it.' },
      {
        id: 'unknown-1',
        role: 'assistant',
        content: 'This is provider text with no final phase.',
        sessionItem: {
          id: 'unknown-1', kind: 'assistant_text', lifecycle: 'completed', terminal: true,
        } as AgentChatMessage['sessionItem'],
      },
    ];

    const model = buildThreadTimeline({
      messages,
      activeSession: { id: 'session-1', title: 'Typed finality' },
      isWaiting: false,
      isStreaming: false,
      activeRunStatus: 'running',
    });

    expect(model.cells[0]?.kind).toBe('user_turn');
    expect(model.cells.filter((cell) => cell.kind === 'active_run').length).toBeGreaterThanOrEqual(1);
    expect(model.cells.some((cell) => cell.kind === 'assistant_final')).toBe(false);
    const activeRun = model.cells.find((cell) => cell.kind === 'active_run');
    if (!activeRun || activeRun.kind !== 'active_run') throw new Error('expected active run cell');
    expect(activeRun.timeline.steps).toEqual([
      expect.objectContaining({
        kind: 'prose',
        details: 'This is provider text with no final phase.',
      }),
    ]);
  });

  it('keeps one canonical process stream open until the typed final answer arrives', () => {
    const messages: AgentChatMessage[] = [
      { id: 'u1', role: 'user', content: 'Fix the Session presentation.' },
      {
        id: 'commentary-1',
        role: 'assistant',
        content: 'I reproduced the projection problem and am checking the renderer.',
        eventType: 'assistant_commentary',
        eventStatus: 'completed',
        sessionItem: {
          id: 'commentary-1',
          kind: 'assistant_commentary',
          lifecycle: 'completed',
          terminal: true,
        } as AgentChatMessage['sessionItem'],
      },
      {
        id: 'tool-1',
        role: 'tool_call',
        content: '',
        toolName: 'read_file',
        toolArgs: { path: 'frontend/src/pages/agent-detail/RunDisclosureBlock.tsx' },
        toolStatus: 'done',
        sessionItem: {
          id: 'tool-1',
          kind: 'tool_call',
          lifecycle: 'completed',
          terminal: true,
        } as AgentChatMessage['sessionItem'],
      },
      {
        id: 'compaction-1',
        role: 'event',
        content: 'The active working state was preserved.',
        eventType: 'context_compaction',
        eventTitle: 'Context compaction',
        eventStatus: 'completed',
        sessionItem: {
          id: 'compaction-1',
          kind: 'context_compaction',
          lifecycle: 'completed',
          terminal: true,
        } as AgentChatMessage['sessionItem'],
      },
    ];

    const model = buildThreadTimeline({
      messages,
      activeSession: { id: 'session-1', title: 'Canonical process stream' },
      isWaiting: true,
      isStreaming: false,
      activeRunStatus: 'running',
    });

    expect(model.cells.map((cell) => cell.kind)).toEqual(['user_turn', 'active_run']);
    const runCell = model.cells[1];
    expect(runCell.kind).toBe('active_run');
    if (runCell.kind !== 'active_run') throw new Error('expected active run cell');
    expect(runCell.timeline.status).toBe('running');
    expect(runCell.timeline.answerMessageId).toBeUndefined();
    expect(runCell.timeline.steps.map((step) => step.kind)).toEqual(['commentary', 'file', 'compaction']);
  });

  it('keeps legacy assistant_commentary inside the run and assistant_message on the final surface', () => {
    const messages: AgentChatMessage[] = [
      { id: 'u1', role: 'user', content: 'Open the older Session transcript.' },
      {
        id: 'legacy-commentary',
        role: 'assistant',
        content: 'I am restoring the historical tool stream.',
        eventType: 'assistant_commentary',
        eventStatus: 'completed',
      },
      {
        id: 'legacy-final',
        role: 'assistant',
        content: 'The historical Session transcript is restored.',
        eventType: 'assistant_message',
        eventStatus: 'completed',
      },
    ];

    const model = buildThreadTimeline({
      messages,
      activeSession: { id: 'session-1', title: 'Legacy process stream' },
      isWaiting: false,
      isStreaming: false,
      activeRunStatus: null,
    });

    expect(model.cells.map((cell) => cell.kind)).toEqual(['user_turn', 'active_run', 'assistant_final']);
    const runCell = model.cells[1];
    expect(runCell.kind).toBe('active_run');
    if (runCell.kind !== 'active_run') throw new Error('expected active run cell');
    expect(runCell.timeline.steps).toEqual([
      expect.objectContaining({ id: 'legacy-commentary', kind: 'commentary' }),
    ]);
    expect(runCell.timeline.answerMessageId).toBe('legacy-final');
  });

  it('keeps completed run steps in interleaved thinking/tool sequence', () => {
    const messages: AgentChatMessage[] = [
      { id: 'u1', role: 'user', content: 'Fix the session renderer.' },
      { id: 'r1', role: 'assistant', content: '', thinking: 'Need to inspect the chat renderer.' },
      { id: 't1', role: 'tool_call', content: '', toolName: 'read_file', toolArgs: { path: 'AgentChatSection.tsx' }, toolStatus: 'done' },
      { id: 'r2', role: 'assistant', content: '', thinking: 'Now inspect the timeline projection.' },
      { id: 't2', role: 'tool_call', content: '', toolName: 'read_file', toolArgs: { path: 'timelineModel.ts' }, toolStatus: 'done' },
      { id: 'a1', role: 'assistant', content: 'Done. The process is fixed.' },
    ];

    const model = buildThreadTimeline({
      messages,
      activeSession: { id: 'session-1', title: 'Session renderer' },
      isWaiting: false,
      isStreaming: false,
      activeRunStatus: null,
    });

    expect(model.cells.map((cell) => cell.kind)).toEqual(['user_turn', 'active_run', 'assistant_final']);
    const runCell = model.cells[1];
    expect(runCell.kind).toBe('active_run');
    if (runCell.kind !== 'active_run') throw new Error('expected active run cell');
    expect(runCell.timeline.steps.map((step) => `${step.kind}:${step.summary}`)).toEqual([
      'reasoning:Need to inspect the chat renderer.',
      'file:AgentChatSection.tsx',
      'reasoning:Now inspect the timeline projection.',
      'file:timelineModel.ts',
    ]);
    const answerCell = model.cells[2];
    expect(answerCell.kind).toBe('assistant_final');
    if (answerCell.kind !== 'assistant_final') throw new Error('expected assistant final cell');
    expect(answerCell.message.content).toBe('Done. The process is fixed.');
  });

  it('keeps an assistant answer visible when reasoning and content share one transcript message', () => {
    const messages: AgentChatMessage[] = [
      { id: 'u1', role: 'user', content: 'Check the checkpoint behavior.' },
      {
        id: 'a1',
        role: 'assistant',
        content: 'The checkpoint should point before the selected prompt.',
        thinking: 'Need to compare branch and rewind semantics.',
      },
    ];

    const model = buildThreadTimeline({
      messages,
      activeSession: { id: 'session-1', title: 'Checkpoint semantics' },
      isWaiting: false,
      isStreaming: false,
      activeRunStatus: null,
    });

    expect(model.cells.map((cell) => cell.kind)).toEqual(['user_turn', 'active_run', 'assistant_final']);
    const runCell = model.cells[1];
    expect(runCell.kind).toBe('active_run');
    if (runCell.kind !== 'active_run') throw new Error('expected active run cell');
    expect(runCell.timeline.steps.map((step) => step.kind)).toEqual(['reasoning']);
    const answerCell = model.cells[2];
    expect(answerCell.kind).toBe('assistant_final');
    if (answerCell.kind !== 'assistant_final') throw new Error('expected assistant final cell');
    expect(answerCell.message.content).toContain('point before the selected prompt');
  });

  it('marks question and plan tool calls as blocking active run steps', () => {
    const messages: AgentChatMessage[] = [
      { id: 'u1', role: 'user', content: 'Make a plan first.' },
      {
        id: 'q1',
        role: 'tool_call',
        content: '',
        toolName: 'ask_user_question',
        toolStatus: 'done',
        toolMeta: {
          kind: 'user_clarification',
          blocking: true,
          nextAction: null,
          questions: [{ header: 'Scope', question: 'Which scope?', options: [], multiSelect: false }],
        },
      },
    ];

    const model = buildThreadTimeline({
      messages,
      activeSession: { id: 'session-1', title: 'Blocking run' },
      isWaiting: true,
      isStreaming: false,
      activeRunStatus: 'running',
    });

    expect(model.header.status).toBe('waiting');
    expect(model.cells.map((cell) => cell.kind)).toEqual(['user_turn', 'active_run']);
    const runCell = model.cells[1];
    expect(runCell.kind).toBe('active_run');
    if (runCell.kind !== 'active_run') throw new Error('expected active run cell');
    expect(runCell.timeline.status).toBe('blocked');
    expect(runCell.timeline.steps[0]).toMatchObject({ kind: 'question', status: 'blocked', blocking: true });
  });

  it('turns an active waiting run into a timeline cell instead of a detached loading bubble', () => {
    const model = buildThreadTimeline({
      messages: [{ id: 'u1', role: 'user', content: 'Continue the previous run.' }],
      activeSession: { id: 'session-1', title: 'Continuing run' },
      isWaiting: true,
      isStreaming: false,
      activeRunStatus: 'running',
    });

    expect(model.header.status).toBe('waiting');
    expect(model.cells.map((cell) => cell.kind)).toEqual(['user_turn', 'active_run']);
    const runCell = model.cells[1];
    expect(runCell.kind).toBe('active_run');
    if (runCell.kind !== 'active_run') throw new Error('expected active run cell');
    expect(runCell.timeline.status).toBe('running');
    expect(runCell.timeline.steps).toHaveLength(1);
    expect(runCell.timeline.steps[0]).toMatchObject({
      kind: 'reasoning',
      status: 'running',
      visibility: 'visible',
    });
  });

  it('does not append a duplicate waiting cell when the transcript already has a live run step', () => {
    const model = buildThreadTimeline({
      messages: [
        { id: 'u1', role: 'user', content: 'Search it.' },
        { id: 't1', role: 'tool_call', content: '', toolName: 'web_search', toolStatus: 'running' },
      ],
      activeSession: { id: 'session-1', title: 'Running search' },
      isWaiting: true,
      isStreaming: false,
      activeRunStatus: 'running',
    });

    expect(model.cells.filter((cell) => cell.kind === 'active_run')).toHaveLength(1);
  });

  it('keeps background worker activity out of the main session header once the handoff answer is rendered', () => {
    const model = buildThreadTimeline({
      messages: [
        { id: 'u1', role: 'user', content: 'Use Agent Team to produce the report.' },
        {
          id: 's1',
          role: 'tool_call',
          content: '',
          toolName: 'spawn_subagent',
          toolStatus: 'done',
          toolMeta: {
            kind: 'runtime_step',
            toolCallId: 'spawn-1',
            stepId: 'spawn-1',
            durationMs: null,
            visibility: 'visible',
            status: 'completed',
          },
        },
        {
          id: 'a1',
          role: 'assistant',
          content: '收到。4 个 subagent 已成功后台启动，完成后我会汇总结果。',
        },
      ],
      activeSession: { id: 'session-1', title: 'Background handoff' },
      isWaiting: false,
      isStreaming: false,
      activeRunStatus: 'running',
    });

    expect(model.header.status).toBe('complete');
    expect(model.header.activeRunStatus).toBeNull();
    expect(model.cells.filter((cell) => cell.kind === 'active_run')).toHaveLength(1);
  });

  it('projects session index facts into the header and inspector state', () => {
    const sessionIndex: SessionIndex = {
      schema: 'session_index.v1',
      thread_id: 'thread-1',
      session_id: 'session-1',
      agent_id: 'agent-1',
      dynamic_tools: [],
      checkpoints: [{ id: 'cp-1', checkpoint_kind: 'user_turn_stop' }],
      active_projection: {
        projection_reason: 'rewind',
        checkpoint_event_id: 'cp-1',
      },
      event_count: 42,
      t0_segments: [{ id: 'seg-1' }, { id: 'seg-2' }],
      resume_health: {
        has_t0_truth: true,
        has_checkpoints: true,
        truth_surface: 'events.jsonl',
      },
    };
    const sessionWorkbench = {
      schema: 'hive.ccplus.session_workbench.v1',
      agent_id: 'agent-1',
      session: {},
      context_window: {
        schema: 'hive.ccplus.context_window.v1',
        decision_count: 2,
        latest_status: {
          active_context_tokens: 50_000,
          auto_compact_scope_limit: 223_000,
          tokens_until_compaction: 173_000,
          cumulative_run_tokens: 1_200_000,
        },
        latest_skipped: {
          reason: 'below_autocompact_threshold',
          active_context_tokens: 50_000,
          cumulative_run_tokens: 1_200_000,
        },
        decisions: [],
      },
      turn: { truth_source: 'transcript', event_count: 42 },
      controls: {},
      runtime_tasks: [],
      goals: [],
      teams: [],
    } as unknown as SessionWorkbench;

    const model = buildThreadTimeline({
      messages: [],
      activeSession: { id: 'session-1', title: 'Recovered session' },
      sessionIndex,
      sessionWorkbench,
      runtimeSummary: {
        activated_tool_groups: [],
        used_tools: [],
        blocked_capabilities: [],
        compaction_count: 2,
        model: { label: 'GPT-5.5', provider: 'openai' },
      },
      branchLineage: [
        { id: 'root', parent_session_id: null },
        { id: 'session-1', parent_session_id: 'root' },
      ],
      isWaiting: false,
      isStreaming: false,
      activeRunStatus: null,
    });

    expect(model.header).toMatchObject({
      title: 'Recovered session',
      modelLabel: 'GPT-5.5',
      resumeHealth: 'events.jsonl + checkpoints',
      activeProjection: 'rewind',
      checkpointCount: 1,
      branchDepth: 1,
      compactionCount: 2,
      contextWindowStatusLabel: 'skipped: below_autocompact_threshold',
    });
    expect(model.header.contextWindowTitle).toContain('active 50.0K tokens');
    expect(model.header.contextWindowTitle).toContain('173.0K tokens until compaction');
    expect(model.header.contextWindowTitle).toContain('latest decision: below_autocompact_threshold');
    expect(model.inspector.sessionEventCount).toBe(42);
    expect(model.inspector.t0SegmentCount).toBe(2);
    expect(model.inspector.latestCheckpointLabel).toBe('user_turn_stop');
  });

  it('projects context usage diagnostics into the context chip when window decisions are absent', () => {
    const sessionWorkbench = {
      schema: 'hive.ccplus.session_workbench.v1',
      agent_id: 'agent-1',
      session: {},
      context_usage: {
        schema: 'hive.ccplus.session_context_usage.v1',
        used_tokens: 96_000,
        free_space_tokens: 32_000,
        model_window_tokens: 128_000,
        counts: {
          context_candidates: 4,
          selected_contexts: 2,
          deferred_tools: 3,
          skills: 1,
        },
      },
      turn: { truth_source: 'transcript', event_count: 3 },
      controls: {},
      runtime_tasks: [],
      goals: [],
      teams: [],
    } as unknown as SessionWorkbench;

    const model = buildThreadTimeline({
      messages: [],
      activeSession: { id: 'session-1', title: 'Context usage' },
      sessionWorkbench,
      isWaiting: false,
      isStreaming: false,
      activeRunStatus: null,
    });

    expect(model.header.contextWindowStatusLabel).toBe('96.0K used');
    expect(model.header.contextWindowTitle).toContain('window 128.0K tokens');
    expect(model.header.contextWindowTitle).toContain('free 32.0K tokens');
    expect(model.header.contextWindowTitle).toContain('4 candidates');
    expect(model.header.contextWindowTitle).toContain('2 selected');
    expect(model.header.contextWindowTitle).toContain('3 deferred tools');
    expect(model.header.contextWindowTitle).toContain('1 skills');
  });

  it('projects background completion wakes into a compact UI model', () => {
    const model = buildCompletionWakeModel({
      completion_wake_summary: {
        total: 3,
        pending: 0,
        running: 1,
        completed: 1,
        failed: 1,
        terminal: 2,
        needs_parent_observation: 2,
      },
      completion_wakes: [
        {
          id: 'team_member:member-1',
          kind: 'team_member',
          label: 'release critic',
          status: 'completed',
          state: 'completed',
          summary: 'team read model summary',
          source: 'agent_team_read_model',
        },
        {
          id: 'runtime_task:subagent-1',
          kind: 'subagent',
          label: 'critic',
          status: 'failed',
          state: 'failed',
          summary: 'critic failed',
          source: 'runtime_task',
        },
        {
          id: 'runtime_task:workflow-1',
          kind: 'workflow',
          label: 'Release checks',
          status: 'running',
          state: 'running',
          summary: '',
          source: 'runtime_task',
        },
      ],
    });

    expect(model.summary).toMatchObject({ total: 3, running: 1, completed: 1, failed: 1 });
    expect(model.items.map((item) => `${item.kind}:${item.label}:${item.state}`)).toEqual([
      'team_member:release critic:completed',
      'subagent:critic:failed',
      'workflow:Release checks:running',
    ]);
    expect(model.items[0].source).toBe('agent_team_read_model');
  });

  it('keeps peer A2A, agent teams, subagents, workflow leaves, background agents, notifications, runs, and raw events in separate runtime sections', () => {
    const model = buildRuntimeSectionsModel({
      runtime_sections: {
        agent_teams: [
          {
            id: 'team-1',
            runtime_kind: 'agent_team',
            label: 'ABS research team',
            status: 'running',
            elapsed_seconds: 125,
            token_count: 4200,
            tool_use_count: 3,
            enterable: true,
            chat_session_id: 'team-session',
            members: [
              {
                id: 'member-1',
                runtime_kind: 'team_member',
                label: 'CLO analyst',
                status: 'completed',
                elapsed_seconds: 65,
                total_tokens: 1800,
                tool_count: 2,
                enterable: true,
                child_session_id: 'member-session',
              },
            ],
          },
        ],
        subagents: [
          {
            id: 'subagent-1',
            runtime_kind: 'subagent',
            label: 'One-shot reviewer',
            status: 'completed',
            enterable: true,
            child_session_id: 'subagent-session',
          },
        ],
        peer_a2a: [
          {
            id: 'a2a-1',
            runtime_kind: 'peer_a2a',
            label: 'Finance digital employee',
            status: 'blocked',
            enterable: true,
            child_session_id: 'a2a-session',
          },
        ],
        workflows: [
          {
            id: 'workflow-1',
            runtime_kind: 'workflow',
            label: 'Dynamic Workflow',
            status: 'running',
            steps: [{ id: 'step-1', label: 'Plan', status: 'completed' }],
            leaf_calls: [
              {
                id: 'leaf-1',
                runtime_kind: 'workflow_leaf',
                label: 'Fetch market data',
                status: 'completed',
                enterable: false,
              },
            ],
          },
        ],
        background: [
          {
            id: 'background-1',
            runtime_kind: 'background_agent',
            label: 'Completion observer',
            status: 'running',
          },
        ],
        notifications: [
          {
            id: 'notification-1',
            runtime_kind: 'notification',
            label: 'Subagent completed',
            status: 'completed',
          },
        ],
        runs: [
          {
            id: 'run-1',
            runtime_kind: 'runtime_task',
            label: 'web chat turn',
            status: 'completed',
          },
        ],
        raw: [
          {
            id: 'raw-1',
            runtime_kind: 'raw_event',
            label: 'runtime_action_completed',
            status: 'completed',
          },
        ],
      },
    });

    expect(model.agentTeams).toHaveLength(1);
    expect(model.agentTeams[0]).toMatchObject({
      id: 'team-1',
      runtimeKind: 'agent_team',
      label: 'ABS research team',
      childSessionId: 'team-session',
      enterable: true,
      metrics: {
        elapsedSeconds: 125,
        elapsedLabel: '2m 5s',
        tokenCount: 4200,
        tokenLabel: '4.2K',
        toolUseCount: 3,
        toolUseLabel: '3',
      },
    });
    expect(model.agentTeams[0].members).toEqual([
      expect.objectContaining({
        id: 'member-1',
        runtimeKind: 'team_member',
        childSessionId: 'member-session',
        enterable: true,
        metrics: expect.objectContaining({
          elapsedLabel: '1m 5s',
          tokenLabel: '1.8K',
          toolUseLabel: '2',
        }),
      }),
    ]);
    expect(model.subagents).toEqual([
      expect.objectContaining({
        id: 'subagent-1',
        runtimeKind: 'subagent',
        childSessionId: 'subagent-session',
        enterable: false,
      }),
    ]);
    expect(model.peerA2A).toEqual([
      expect.objectContaining({
        id: 'a2a-1',
        runtimeKind: 'peer_a2a',
        childSessionId: 'a2a-session',
        enterable: true,
        status: 'blocked',
      }),
    ]);
    expect(model.workflows[0].leafCalls).toEqual([
      expect.objectContaining({
        id: 'leaf-1',
        runtimeKind: 'workflow_leaf',
        enterable: false,
      }),
    ]);
    expect(model.background.map((item) => item.runtimeKind)).toEqual(['background_agent']);
    expect(model.notifications.map((item) => item.runtimeKind)).toEqual(['notification']);
    expect(model.runs.map((item) => item.runtimeKind)).toEqual(['runtime_task']);
    expect(model.raw.map((item) => item.runtimeKind)).toEqual(['raw_event']);
    expect(model.summary.total).toBe(8);
    expect(model.summary.running).toBe(3);
  });

  it('normalizes bare subagent child_session references as continuable but not enterable', () => {
    const model = buildRuntimeSectionsModel({
      runtime_sections: {
        subagents: [
          {
            id: 'subagent-legacy',
            runtime_kind: 'subagent',
            label: 'Legacy child session payload',
            status: 'running',
            child_session: 'child-session-legacy',
          },
        ],
      },
    });

    expect(model.subagents).toEqual([
      expect.objectContaining({
        id: 'subagent-legacy',
        childSessionId: 'child-session-legacy',
        enterable: false,
      }),
    ]);
  });

  it('consumes the canonical backend runtime-section envelope instead of a test-only bare array', () => {
    const model = buildRuntimeSectionsModel({
      runtime_sections: {
        peer_a2a: {
          schema: 'hive.ccplus.runtime_section.v1',
          key: 'peer_a2a',
          count: 1,
          items: [{
            id: 'a2a-live-1',
            runtime_kind: 'peer_a2a',
            label: 'Live peer employee',
            status: 'failed',
            child_session_id: 'a2a-live-session',
            enterable: true,
          }],
        },
        subagents: {
          schema: 'hive.ccplus.runtime_section.v1',
          key: 'subagents',
          count: 1,
          items: [{
            id: 'subagent-live-1',
            runtime_kind: 'subagent',
            label: 'Live one-shot worker',
            status: 'completed',
          }],
        },
      },
    });

    expect(model.peerA2A).toEqual([
      expect.objectContaining({ id: 'a2a-live-1', childSessionId: 'a2a-live-session', enterable: true }),
    ]);
    expect(model.subagents).toEqual([
      expect.objectContaining({ id: 'subagent-live-1', enterable: false }),
    ]);
    expect(model.summary.total).toBe(2);
  });

  it('projects peer A2A separately from Team, Workers, Workflow, and Activity console segments', () => {
    const rightPanel = buildSessionRightPanelModel({
      messages: [],
      sessionWorkbench: {
        runtime_sections: {
          agent_teams: [
            {
              id: 'team-1',
              runtime_kind: 'agent_team',
              label: 'ABS research team',
              status: 'running',
              elapsed_seconds: 125,
              token_count: 4200,
              tool_use_count: 3,
              members: [
                {
                  id: 'member-1',
                  runtime_kind: 'team_member',
                  label: 'CLO analyst',
                  status: 'completed',
                  child_session_id: 'member-session',
                  enterable: true,
                },
              ],
            },
          ],
          subagents: [
            {
              id: 'subagent-1',
              runtime_kind: 'subagent',
              label: 'One-shot reviewer',
              status: 'completed',
              child_session_id: 'subagent-session',
              enterable: true,
            },
          ],
          peer_a2a: [
            {
              id: 'a2a-1',
              runtime_kind: 'peer_a2a',
              label: 'Finance digital employee',
              status: 'blocked',
              child_session_id: 'a2a-session',
              enterable: true,
              summary: 'The target model provider rejected the request.',
            },
          ],
          workflows: [
            {
              id: 'workflow-1',
              runtime_kind: 'workflow',
              label: 'Dynamic Workflow',
              status: 'waiting',
              steps: [{ id: 'step-1', label: 'Gate review', status: 'waiting' }],
            },
          ],
          background: [{ id: 'background-1', runtime_kind: 'background_agent', label: 'Completion observer', status: 'running' }],
          notifications: [{ id: 'notification-1', runtime_kind: 'notification', label: 'Notify user', status: 'completed' }],
          runs: [{ id: 'run-1', runtime_kind: 'runtime_task', label: 'web chat turn', status: 'completed' }],
          raw: [{ id: 'raw-1', runtime_kind: 'raw_event', label: 'runtime_action_completed', status: 'completed' }],
        },
      } as unknown as SessionWorkbench,
    });

    expect(rightPanel.runtimeConsole.segments.map((segment) => [segment.key, segment.count])).toEqual([
      ['team', 1],
      ['a2a', 1],
      ['workers', 1],
      ['workflow', 1],
      ['activity', 3],
    ]);
    expect(rightPanel.runtimeConsole.defaultSegment).toBe('a2a');
    expect(rightPanel.runtimeConsole.summary).toMatchObject({
      state: 'blocked',
      totalCount: 7,
      runningCount: 2,
      waitingCount: 1,
      blockedCount: 1,
      elapsedLabel: '2m 5s',
      tokenLabel: '4.2K',
      toolUseLabel: '3',
    });
    expect(rightPanel.runtimeConsole.team.items[0].members[0]).toMatchObject({
      id: 'member-1',
      enterable: true,
    });
    expect(rightPanel.runtimeConsole.workers.items[0]).toMatchObject({
      id: 'subagent-1',
      childSessionId: 'subagent-session',
      enterable: false,
    });
    expect(rightPanel.runtimeConsole.peerA2A.items[0]).toMatchObject({
      id: 'a2a-1',
      childSessionId: 'a2a-session',
      enterable: true,
      status: 'blocked',
    });
    expect(rightPanel.runtimeConsole.workflow.items[0]).toMatchObject({
      id: 'workflow-1',
      status: 'waiting',
    });
    expect(rightPanel.runtimeConsole.waiters.map((waiter) => [waiter.segment, waiter.label])).toEqual([
      ['a2a', 'Finance digital employee'],
      ['workflow', 'Gate review'],
    ]);
    expect(rightPanel.runtimeConsole.activity.background.map((item) => item.id)).toEqual(['background-1']);
    expect(rightPanel.runtimeConsole.activity.notifications.map((item) => item.id)).toEqual(['notification-1']);
    expect(rightPanel.runtimeConsole.activity.runs.map((item) => item.id)).toEqual(['run-1']);
    expect(rightPanel.runtimeConsole.activity.raw.map((item) => item.id)).toEqual(['raw-1']);
  });

  it('keeps multiple runtime waiters as separate rows across team, workers, and workflows', () => {
    const rightPanel = buildSessionRightPanelModel({
      messages: [],
      sessionWorkbench: {
        runtime_sections: {
          agent_teams: [
            {
              id: 'team-1',
              runtime_kind: 'agent_team',
              label: 'ABS team',
              status: 'running',
              members: [
                {
                  id: 'member-1',
                  runtime_kind: 'team_member',
                  label: 'credit analyst',
                  status: 'awaiting_approval',
                  child_session_id: 'member-session',
                  enterable: true,
                },
              ],
            },
          ],
          subagents: [
            {
              id: 'subagent-1',
              runtime_kind: 'subagent',
              label: 'risk reviewer',
              status: 'awaiting_user_clarification',
              child_session_id: 'subagent-session',
            },
          ],
          workflows: [
            {
              id: 'workflow-1',
              runtime_kind: 'workflow',
              label: 'Report workflow',
              status: 'running',
              steps: [{ id: 'step-1', label: 'Data gate', status: 'gate_waiting' }],
            },
          ],
        },
      } as unknown as SessionWorkbench,
    });

    expect(rightPanel.runtimeConsole.summary).toMatchObject({
      state: 'waiting',
      waitingCount: 3,
    });
    expect(rightPanel.runtimeConsole.waiters.map((waiter) => [waiter.segment, waiter.label, waiter.status])).toEqual([
      ['team', 'credit analyst', 'awaiting_approval'],
      ['workers', 'risk reviewer', 'awaiting_user_clarification'],
      ['workflow', 'Data gate', 'gate_waiting'],
    ]);
  });

  it('deduplicates one workflow projected by both transcript and workbench using the workflow run id', () => {
    const rightPanel = buildSessionRightPanelModel({
      messages: [{
        role: 'event',
        content: 'Workflow running',
        eventType: 'workflow_started',
        eventStatus: 'running',
        eventWorkflowRunId: 'workflow-1',
        eventRuntimeTaskId: 'runtime-task-1',
      }],
      sessionWorkbench: {
        runtime_sections: {
          workflows: [{
            id: 'workflow-1',
            runtime_kind: 'workflow',
            label: 'Release verification',
            status: 'running',
          }],
        },
      } as unknown as SessionWorkbench,
    });

    expect(rightPanel.runtimeConsole.workflow.count).toBe(1);
    expect(rightPanel.runtimeConsole.workflow.items[0].id).toBe('workflow-1');
  });

  it('projects a budget approval blocker as user-readable waiting state', () => {
    const rightPanel = buildSessionRightPanelModel({
      messages: [],
      sessionWorkbench: {
        runtime_sections: {
          subagents: [
            {
              id: 'subagent-budget-wait',
              runtime_kind: 'subagent',
              label: 'Research worker',
              status: 'pending',
              user_blocker: {
                kind: 'runtime_budget_approval',
                status: 'waiting',
                title: '等待运行额度批准',
                reason: '本任务达到公司设置的运行上限，尚未继续执行。',
                next_action: '你可以继续其他工作；管理员批准后本任务会自动恢复。',
                owner: 'company_admin',
                can_continue_other_work: true,
                auto_resume: true,
              },
            },
          ],
        },
      } as unknown as SessionWorkbench,
    });

    expect(rightPanel.runtimeConsole.summary.state).toBe('waiting');
    expect(rightPanel.runtimeConsole.waiters).toHaveLength(1);
    expect(rightPanel.runtimeConsole.waiters[0].userBlocker).toMatchObject({
      title: '等待运行额度批准',
      owner: 'company_admin',
      autoResume: true,
    });
  });

  it('falls back to session runtime events when workbench runtime sections are missing', () => {
    const rightPanel = buildSessionRightPanelModel({
      messages: [
        {
          id: 'evt-subagent-started',
          role: 'event',
          content: 'Subagent regulatory-expert is running in the background.',
          eventType: 'runtime_action_started',
          eventStatus: 'running',
          eventNotificationSource: 'subagent_wake',
          eventRuntimeTaskId: 'run-subagent-1',
          eventChildSessionId: 'child-subagent-1',
        },
        {
          id: 'evt-a2a-blocked',
          role: 'event',
          content: 'Peer delegation was blocked by the target provider.',
          eventType: 'child_session',
          eventStatus: 'blocked',
          eventNotificationSource: 'a2a',
          eventRuntimeTaskId: 'run-a2a-1',
          eventChildSessionId: 'child-a2a-1',
        },
        {
          id: 'evt-workflow-started',
          role: 'event',
          content: 'Dynamic workflow is waiting on a gate.',
          eventType: 'workflow_run',
          eventStatus: 'waiting',
          eventNotificationSource: 'workflow',
          eventWorkflowRunId: 'workflow-1',
        },
        {
          id: 'evt-team-member-started',
          role: 'event',
          content: 'Team member analyst is running.',
          eventType: 'team_member',
          eventStatus: 'running',
          eventNotificationSource: 'team_member',
          eventRuntimeTaskId: 'member-run-1',
          eventChildSessionId: 'member-session-1',
        },
      ] as AgentChatMessage[],
      sessionWorkbench: { runtime_sections: {} } as unknown as SessionWorkbench,
    });

    expect(rightPanel.runtimeConsole.summary).toMatchObject({
      state: 'blocked',
      totalCount: 4,
      runningCount: 2,
      waitingCount: 1,
      blockedCount: 1,
    });
    expect(rightPanel.runtimeConsole.defaultSegment).toBe('a2a');
    expect(rightPanel.runtimeConsole.workers.items).toEqual([
      expect.objectContaining({
        id: 'run-subagent-1',
        runtimeKind: 'subagent',
        childSessionId: 'child-subagent-1',
        enterable: false,
      }),
    ]);
    expect(rightPanel.runtimeConsole.workflow.items).toEqual([
      expect.objectContaining({
        id: 'workflow-1',
        runtimeKind: 'workflow',
        status: 'waiting',
      }),
    ]);
    expect(rightPanel.runtimeConsole.peerA2A.items).toEqual([
      expect.objectContaining({
        id: 'run-a2a-1',
        runtimeKind: 'peer_a2a',
        childSessionId: 'child-a2a-1',
        enterable: true,
      }),
    ]);
    expect(rightPanel.runtimeConsole.team.items).toEqual([
      expect.objectContaining({
        id: 'member-run-1',
        runtimeKind: 'team_member',
        childSessionId: 'member-session-1',
      }),
    ]);
  });

  it('builds named session workbench models for windows, checkpoints, right panel, runtime sections, and workflow run windows', () => {
    const messages: AgentChatMessage[] = [
      {
        role: 'assistant',
        content: 'Delivered current report.',
        artifacts: [
          {
            id: 'artifact-current',
            name: 'current-report.md',
            path: 'workspace/current-report.md',
            previewKind: 'markdown',
            size: 2048,
            runtimeTaskId: 'run-1',
            snapshotHash: 'sha256-current',
          },
          {
            id: 'artifact-historical',
            name: 'old-report.md',
            path: 'workspace/old-report.md',
            previewKind: 'markdown',
            source: 'historical_session',
          },
          {
            id: 'artifact-unattributed',
            name: 'scratch.txt',
            path: 'workspace/scratch.txt',
            previewKind: 'text',
          },
        ],
      },
    ];
    const runtimeSections = buildRuntimeSectionsModel({
      runtime_sections: {
        workflows: [
          {
            id: 'workflow-1',
            runtime_kind: 'workflow',
            label: 'Dynamic Workflow',
            status: 'waiting',
            elapsed_seconds: 90,
            total_tokens: 8000,
            tool_use_count: 5,
            child_session_id: 'workflow-session',
            workflow_controls: {
              gate_status: 'waiting',
              wait_status: 'waiting_for_gate',
              actions: [
                {
                  action: 'approve_gate',
                  enabled: true,
                  run_id: 'workflow-1',
                  step_id: 'approve-send',
                  reason: 'approval required',
                },
                {
                  action: 'reject_gate',
                  enabled: true,
                  run_id: 'workflow-1',
                  step_id: 'approve-send',
                  reason: 'approval required',
                },
              ],
            },
            steps: [{ id: 'step-1', label: 'Gate review', status: 'waiting' }],
            leaf_calls: [{ id: 'leaf-1', label: 'Research leaf', status: 'completed' }],
          },
        ],
        subagents: [
          {
            id: 'subagent-1',
            runtime_kind: 'subagent',
            label: 'Reviewer',
            status: 'running',
            elapsed_seconds: 30,
            token_count: 1200,
            tool_count: 1,
          },
        ],
      },
    });

    const sessionWindow = buildSessionWindowModel({
      id: 'member-session',
      session_kind: 'team_member',
      source_channel: 'agent_team',
      title: 'Research Team / Reviewer',
      transcript_metadata_json: { team_id: 'team-1', member_name: 'Reviewer', member_role: 'audit' },
    }, 'running');
    const peerA2AWindow = buildSessionWindowModel({
      id: 'a2a-session',
      session_kind: 'delegation_run',
      source_channel: 'agent',
      runtime_source: 'delegation',
      title: 'Finance digital employee',
      transcript_metadata_json: { runtime_task_id: 'a2a-task-1' },
    }, 'failed');
    const checkpointNodes = buildCheckpointTimelineNodes({
      checkpoints: [
        { id: 'cp-1', checkpoint_event_id: 'evt-1', checkpoint_kind: 'user_turn_stop' },
        { id: 'cp-2', checkpoint_event_id: 'evt-2', checkpoint_kind: 'assistant_turn_stop', compacted: true },
      ],
    } as unknown as SessionIndex, [{ id: 'branch-1', parent_session_id: 'member-session' }]);
    const rightPanel = buildSessionRightPanelModel({
      messages,
      sessionWorkbench: { runtime_sections: { workflows: runtimeSections.workflows, subagents: runtimeSections.subagents } } as unknown as SessionWorkbench,
      activeSession: { id: 'member-session', title: 'Member session' },
      activeRunStatus: 'running',
    });
    const workflowWindow = buildWorkflowRunWindowModel(runtimeSections.workflows[0]);

    expect(sessionWindow).toMatchObject({
      id: 'member-session',
      kind: 'team_member',
      label: 'Reviewer',
      activeTabLabel: 'Agent: Reviewer',
      tabTone: 'running',
      teamId: 'team-1',
      sessionId: 'member-session',
    });
    expect(peerA2AWindow).toMatchObject({
      id: 'a2a-session',
      kind: 'peer_a2a',
      label: 'Finance digital employee',
      activeTabLabel: 'A2A: Finance digital employee',
      tabTone: 'failed',
      runtimeTaskId: 'a2a-task-1',
    });
    expect(checkpointNodes).toEqual([
      expect.objectContaining({ id: 'evt-1', sequence: 1, state: 'current_head', branchSessionIds: ['branch-1'] }),
      expect.objectContaining({ id: 'evt-2', sequence: 2, state: 'compacted_scope', compacted: true }),
    ]);
    expect(rightPanel.workspaceDocuments.currentSession.items.map((item) => item.name)).toEqual(['current-report.md']);
    expect(rightPanel.workspaceDocuments.historical.items.map((item) => item.name)).toEqual(['old-report.md']);
    expect(rightPanel.workspaceDocuments.unattributed.items.map((item) => item.name)).toEqual(['scratch.txt']);
    expect(rightPanel.runtimeTables.map((section) => section.key)).toEqual([
      'agent_teams',
      'peer_a2a',
      'subagents',
      'workflows',
      'background',
      'notifications',
      'runs',
      'raw',
    ]);
    expect(rightPanel.runtimeMetrics).toMatchObject({
      runningCount: 1,
      totalCount: 2,
      elapsedLabel: '1m 30s',
      tokenLabel: '9.2K',
      toolUseLabel: '6',
    });
    expect(rightPanel.runtimeConsole.defaultSegment).toBe('workflow');
    expect(rightPanel.runtimeConsole.summary).toMatchObject({
      state: 'waiting',
      totalCount: 2,
      runningCount: 1,
      waitingCount: 1,
    });
    expect(rightPanel.runtimeConsole.workflow.items.map((item) => item.id)).toEqual(['workflow-1']);
    expect(rightPanel.runtimeConsole.workers.items).toEqual([
      expect.objectContaining({
        id: 'subagent-1',
        enterable: false,
      }),
    ]);
    expect(workflowWindow).toMatchObject({
      id: 'workflow-1',
      label: 'Dynamic Workflow',
      status: 'waiting',
      breadcrumb: 'Main > Dynamic Workflow',
      metrics: expect.objectContaining({ elapsedLabel: '1m 30s', tokenLabel: '8.0K', toolUseLabel: '5' }),
    });
    expect(workflowWindow.steps).toHaveLength(1);
    expect(workflowWindow.leafCalls).toHaveLength(1);
    expect(workflowWindow.controls).toMatchObject({
      gateStatus: 'waiting',
      waitStatus: 'waiting_for_gate',
      actions: [
        expect.objectContaining({ action: 'approve_gate', stepId: 'approve-send' }),
        expect.objectContaining({ action: 'reject_gate', stepId: 'approve-send' }),
      ],
    });
  });

  it('shows a tool-produced workspace artifact immediately as a file change, not a declared deliverable', () => {
    const rightPanel = buildSessionRightPanelModel({
      messages: [
        {
          role: 'tool_call',
          content: '',
          toolName: 'write_file',
          artifacts: [
            {
              id: 'artifact-live',
              name: 'live-report.md',
              path: 'workspace/live-report.md',
              source: 'workspace_write',
              runtimeTaskId: 'run-live',
              snapshotHash: 'sha-live',
            },
          ],
        },
      ],
      sessionWorkbench: {
        runtime_sections: {
          runs: [{ id: 'run-live', runtime_task_id: 'run-live', runtime_kind: 'runtime_task', status: 'running' }],
        },
      } as unknown as SessionWorkbench,
    });

    expect(rightPanel.workspaceDocuments.currentSession.items).toEqual([]);
    expect(rightPanel.workspaceDocuments.unattributed.items).toEqual([
      expect.objectContaining({ name: 'live-report.md', path: 'workspace/live-report.md' }),
    ]);
  });

  it('keeps the run panel aligned with the canonical active presentation before runtime rows arrive', () => {
    const rightPanel = buildSessionRightPanelModel({
      messages: [{ id: 'user-1', role: 'user', content: 'Start the run.' }],
      activeSession: { id: 'session-1', title: 'Fresh run', status: 'idle' },
      activeRunStatus: null,
      presentationStatus: 'running',
    });

    expect(rightPanel.runtimeConsole.summary).toMatchObject({
      state: 'running',
      totalCount: 1,
      runningCount: 1,
      waitingCount: 0,
    });
    expect(rightPanel.runtimeConsole.activity.runs).toEqual([
      expect.objectContaining({
        id: 'session-1:presentation-run',
        label: 'Fresh run',
        status: 'running',
        runtimeKind: 'session_run',
      }),
    ]);
    expect(rightPanel.sessionWindow).toMatchObject({ status: 'running' });
  });

  it('settles only the stale main-run projection once the final answer is consumable', () => {
    const rightPanel = buildSessionRightPanelModel({
      messages: [
        { id: 'user-1', role: 'user', content: 'Return the marker.' },
        { id: 'answer-1', role: 'assistant', content: 'MARKER' },
      ],
      activeSession: { id: 'session-1', title: 'Completed run', status: 'idle' },
      sessionWorkbench: {
        runtime_sections: {
          runs: [{ id: 'run-1', runtime_kind: 'runtime_task', status: 'running' }],
          subagents: [{ id: 'worker-1', runtime_kind: 'subagent', status: 'running' }],
        },
      },
      activeRunStatus: null,
      presentationStatus: 'complete',
    });

    expect(rightPanel.runtimeConsole.activity.runs).toEqual([
      expect.objectContaining({
        id: 'run-1',
        status: 'completed',
        raw: expect.objectContaining({ status: 'running' }),
      }),
    ]);
    expect(rightPanel.runtimeConsole.workers.items).toEqual([
      expect.objectContaining({ id: 'worker-1', status: 'running' }),
    ]);
    expect(rightPanel.runtimeConsole.summary).toMatchObject({
      state: 'running',
      runningCount: 1,
    });
  });
});

describe('buildWorkspaceDocumentsModel (session deliverables semantics)', () => {
  const artifact = (over: Record<string, unknown>) => ({
    name: 'report.md',
    path: 'workspace/report.md',
    source: 'artifact_delivery',
    ...over,
  });
  const msg = (artifacts: unknown[]) => ({ role: 'assistant', content: '', artifacts }) as never;

  it('collapses repeated deliveries of the same path into one row without making the repeat count user-facing', () => {
    const model = buildWorkspaceDocumentsModel([
      msg([artifact({ id: 'a1', size: 100 })]),
      msg([artifact({ id: 'a2', size: 140 })]),
    ]);
    expect(model.currentSession.items).toHaveLength(1);
    expect(model.currentSession.items[0].revisions).toBe(2);
    expect(model.currentSession.items[0].size).toBe(140); // newest wins
  });

  it('moves artifacts from other tasks runs out of the session deliverables group', () => {
    const sessionRuns = new Set(['run-mine']);
    const model = buildWorkspaceDocumentsModel(
      [
        msg([artifact({ id: 'mine', runtimeTaskId: 'run-mine' })]),
        msg([artifact({ id: 'foreign', path: 'workspace/other.md', name: 'other.md', runtimeTaskId: 'run-other-workflow' })]),
      ],
      sessionRuns,
    );
    expect(model.currentSession.items.map((doc) => doc.path)).toEqual(['workspace/report.md']);
    expect(model.historical.items.map((doc) => doc.path)).toEqual(['workspace/other.md']);
  });

  it('does not promote raw tool workspace writes into current session deliverables', () => {
    const sessionRuns = new Set(['run-mine']);
    const model = buildWorkspaceDocumentsModel(
      [
        {
          role: 'tool_call',
          content: '',
          toolName: 'write_file',
          artifacts: [
            artifact({
              id: 'tool-write',
              runtimeTaskId: 'run-mine',
              source: 'workspace_write',
            }),
          ],
        } as never,
      ],
      sessionRuns,
    );

    expect(model.currentSession.items).toEqual([]);
    expect(model.unattributed.items.map((doc) => doc.path)).toEqual(['workspace/report.md']);
  });

  it('keeps A2A delivery refs in current session deliverables while preserving producer provenance', () => {
    const sessionRuns = new Set(['parent-run']);
    const model = buildWorkspaceDocumentsModel(
      [
        msg([
          artifact({
            id: 'projected-artifact',
            sourceArtifactId: 'child-artifact',
            runtimeTaskId: 'parent-run',
            source: 'a2a_delivery_ref',
            ownerAgentId: 'child-agent',
            sourceAgentId: 'child-agent',
            downloadAgentId: 'child-agent',
            deliveryAgentId: 'parent-agent',
            producerAgentId: 'child-agent',
            sourceSessionId: 'child-session',
            rootSessionId: 'parent-session',
          }),
        ]),
      ],
      sessionRuns,
    );

    expect(model.currentSession.items).toHaveLength(1);
    expect(model.currentSession.items[0].artifact).toMatchObject({
      id: 'projected-artifact',
      sourceArtifactId: 'child-artifact',
      downloadAgentId: 'child-agent',
      deliveryAgentId: 'parent-agent',
      sourceSessionId: 'child-session',
      rootSessionId: 'parent-session',
    });
  });
});

describe('RuntimePhase in the thread projection (§3 seam 3)', () => {
  it('keeps a user-visible disclosure while a typed runtime-only process item is live', () => {
    const model = buildThreadTimeline({
      messages: [
        {
          id: 'accepted-input-1',
          role: 'user',
          content: 'Return the exact marker.',
          timestamp: '2026-08-29T00:00:00Z',
        },
        {
          id: 'result-commit-1',
          role: 'event',
          content: '',
          timestamp: '2026-08-29T00:00:01Z',
          sessionItem: {
            id: 'result-commit-1',
            kind: 'result_commit',
            lifecycle: 'streaming',
            terminal: false,
          },
        },
      ] as AgentChatMessage[],
      isWaiting: false,
      isStreaming: false,
      runtimePhase: 'resuming',
    });

    const openRuns = model.cells.filter((cell) => (
      cell.kind === 'active_run'
      && (cell.timeline.status === 'running' || cell.timeline.status === 'blocked')
    ));
    expect(openRuns).toHaveLength(1);
    const visibleSteps = openRuns.flatMap((cell) => (
      cell.kind === 'active_run'
        ? cell.timeline.steps.filter((step) => step.presentation !== 'external')
        : []
    ));
    expect(visibleSteps).toEqual([
      expect.objectContaining({
        kind: 'reasoning',
        title: 'Thinking',
        status: 'running',
      }),
    ]);
  });

  it('threads the phase onto the pending active-run cell with a phase-aware label', () => {
    const model = buildThreadTimeline({
      messages: [{ role: 'user', content: 'do the thing' }] as any,
      isWaiting: true,
      isStreaming: false,
      runtimePhase: 'queued',
    });
    const run = model.cells.find((cell) => cell.kind === 'active_run') as any;
    expect(run).toBeTruthy();
    expect(run.phase).toBe('queued');
  });

  it('attaches the live phase to the open run cell built from streamed messages', () => {
    const model = buildThreadTimeline({
      messages: [
        { role: 'user', content: 'write the file' },
        { role: 'tool_call', content: '', toolName: 'write_file', toolStatus: 'running' },
      ] as any,
      isWaiting: false,
      isStreaming: true,
      runtimePhase: 'tool_running',
    });
    const run = model.cells.find((cell) => cell.kind === 'active_run') as any;
    expect(run).toBeTruthy();
    expect(run.phase).toBe('tool_running');
  });

  it('does not attach a phase to settled runs', () => {
    const model = buildThreadTimeline({
      messages: [
        { role: 'user', content: 'question' },
        { role: 'tool_call', content: '', toolName: 'read_file', toolStatus: 'done' },
        { role: 'assistant', content: 'Answer.' },
      ] as any,
      isWaiting: false,
      isStreaming: false,
      runtimePhase: 'done',
    });
    const runs = model.cells.filter((cell) => cell.kind === 'active_run') as any[];
    expect(runs.every((run) => run.phase == null)).toBe(true);
  });

  it('derives the header status from parked phases', () => {
    const model = buildThreadTimeline({
      messages: [{ role: 'user', content: 'x' }] as any,
      isWaiting: false,
      isStreaming: false,
      runtimePhase: 'awaiting_approval',
    });
    expect(model.header.status).toBe('waiting');
  });

  it.each(['queued', 'resuming', 'starting', 'thinking', 'tool_running', 'hook_evaluating', 'compacting', 'summarizing', 'continuation_gap'])(
    'never presents the live %s phase as waiting for user input',
    (runtimePhase) => {
      const model = buildThreadTimeline({
        messages: [{ role: 'user', content: 'Run it.' }] as any,
        isWaiting: true,
        isStreaming: false,
        runtimePhase,
      });

      expect(model.header.status).toBe('running');
    },
  );

  it('keeps terminal delivery in progress until the accepted answer is consumable', () => {
    const input = {
      messages: [{ role: 'user', content: 'Return the marker.' }] as AgentChatMessage[],
      isWaiting: false,
      isStreaming: false,
      runtimePhase: 'done',
    };

    expect(buildThreadTimeline(input).header.status).toBe('running');
    expect(buildThreadTimeline({
      ...input,
      messages: [
        ...input.messages,
        { role: 'assistant', content: 'MARKER' },
      ],
    }).header.status).toBe('complete');
  });

  it('keeps a canonical run failure visibly failed after the live run registry clears', () => {
    const scope = {
      level: 'run' as const,
      session_id: 'session-1',
      thread_id: 'session-1',
      turn_id: 'turn-1',
      run_id: 'run-1',
    };
    const model = buildThreadTimeline({
      messages: [
        { id: 'user-1', role: 'user', content: 'Return the marker.' },
        {
          id: 'failure-1',
          role: 'event',
          content: '模型服务当前繁忙，请稍后重试。',
          eventType: 'runtime_failure',
          eventStatus: 'recorded',
          sessionItem: {
            id: 'failure-1',
            kind: 'runtime_failure',
            lifecycle: 'recorded',
            scope,
            terminal: true,
          },
        },
      ] as AgentChatMessage[],
      activeSession: { id: 'session-1', title: 'Failed run' },
      isWaiting: false,
      isStreaming: false,
      activeRunStatus: null,
    });

    expect(model.header.status).toBe('failed');
  });

  it('presents an accepted user turn as running before the run registry catches up', () => {
    const model = buildThreadTimeline({
      messages: [{
        id: 'accepted-input-1',
        role: 'user',
        content: 'Accepted prompt bytes.',
      }] as AgentChatMessage[],
      isWaiting: false,
      isStreaming: false,
      activeRunStatus: null,
    });

    expect(model.header.status).toBe('running');
    expect(model.cells).toEqual([
      expect.objectContaining({ kind: 'user_turn', id: 'accepted-input-1' }),
      expect.objectContaining({
        kind: 'active_run',
        timeline: expect.objectContaining({ status: 'running' }),
      }),
    ]);
    const rightPanel = buildSessionRightPanelModel({
      messages: [{ id: 'accepted-input-1', role: 'user', content: 'Accepted prompt bytes.' }],
      activeSession: { id: 'session-1', title: 'Accepted input' },
      activeRunStatus: null,
      presentationStatus: model.header.status,
    });
    expect(rightPanel.runtimeConsole.summary).toMatchObject({
      state: 'running',
      runningCount: 1,
      waitingCount: 0,
    });
  });

  it('keeps a consumed assistant snapshot visible while the typed terminal answer catches up', () => {
    const marker = 'SESSION-PHASE-FIX-PASS1-20260828-2358-R7';
    const model = buildThreadTimeline({
      messages: [
        { id: 'accepted-input-1', role: 'user', content: 'Return the marker.' },
        {
          id: 'assistant-text-1',
          role: 'assistant',
          content: marker,
          sessionItem: {
            id: 'assistant-text-1',
            kind: 'assistant_text',
            lifecycle: 'completed',
            terminal: true,
          } as AgentChatMessage['sessionItem'],
        },
      ],
      isWaiting: false,
      isStreaming: false,
      activeRunStatus: null,
      runtimePhase: 'done',
    });

    const run = model.cells.find((cell) => cell.kind === 'active_run');
    expect(model.header.status).toBe('running');
    expect(run).toEqual(expect.objectContaining({
      kind: 'active_run',
      timeline: expect.objectContaining({
        status: 'running',
        steps: expect.arrayContaining([
          expect.objectContaining({ kind: 'prose', details: marker }),
        ]),
      }),
    }));
    expect(model.cells.some((cell) => cell.kind === 'assistant_final')).toBe(false);
  });

  it('keeps one disclosure when completed assistant text is followed by terminal bookkeeping for the same live run', () => {
    const marker = 'SESSION-8B1-LIVE-T1-20260829-1522';
    const runScope = {
      level: 'round' as const,
      session_id: 'session-1',
      thread_id: 'session-1',
      turn_id: 'turn-1',
      run_id: 'run-1',
      round_id: 'round-1',
    };
    const model = buildThreadTimeline({
      messages: [
        {
          id: 'accepted-input-1',
          role: 'user',
          content: 'Return the marker.',
          timestamp: '2026-08-29T07:22:03Z',
        },
        {
          id: 'assistant-text-1',
          role: 'assistant',
          content: marker,
          timestamp: '2026-08-29T07:22:18Z',
          sessionItem: {
            id: 'assistant-text-1',
            kind: 'assistant_text',
            lifecycle: 'completed',
            scope: runScope,
            terminal: true,
          } as AgentChatMessage['sessionItem'],
        },
        {
          id: 'human-input-applied-1',
          role: 'event',
          content: '',
          timestamp: '2026-08-29T07:22:19Z',
          eventType: 'human_input',
          eventStatus: 'applied',
          sessionItem: {
            id: 'human-input-1',
            kind: 'human_input',
            lifecycle: 'applied',
            terminal: true,
          } as AgentChatMessage['sessionItem'],
        },
        {
          id: 'provider-call-ledger-1',
          role: 'event',
          content: '',
          timestamp: '2026-08-29T07:22:19Z',
          eventType: 'provider_call_ledger',
        },
      ] as AgentChatMessage[],
      activeSession: { id: 'session-1', title: 'Terminal bookkeeping tail' },
      isWaiting: true,
      isStreaming: false,
      activeRunStatus: 'running',
      activeRunId: 'run-1',
      runtimePhase: 'thinking',
    });

    const runCells = model.cells.filter((cell) => cell.kind === 'active_run');
    expect(runCells).toHaveLength(1);
    expect(runCells[0]).toMatchObject({
      runId: 'run-1',
      timeline: {
        status: 'running',
        steps: expect.arrayContaining([
          expect.objectContaining({ kind: 'prose', details: marker }),
        ]),
      },
    });
    expect(model.cells).not.toContainEqual(expect.objectContaining({ id: 'active-run-pending' }));
    expect(model.header.status).toBe('running');
  });

  it('projects one continuous run process when canonical boundaries split one accepted user turn', () => {
    const marker = 'SESSION-81DCD5-T1-20260829-1539';
    const sessionScope = {
      level: 'session' as const,
      session_id: 'session-1',
      thread_id: 'session-1',
    };
    const turnScope = {
      level: 'turn' as const,
      session_id: 'session-1',
      thread_id: 'session-1',
      turn_id: 'turn-1',
    };
    const runScope = {
      level: 'run' as const,
      session_id: 'session-1',
      thread_id: 'session-1',
      turn_id: 'turn-1',
      run_id: 'run-1',
    };
    const model = buildThreadTimeline({
      messages: [
        {
          id: 'accepted-input-1',
          role: 'user',
          content: 'Return eight short paragraphs.',
          timestamp: '2026-08-29T07:39:36Z',
          sessionItem: {
            id: 'human-input-1',
            kind: 'human_input',
            lifecycle: 'accepted',
            scope: sessionScope,
            terminal: false,
          } as AgentChatMessage['sessionItem'],
        },
        {
          id: 'input-admission-started-1',
          role: 'event',
          content: '',
          timestamp: '2026-08-29T07:39:36.100Z',
          sessionItem: {
            id: 'input-admission-1',
            kind: 'input_admission',
            lifecycle: 'started',
            scope: sessionScope,
            terminal: false,
          } as AgentChatMessage['sessionItem'],
        },
        {
          id: 'hook-completed-1',
          role: 'event',
          content: 'Session start hook completed.',
          timestamp: '2026-08-29T07:39:36.200Z',
          sessionItem: {
            id: 'hook-1',
            kind: 'hook',
            lifecycle: 'completed',
            scope: sessionScope,
            terminal: true,
          } as AgentChatMessage['sessionItem'],
        },
        {
          id: 'turn-accepted-1',
          role: 'event',
          content: '',
          timestamp: '2026-08-29T07:39:36.300Z',
          sessionItem: {
            id: 'turn-1',
            kind: 'turn',
            lifecycle: 'accepted',
            scope: turnScope,
            terminal: false,
          } as AgentChatMessage['sessionItem'],
        },
        {
          id: 'run-queued-1',
          role: 'event',
          content: '',
          timestamp: '2026-08-29T07:39:36.400Z',
          sessionItem: {
            id: 'run-1',
            kind: 'run',
            lifecycle: 'queued',
            scope: runScope,
            terminal: false,
          } as AgentChatMessage['sessionItem'],
        },
        {
          id: 'reasoning-completed-1',
          role: 'assistant',
          content: '',
          thinking: 'Preparing the requested response.',
          timestamp: '2026-08-29T07:39:55Z',
          sessionItem: {
            id: 'reasoning-1',
            kind: 'assistant_reasoning_private',
            lifecycle: 'completed',
            scope: runScope,
            terminal: true,
          } as AgentChatMessage['sessionItem'],
        },
        {
          id: 'assistant-text-completed-1',
          role: 'assistant',
          content: marker,
          timestamp: '2026-08-29T07:39:56.699Z',
          sessionItem: {
            id: 'assistant-text-1',
            kind: 'assistant_text',
            lifecycle: 'completed',
            scope: runScope,
            terminal: true,
          } as AgentChatMessage['sessionItem'],
        },
        {
          id: 'human-input-applied-1',
          role: 'event',
          content: '',
          timestamp: '2026-08-29T07:39:57Z',
          sessionItem: {
            id: 'human-input-1',
            kind: 'human_input',
            lifecycle: 'applied',
            scope: sessionScope,
            terminal: true,
          } as AgentChatMessage['sessionItem'],
        },
        {
          id: 'provider-call-ledger-1',
          role: 'event',
          content: '',
          timestamp: '2026-08-29T07:39:57.100Z',
          eventType: 'provider_call_ledger',
        },
      ] as AgentChatMessage[],
      activeSession: { id: 'session-1', title: 'One turn process projection' },
      isWaiting: true,
      isStreaming: false,
      activeRunStatus: 'running',
      activeRunId: 'run-1',
      runtimePhase: 'thinking',
    });

    const runCells = model.cells.filter((cell) => cell.kind === 'active_run');
    expect(runCells).toHaveLength(1);
    expect(runCells[0]).toMatchObject({
      runId: 'run-1',
      timeline: {
        status: 'running',
        steps: expect.arrayContaining([
          expect.objectContaining({ kind: 'prose', details: marker }),
        ]),
      },
    });
    if (runCells[0]?.kind === 'active_run') {
      expect(runCells[0].sourceMessages.map((entry) => entry.message.id)).toEqual([
        'hook-completed-1',
        'reasoning-completed-1',
        'assistant-text-completed-1',
      ]);
    }
    expect(model.cells.some((cell) => cell.kind === 'boundary')).toBe(true);
    expect(model.cells).not.toContainEqual(expect.objectContaining({ id: 'active-run-pending' }));
    expect(model.header.status).toBe('running');
  });

  it('keeps an unsealed raw stream inside the current canonical process instead of creating a second run card', () => {
    const scope = (turnId: string, runId: string) => ({
      level: 'round' as const,
      session_id: 'session-1',
      thread_id: 'session-1',
      turn_id: turnId,
      run_id: runId,
      round_id: `${runId}:round:1`,
    });
    const model = buildThreadTimeline({
      messages: [
        { id: 'user-1', role: 'user', content: 'Finish the first turn.', timestamp: '2026-08-29T08:01:15Z' },
        {
          id: 'reasoning-1',
          role: 'assistant',
          content: '',
          timestamp: '2026-08-29T08:04:01Z',
          sessionItem: {
            id: 'reasoning-1',
            kind: 'assistant_reasoning_private',
            lifecycle: 'completed',
            scope: scope('turn-1', 'run-1'),
            terminal: true,
          } as AgentChatMessage['sessionItem'],
        },
        {
          id: 'answer-1',
          role: 'assistant',
          content: 'First turn complete.',
          timestamp: '2026-08-29T08:06:48Z',
          sessionItem: {
            id: 'answer-1',
            kind: 'assistant_final',
            lifecycle: 'completed',
            scope: scope('turn-1', 'run-1'),
            terminal: true,
          } as AgentChatMessage['sessionItem'],
        },
        { id: 'user-2', role: 'user', content: 'Stream the second turn.', timestamp: '2026-08-29T08:17:07Z' },
        {
          id: 'reasoning-2',
          role: 'assistant',
          content: '',
          timestamp: '2026-08-29T08:19:55Z',
          sessionItem: {
            id: 'reasoning-2',
            kind: 'assistant_reasoning_private',
            lifecycle: 'completed',
            scope: scope('turn-2', 'run-2'),
            terminal: true,
          } as AgentChatMessage['sessionItem'],
        },
        {
          id: 'assistant-text-2',
          role: 'assistant',
          content: 'Canonical assistant text is complete, but the run is not terminal.',
          timestamp: '2026-08-29T08:19:57.723Z',
          sessionItem: {
            id: 'assistant-text-2',
            kind: 'assistant_text',
            lifecycle: 'completed',
            scope: scope('turn-2', 'run-2'),
            terminal: true,
          } as AgentChatMessage['sessionItem'],
        },
        {
          id: 'raw-stream-2',
          role: 'assistant',
          content: 'Partial raw transport projection without a terminal witness.',
          timestamp: '2026-08-29T08:19:57.724Z',
          _streaming: true,
        } as AgentChatMessage & { _streaming: true },
      ],
      activeSession: { id: 'session-1', title: 'Two-plane streaming ownership' },
      isWaiting: true,
      isStreaming: true,
      activeRunStatus: 'running',
      activeRunId: 'run-2',
      runtimePhase: 'responding',
    });

    const runCells = model.cells.filter((cell) => cell.kind === 'active_run');
    expect(runCells).toHaveLength(2);
    expect(runCells[1]).toMatchObject({
      runId: 'run-2',
      timeline: { status: 'running', answerMessageId: undefined },
    });
    if (runCells[1]?.kind === 'active_run') {
      expect(runCells[1].sourceMessages.map((entry) => entry.message.id)).toEqual([
        'reasoning-2',
        'assistant-text-2',
      ]);
    }
    expect(model.cells.filter((cell) => cell.kind === 'assistant_final')).toHaveLength(1);
    expect(model.cells).not.toContainEqual(expect.objectContaining({ id: 'active-run-pending' }));
  });

  it('includes the phase in the cache signature so live transitions invalidate', () => {
    const cache = {
      previousInputSignature: null,
      previousMessages: null,
      previousModel: null,
    } as any;
    const messages = [{ role: 'user', content: 'x' }] as any;
    const first = buildThreadTimelineCached(
      { messages, isWaiting: true, isStreaming: false, runtimePhase: 'starting' },
      cache,
    );
    const second = buildThreadTimelineCached(
      { messages, isWaiting: true, isStreaming: false, runtimePhase: 'thinking' },
      cache,
    );
    const firstRun = first.cells.find((cell) => cell.kind === 'active_run') as any;
    const secondRun = second.cells.find((cell) => cell.kind === 'active_run') as any;
    expect(firstRun.phase).toBe('starting');
    expect(secondRun.phase).toBe('thinking');
  });
});


describe('DAY1-A2A-LIVE-STATE-001: live A2A runtime state converges without reload', () => {
  it('(a) canonical completed peer A2A task suppresses the stale running fallback for the same runtime_task_id', () => {
    // Production shape: the canonical peer_a2a row carries runtime_task_id
    // and child_session_id but no id; the earlier live delegation_run event
    // keeps a running row alive with the same two durable identities.
    const rightPanel = buildSessionRightPanelModel({
      messages: [
        {
          id: 'evt-a2a-running',
          role: 'event',
          content: 'Worker Agent B is running.',
          eventType: 'delegation_run',
          eventStatus: 'running',
          eventNotificationSource: 'a2a',
          eventRuntimeTaskId: 'delegation-task-1',
          eventChildSessionId: 'child-session-1',
        },
      ] as AgentChatMessage[],
      sessionWorkbench: {
        runtime_sections: {
          peer_a2a: [
            {
              runtime_kind: 'peer_a2a',
              label: 'Worker Agent B',
              status: 'completed',
              child_session_id: 'child-session-1',
              runtime_task_id: 'delegation-task-1',
            },
          ],
        },
      } as unknown as SessionWorkbench,
    });

    expect(rightPanel.runtimeSections.peerA2A).toHaveLength(1);
    expect(rightPanel.runtimeSections.peerA2A[0]).toMatchObject({
      status: 'completed',
      childSessionId: 'child-session-1',
    });
    expect(rightPanel.runtimeSections.summary.running).toBe(0);
    expect(rightPanel.runtimeConsole.summary).toMatchObject({ state: 'idle', runningCount: 0 });
  });

  it('(b1) a canonical completion wake deduplicates the live agent_task_notification for the same runtime_task_id and never creates a Workers row', () => {
    const rightPanel = buildSessionRightPanelModel({
      messages: [
        {
          id: 'evt-a2a-notify',
          role: 'event',
          content: 'Worker Agent B completed: durable result ready.',
          eventType: 'agent_task_notification',
          eventStatus: 'completed',
          eventNotificationSource: 'runtime_result_integration',
          eventRuntimeTaskId: 'delegation-task-1',
          eventChildSessionId: 'child-session-1',
          // The generated thread-item mapping pins subagent_activity; the
          // exact event type must still win over it for section routing.
          threadItem: { item_type: 'subagent_activity' } as AgentChatMessage['threadItem'],
        },
      ] as AgentChatMessage[],
      sessionWorkbench: {
        runtime_sections: {
          notifications: [
            {
              runtime_kind: 'notification',
              label: 'Completion wake',
              status: 'completed',
              runtime_task_id: 'delegation-task-1',
            },
          ],
        },
      } as unknown as SessionWorkbench,
    });

    expect(rightPanel.runtimeSections.subagents).toHaveLength(0);
    expect(rightPanel.runtimeConsole.workers.items).toHaveLength(0);
    expect(rightPanel.runtimeSections.notifications).toHaveLength(1);
    expect(rightPanel.runtimeSections.notifications[0]).toMatchObject({
      label: 'Completion wake',
      status: 'completed',
    });
  });

  it('(b2) a fallback-only agent_task_notification lands in Notifications/Activity and never in Workers', () => {
    const rightPanel = buildSessionRightPanelModel({
      messages: [
        {
          id: 'evt-a2a-notify',
          role: 'event',
          content: 'Worker Agent B completed: durable result ready.',
          eventType: 'agent_task_notification',
          eventStatus: 'completed',
          eventNotificationSource: 'runtime_result_integration',
          eventRuntimeTaskId: 'delegation-task-1',
          eventChildSessionId: 'child-session-1',
          threadItem: { item_type: 'subagent_activity' } as AgentChatMessage['threadItem'],
        },
      ] as AgentChatMessage[],
      sessionWorkbench: { runtime_sections: {} } as unknown as SessionWorkbench,
    });

    expect(rightPanel.runtimeSections.subagents).toHaveLength(0);
    expect(rightPanel.runtimeConsole.workers.items).toHaveLength(0);
    expect(rightPanel.runtimeSections.notifications).toHaveLength(1);
    expect(rightPanel.runtimeSections.notifications[0]).toMatchObject({
      runtimeKind: 'notification',
      status: 'completed',
    });
  });

  it('(c) fallback-only A2A and subagent rows remain usable when canonical runtime sections are absent', () => {
    const rightPanel = buildSessionRightPanelModel({
      messages: [
        {
          id: 'evt-a2a-running',
          role: 'event',
          content: 'Worker Agent B is running.',
          eventType: 'delegation_run',
          eventStatus: 'running',
          eventNotificationSource: 'a2a',
          eventRuntimeTaskId: 'delegation-task-1',
          eventChildSessionId: 'child-session-1',
        },
        {
          id: 'evt-subagent-started',
          role: 'event',
          content: 'Subagent reviewer is running in the background.',
          eventType: 'runtime_action_started',
          eventStatus: 'running',
          eventNotificationSource: 'subagent_wake',
          eventRuntimeTaskId: 'run-subagent-1',
          eventChildSessionId: 'child-subagent-1',
        },
      ] as AgentChatMessage[],
      sessionWorkbench: { runtime_sections: {} } as unknown as SessionWorkbench,
    });

    expect(rightPanel.runtimeSections.peerA2A).toHaveLength(1);
    expect(rightPanel.runtimeSections.peerA2A[0]).toMatchObject({ status: 'running' });
    expect(rightPanel.runtimeSections.subagents).toHaveLength(1);
    expect(rightPanel.runtimeSections.subagents[0]).toMatchObject({ status: 'running' });
    expect(rightPanel.runtimeSections.summary.running).toBe(2);
  });

  it('(d) distinct runtime_task_ids sharing one child_session_id remain distinct rows', () => {
    const rightPanel = buildSessionRightPanelModel({
      messages: [
        {
          id: 'evt-a2a-running-2',
          role: 'event',
          content: 'Worker Agent B is running again.',
          eventType: 'delegation_run',
          eventStatus: 'running',
          eventNotificationSource: 'a2a',
          eventRuntimeTaskId: 'delegation-task-2',
          eventChildSessionId: 'child-session-1',
        },
      ] as AgentChatMessage[],
      sessionWorkbench: {
        runtime_sections: {
          peer_a2a: [
            {
              runtime_kind: 'peer_a2a',
              label: 'Worker Agent B first run',
              status: 'completed',
              child_session_id: 'child-session-1',
              runtime_task_id: 'delegation-task-1',
            },
            {
              runtime_kind: 'peer_a2a',
              label: 'Worker Agent B second run',
              status: 'completed',
              child_session_id: 'child-session-1',
              runtime_task_id: 'delegation-task-2',
            },
          ],
        },
      } as unknown as SessionWorkbench,
    });

    expect(rightPanel.runtimeSections.peerA2A).toHaveLength(2);
    expect(
      rightPanel.runtimeSections.peerA2A.map((item) => String((item.raw as Record<string, unknown>).runtime_task_id)),
    ).toEqual(['delegation-task-1', 'delegation-task-2']);
    expect(rightPanel.runtimeSections.summary.running).toBe(0);
  });
});
