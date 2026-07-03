import { describe, expect, it } from 'vitest';

import {
  buildWorkspaceDocumentsModel,
  buildCheckpointTimelineNodes,
  buildCompletionWakeModel,
  buildRuntimeSectionsModel,
  buildSessionRightPanelModel,
  buildSessionWindowModel,
  buildThreadTimeline,
  buildWorkflowRunWindowModel,
} from './timelineModel';
import type { AgentChatMessage } from '../agent-detail/chatRuntime';
import type { SessionIndex } from '../../api/domains/chat';
import type { SessionWorkbench } from '../../api/domains/ccParity';

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

    expect(model.cells.map((cell) => cell.kind)).toEqual(['user_turn', 'active_run']);
    const runCell = model.cells[1];
    expect(runCell.kind).toBe('active_run');
    if (runCell.kind !== 'active_run') throw new Error('expected active run cell');
    expect(runCell.timeline.steps.map((step) => step.kind)).toEqual(['reasoning']);
    expect(runCell.answer?.content).toContain('point before the selected prompt');
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
    expect(workflowWindow).toMatchObject({
      id: 'workflow-1',
      label: 'Dynamic Workflow',
      status: 'waiting',
      breadcrumb: 'Main > Dynamic Workflow',
      metrics: expect.objectContaining({ elapsedLabel: '1m 30s', tokenLabel: '8.0K', toolUseLabel: '5' }),
    });
    expect(workflowWindow.steps).toHaveLength(1);
    expect(workflowWindow.leafCalls).toHaveLength(1);
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
});
