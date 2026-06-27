import { describe, expect, it } from 'vitest';

import { buildCompletionWakeModel, buildThreadTimeline } from './timelineModel';
import type { AgentChatMessage } from '../agent-detail/chatRuntime';
import type { SessionIndex } from '../../api/domains/chat';

describe('session workbench timeline model', () => {
  it('keeps a turn run as one cell with reasoning, tool, and final answer parts', () => {
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

    expect(model.cells.map((cell) => cell.kind)).toEqual(['user_turn', 'active_run']);
    const runCell = model.cells[1];
    expect(runCell.kind).toBe('active_run');
    if (runCell.kind !== 'active_run') throw new Error('expected active run cell');
    expect(runCell.timeline.steps.map((step) => step.kind)).toEqual(['reasoning', 'file']);
    expect(runCell.answer?.content).toContain('presentation model');
    expect(model.header.status).toBe('complete');
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

  it('projects session index facts into the header and inspector state', () => {
    const sessionIndex: SessionIndex = {
      schema: 'session_index.v1',
      thread_id: 'thread-1',
      session_id: 'session-1',
      agent_id: 'agent-1',
      dynamic_tools: [],
      checkpoints: [{ id: 'cp-1', checkpoint_kind: 'user_turn_stop' }],
      event_count: 42,
      t0_segments: [{ id: 'seg-1' }, { id: 'seg-2' }],
      resume_health: { status: 'recovered', reason: 'interrupted tool repaired' },
    };

    const model = buildThreadTimeline({
      messages: [],
      activeSession: { id: 'session-1', title: 'Recovered session' },
      sessionIndex,
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
      resumeHealth: 'recovered',
      checkpointCount: 1,
      branchDepth: 1,
      compactionCount: 2,
    });
    expect(model.inspector.sessionEventCount).toBe(42);
    expect(model.inspector.t0SegmentCount).toBe(2);
    expect(model.inspector.latestCheckpointLabel).toBe('user_turn_stop');
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
});
