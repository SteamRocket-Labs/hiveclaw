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
    expect(second.cells[second.cells.length - 1]).toMatchObject({ kind: 'user_turn', id: 'u2' });
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

  it('keeps agent teams, subagents, workflow leaves, background agents, notifications, runs, and raw events in separate runtime sections', () => {
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
    expect(model.summary.total).toBe(7);
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

  it('projects runtime sections into Team, Workers, Workflow, and Activity console segments', () => {
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
      ['workers', 1],
      ['workflow', 1],
      ['activity', 3],
    ]);
    expect(rightPanel.runtimeConsole.defaultSegment).toBe('workflow');
    expect(rightPanel.runtimeConsole.summary).toMatchObject({
      state: 'waiting',
      totalCount: 6,
      runningCount: 2,
      waitingCount: 1,
      blockedCount: 0,
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
    expect(rightPanel.runtimeConsole.workflow.items[0]).toMatchObject({
      id: 'workflow-1',
      status: 'waiting',
    });
    expect(rightPanel.runtimeConsole.waiters.map((waiter) => [waiter.segment, waiter.label])).toEqual([
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
      state: 'waiting',
      totalCount: 3,
      runningCount: 2,
      waitingCount: 1,
    });
    expect(rightPanel.runtimeConsole.defaultSegment).toBe('workflow');
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
    expect(checkpointNodes).toEqual([
      expect.objectContaining({ id: 'evt-1', sequence: 1, state: 'current_head', branchSessionIds: ['branch-1'] }),
      expect.objectContaining({ id: 'evt-2', sequence: 2, state: 'compacted_scope', compacted: true }),
    ]);
    expect(rightPanel.workspaceDocuments.currentSession.items.map((item) => item.name)).toEqual(['current-report.md']);
    expect(rightPanel.workspaceDocuments.historical.items.map((item) => item.name)).toEqual(['old-report.md']);
    expect(rightPanel.workspaceDocuments.unattributed.items.map((item) => item.name)).toEqual(['scratch.txt']);
    expect(rightPanel.runtimeTables.map((section) => section.key)).toEqual([
      'agent_teams',
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
