import { describe, expect, it } from 'vitest';

import {
  buildRunTimelineFromMessages,
  getDisclosureStepSummary,
  isDisclosureStepMessage,
} from './chatDisclosureReducer';
import type { AgentChatMessage } from './chatRuntime';

describe('chatDisclosureReducer', () => {
  it('projects canonical commentary and compaction without treating progress prose as the final answer', () => {
    const messages = [
      {
        role: 'assistant',
        content: 'I found the first root cause and am checking the adjacent path.',
        id: 'commentary-1',
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
        role: 'event',
        content: 'The active working state was preserved.',
        id: 'compaction-1',
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
    ] as AgentChatMessage[];

    expect(isDisclosureStepMessage(messages[1])).toBe(true);

    const timeline = buildRunTimelineFromMessages(messages);

    expect(timeline.steps).toEqual([
      expect.objectContaining({
        kind: 'commentary',
        details: 'I found the first root cause and am checking the adjacent path.',
        visibility: 'visible',
      }),
      expect.objectContaining({ kind: 'compaction', visibility: 'collapsed' }),
    ]);
    expect(timeline.answerMessageId).toBeUndefined();
  });

  it('keeps legacy commentary in the process stream and legacy assistant_message as the final answer', () => {
    const timeline = buildRunTimelineFromMessages([
      {
        id: 'legacy-commentary',
        role: 'assistant',
        content: 'I am checking the older transcript projection.',
        eventType: 'assistant_commentary',
        eventStatus: 'completed',
      },
      {
        id: 'legacy-final',
        role: 'assistant',
        content: 'The historical Session replay is complete.',
        eventType: 'assistant_message',
        eventStatus: 'completed',
      },
    ] as AgentChatMessage[]);

    expect(timeline.steps).toEqual([
      expect.objectContaining({
        id: 'legacy-commentary',
        kind: 'commentary',
        details: 'I am checking the older transcript projection.',
      }),
    ]);
    expect(timeline.status).toBe('done');
    expect(timeline.answerMessageId).toBe('legacy-final');
  });

  it('projects thinking, ordinary tool calls, and compaction events into visible timeline steps', () => {
    const messages: AgentChatMessage[] = [
      { role: 'assistant', content: '', thinking: 'I need to inspect the current code.', timestamp: '2026-06-22T10:00:00Z' },
      {
        role: 'tool_call',
        content: '',
        toolName: 'read_file',
        toolArgs: { path: 'frontend/src/pages/agent-detail/AgentChatSection.tsx' },
        toolStatus: 'done',
        toolResult: 'raw file content that should stay collapsed',
        timestamp: '2026-06-22T10:00:01Z',
      },
      {
        role: 'event',
        content: '',
        eventType: 'session_compact',
        eventTitle: 'Context Compacted',
        timestamp: '2026-06-22T10:00:02Z',
      },
      { role: 'assistant', content: 'Done.', timestamp: '2026-06-22T10:00:03Z' },
    ];

    const timeline = buildRunTimelineFromMessages(messages, { now: new Date('2026-06-22T10:00:04Z') });

    expect(timeline.status).toBe('done');
    expect(timeline.steps.map((step) => [step.kind, step.title, step.status, step.visibility])).toEqual([
      ['reasoning', 'Thinking', 'done', 'collapsed'],
      ['file', 'Read file', 'done', 'collapsed'],
      ['compaction', 'Context Compacted', 'done', 'collapsed'],
    ]);
    expect(timeline.steps[1].summary).toContain('AgentChatSection.tsx');
    expect(timeline.steps[1].summary).not.toContain('path:');
    expect(timeline.answerMessageId).toBe('answer-3');
  });

  it('keeps running tool calls visible and summarizes them without exposing raw result text', () => {
    const message: AgentChatMessage = {
      role: 'tool_call',
      content: '',
      toolName: 'web_search',
      toolArgs: { query: 'Hive chat runtime disclosure' },
      toolStatus: 'running',
    };

    expect(isDisclosureStepMessage(message)).toBe(true);
    expect(getDisclosureStepSummary(message)).toBe('Hive chat runtime disclosure');

    const timeline = buildRunTimelineFromMessages([message], { now: new Date('2026-06-22T10:00:10Z') });

    expect(timeline.status).toBe('running');
    expect(timeline.steps[0]).toMatchObject({
      kind: 'search',
      title: 'Search web',
      status: 'running',
      summary: 'Hive chat runtime disclosure',
    });
    expect(timeline.steps[0].summary).not.toContain('query:');
  });

  it('surfaces compact tool completion results while keeping raw payloads collapsed', () => {
    const timeline = buildRunTimelineFromMessages([
      {
        role: 'tool_call',
        content: '',
        toolName: 'write_file',
        toolArgs: { path: 'workspace/report.md' },
        toolStatus: 'done',
        toolResult: '✅ Written to workspace/report.md (1234 chars)',
        toolRawResult: 'RAW REPORT CONTENT THAT MUST STAY INSIDE DETAILS',
      },
    ]);

    expect(timeline.steps[0]).toMatchObject({
      kind: 'file',
      title: 'Write file',
      status: 'done',
      summary: 'Written to workspace/report.md (1234 chars)',
    });
    expect(timeline.steps[0].summary).not.toContain('RAW REPORT CONTENT');
    expect((timeline.steps[0].details as { rawResult?: unknown }).rawResult).toBe(
      'RAW REPORT CONTENT THAT MUST STAY INSIDE DETAILS',
    );
  });

  it('keeps final-answer reasoning as a first-class visible process step', () => {
    const finalAnswer: AgentChatMessage = {
      role: 'assistant',
      content: 'Report is ready.',
      thinking: 'Verified the delegated artifact and prepared the final handoff.',
      timestamp: '2026-06-29T12:00:00Z',
    };

    expect(isDisclosureStepMessage(finalAnswer)).toBe(true);

    const timeline = buildRunTimelineFromMessages([finalAnswer]);

    expect(timeline.steps).toHaveLength(1);
    expect(timeline.steps[0]).toMatchObject({
      kind: 'reasoning',
      title: 'Thinking',
      status: 'done',
      summary: 'Verified the delegated artifact and prepared the final handoff.',
    });
    expect(timeline.answerMessageId).toBe('answer-0');
  });

  it('keeps canonical private reasoning but leaves response bytes to the final answer surface', () => {
    const messages = [
      {
        role: 'event',
        content: '',
        id: 'reasoning-1',
        eventType: 'assistant_reasoning_private',
        eventStatus: 'completed',
        sessionItem: {
          id: 'reasoning-1', kind: 'assistant_reasoning_private', lifecycle: 'completed', terminal: true,
          visibility: { audience: 'private_provider' }, payload: {}, content: '',
        },
      },
      {
        role: 'assistant',
        content: 'The answer bytes are being composed.',
        id: 'text-1',
        eventType: 'assistant_text',
        eventStatus: 'completed',
        sessionItem: {
          id: 'text-1', kind: 'assistant_text', lifecycle: 'completed', terminal: true,
          visibility: { audience: 'direct_user' }, payload: {}, content: 'The answer bytes are being composed.',
        },
      },
    ] as AgentChatMessage[];

    const timeline = buildRunTimelineFromMessages(messages);

    expect(timeline.steps).toEqual([
      expect.objectContaining({ kind: 'reasoning', title: 'Thinking', summary: 'Provider-private reasoning was used.' }),
      expect.objectContaining({
        kind: 'prose',
        title: 'Assistant update',
        details: 'The answer bytes are being composed.',
        visibility: 'visible',
      }),
    ]);
    expect(timeline.steps.some((step) => step.title === 'Writing response')).toBe(false);
  });

  it('keeps canonical no-phase public text visible without relabeling it as Thinking or commentary', () => {
    const timeline = buildRunTimelineFromMessages([
      {
        role: 'assistant',
        content: 'I found the Session projection gap. Next I am validating live delivery.',
        id: 'assistant-text-1',
        eventType: 'assistant_text',
        eventStatus: 'completed',
        sessionItem: {
          id: 'assistant-text-1',
          kind: 'assistant_text',
          lifecycle: 'completed',
          terminal: true,
        } as AgentChatMessage['sessionItem'],
      },
    ] as AgentChatMessage[]);

    expect(timeline.steps).toEqual([
      expect.objectContaining({
        kind: 'prose',
        title: 'Assistant update',
        details: 'I found the Session projection gap. Next I am validating live delivery.',
      }),
    ]);
    expect(timeline.steps.some((step) => step.title === 'Thinking')).toBe(false);
    expect(timeline.steps.some((step) => step.kind === 'commentary')).toBe(false);
  });

  it('adds a turn-level aggregate summary for repeated tool groups', () => {
    const timeline = buildRunTimelineFromMessages([
      {
        role: 'tool_call',
        content: '',
        toolName: 'read_file',
        toolArgs: { path: 'workspace/a.md' },
        toolStatus: 'done',
      },
      {
        role: 'tool_call',
        content: '',
        toolName: 'read_file',
        toolArgs: { path: 'workspace/b.md' },
        toolStatus: 'done',
      },
      {
        role: 'tool_call',
        content: '',
        toolName: 'web_search',
        toolArgs: { query: 'session ux' },
        toolStatus: 'done',
      },
      {
        role: 'tool_call',
        content: '',
        toolName: 'execute_code',
        toolArgs: { command: 'npm test' },
        toolStatus: 'done',
      },
    ]);

    expect(timeline.summary).toBe('Read 2 files · Searched web 1 time');
  });

  it('summarizes tool discovery without exposing raw select queries', () => {
    const timeline = buildRunTimelineFromMessages([
      {
        role: 'tool_call',
        content: '',
        toolName: 'tool_search',
        toolArgs: { query: 'select:start_workflow' },
        toolStatus: 'done',
      },
    ]);

    expect(timeline.steps[0]).toMatchObject({
      kind: 'tool',
      title: 'Loading tools',
      summary: 'Checking available tools',
    });
    expect(timeline.steps[0].summary).not.toContain('select:start_workflow');
  });

  it('preserves backend step ids, tool call ids, and duration metadata', () => {
    const timeline = buildRunTimelineFromMessages([
      {
        role: 'tool_call',
        content: '',
        id: 'transcript-event-1',
        toolName: 'read_file',
        toolStatus: 'done',
        toolArgs: { path: 'workspace/a.md' },
        toolResult: 'file content',
        timestamp: '2026-06-22T10:00:02.500Z',
        toolMeta: {
          kind: 'runtime_step',
          toolCallId: 'toolu_123',
          stepId: 'tool:toolu_123',
          durationMs: 2500,
          visibility: 'collapsed',
          status: 'done',
        },
      } as AgentChatMessage,
    ]);

    expect(timeline.steps[0]).toMatchObject({
      id: 'tool:toolu_123',
      toolCallId: 'toolu_123',
      durationMs: 2500,
      visibility: 'collapsed',
    });
  });

  it('classifies workflow, subagent, and trigger runtime sources as first-class steps', () => {
    const messages: AgentChatMessage[] = [
      { role: 'tool_call', content: '', toolName: 'start_workflow', toolStatus: 'running' },
      { role: 'tool_call', content: '', toolName: 'spawn_subagent', toolStatus: 'done' },
      { role: 'tool_call', content: '', toolName: 'set_trigger', toolStatus: 'done' },
      { role: 'event', content: 'Schedule created', eventType: 'schedule', eventStatus: 'created', eventScheduleId: 'schedule-1' },
    ] as AgentChatMessage[];

    const timeline = buildRunTimelineFromMessages(messages);

    expect(timeline.steps.map((step) => step.kind)).toEqual([
      'workflow',
      'subagent',
      'trigger',
      'trigger',
    ]);
    expect(timeline.status).toBe('running');
  });

  it('folds only low-risk retrieval tools and surfaces every interactive, mutating, or lifecycle tool', () => {
    const questionMeta = {
      kind: 'user_clarification' as const,
      blocking: true,
      nextAction: 'Answer to continue.',
      questions: [
        {
          question: 'Which scope should I use?',
          header: 'Scope',
          multiSelect: false,
          options: [{ label: 'Current session', description: '' }],
        },
      ],
    };
    const messages = [
      { role: 'tool_call', content: '', toolName: 'tool_search', toolStatus: 'done' },
      { role: 'tool_call', content: '', toolName: 'read_file', toolStatus: 'done' },
      { role: 'tool_call', content: '', toolName: 'grep_search', toolStatus: 'done' },
      { role: 'tool_call', content: '', toolName: 'web_search', toolStatus: 'done' },
      { role: 'tool_call', content: '', toolName: 'load_skill', toolStatus: 'done' },
      { role: 'tool_call', content: '', toolName: 'ask_user_question', toolStatus: 'done', toolMeta: questionMeta },
      { role: 'tool_call', content: '', toolName: 'write_file', toolStatus: 'done' },
      { role: 'tool_call', content: '', toolName: 'run_command', toolStatus: 'done' },
      { role: 'tool_call', content: '', toolName: 'call_mcp_tool', toolStatus: 'done' },
      { role: 'tool_call', content: '', toolName: 'delegate_to_agent', toolStatus: 'done' },
      { role: 'tool_call', content: '', toolName: 'spawn_subagent', toolStatus: 'done' },
      { role: 'tool_call', content: '', toolName: 'check_subagent', toolStatus: 'done' },
      { role: 'tool_call', content: '', toolName: 'start_workflow', toolStatus: 'done' },
      { role: 'tool_call', content: '', toolName: 'set_trigger', toolStatus: 'done' },
      { role: 'tool_call', content: '', toolName: 'future_unknown_tool', toolStatus: 'done' },
    ] as AgentChatMessage[];

    const timeline = buildRunTimelineFromMessages(messages);

    expect(timeline.steps.map((step) => [step.title, step.presentation])).toEqual([
      ['Loading tools', 'tool_history'],
      ['Read file', 'tool_history'],
      ['Search files', 'tool_history'],
      ['Search web', 'tool_history'],
      ['load_skill', 'tool_history'],
      ['ask_user_question', 'external'],
      ['Write file', 'surface'],
      ['Run command', 'surface'],
      ['call_mcp_tool', 'surface'],
      ['A2A step', 'surface'],
      ['Sub-agent step', 'surface'],
      ['Sub-agent step', 'surface'],
      ['Workflow step', 'surface'],
      ['Schedule step', 'surface'],
      ['future_unknown_tool', 'surface'],
    ]);
  });

  it('routes successful Task ledger mutations into recoverable tool history instead of permanent timeline rows', () => {
    const timeline = buildRunTimelineFromMessages([
      {
        role: 'tool_call',
        content: '',
        toolName: 'task_create',
        toolArgs: { subject: 'Inspect the live Session path', activeForm: 'Inspecting the live Session path' },
        toolStatus: 'done',
      },
      {
        role: 'tool_call',
        content: '',
        toolName: 'task_update',
        toolArgs: { task_id: '1', status: 'in_progress', activeForm: 'Fixing the live Session path' },
        toolStatus: 'done',
      },
      {
        role: 'tool_call',
        content: '',
        toolName: 'task_stop',
        toolArgs: { task_id: 'legacy-task' },
        toolStatus: 'done',
      },
    ] as AgentChatMessage[]);

    expect(timeline.steps).toEqual([
      expect.objectContaining({
        title: 'Update tasks',
        summary: 'Inspecting the live Session path',
        presentation: 'tool_history',
      }),
      expect.objectContaining({
        title: 'Update tasks',
        summary: 'Fixing the live Session path',
        presentation: 'tool_history',
      }),
      expect.objectContaining({
        title: 'Update tasks',
        summary: 'Task legacy-task',
        presentation: 'tool_history',
      }),
    ]);
  });

  it('keeps a failed Task ledger mutation surfaced with its recovery evidence', () => {
    const timeline = buildRunTimelineFromMessages([
      {
        role: 'tool_call',
        content: '',
        toolName: 'task_update',
        toolArgs: { task_id: '1', status: 'completed' },
        toolStatus: 'done',
        toolMeta: {
          kind: 'runtime_step',
          toolCallId: 'task-update-failed',
          stepId: 'tool:task-update-failed',
          status: 'failed',
          durationMs: null,
          visibility: 'collapsed',
        },
      },
    ] as AgentChatMessage[]);

    expect(timeline.steps[0]).toMatchObject({
      title: 'Update tasks',
      status: 'failed',
      presentation: 'surface',
    });
  });

  it('promotes a failed retrieval call out of generic tool history for recovery', () => {
    const timeline = buildRunTimelineFromMessages([
      {
        role: 'tool_call',
        content: '',
        toolName: 'read_file',
        toolStatus: 'done',
        toolMeta: {
          kind: 'runtime_step',
          toolCallId: 'tool-read-failed',
          stepId: 'tool:read-failed',
          status: 'failed',
          visibility: 'collapsed',
        },
      } as AgentChatMessage,
    ]);

    expect(timeline.steps[0]).toMatchObject({
      kind: 'file',
      status: 'failed',
      presentation: 'surface',
    });
  });

  it('routes every dedicated interactive tool result to its usable card surface', () => {
    const toolMetas = [
      { kind: 'user_clarification', questions: [], blocking: true, nextAction: null },
      { kind: 'plan_mode_request', reason: 'Plan first', nextAction: null },
      { kind: 'plan_proposal', summary: 'Review this plan', nextAction: null },
      { kind: 'dynamic_workflow_proposal', goal: 'Run a governed workflow', nextAction: null },
      { kind: 'workflow_preview' },
      { kind: 'hr_preview' },
      { kind: 'create_employee_success' },
    ];
    const messages = toolMetas.map((toolMeta, index) => ({
      role: 'tool_call',
      content: '',
      toolName: `interactive_tool_${index}`,
      toolStatus: 'done',
      toolMeta,
    })) as unknown as AgentChatMessage[];

    const timeline = buildRunTimelineFromMessages(messages);

    expect(timeline.steps.map((step) => step.presentation)).toEqual(toolMetas.map(() => 'external'));
  });

  it('keeps lifecycle and artifact events surfaced while compaction stays in the process disclosure', () => {
    const timeline = buildRunTimelineFromMessages([
      { role: 'event', content: '', eventType: 'context_compaction', eventStatus: 'completed' },
      { role: 'event', content: '', eventType: 'tool_group_activation', eventStatus: 'completed' },
      {
        role: 'event',
        content: 'Delegated worker is running.',
        eventType: 'runtime_action_started',
        eventStatus: 'running',
        eventNotificationSource: 'a2a',
      },
      { role: 'event', content: 'file_changes', eventType: 'file_changes', eventStatus: 'info' },
      {
        role: 'event',
        content: 'Permission required.',
        eventType: 'permission',
        eventStatus: 'session_permission_required',
        sessionPermissionRequest: {
          permission_request_id: 'permission-1',
          tool_name: 'delete_file',
          arguments: {},
        },
      },
    ] as AgentChatMessage[]);

    expect(timeline.steps.map((step) => [step.kind, step.presentation])).toEqual([
      ['compaction', 'process'],
      ['tool', 'tool_history'],
      ['a2a', 'surface'],
      ['artifact', 'surface'],
      ['permission', 'external'],
    ]);
  });

  it('keeps A2A delegation separate from CC-style subagent steps', () => {
    const timeline = buildRunTimelineFromMessages([
      { role: 'tool_call', content: '', toolName: 'delegate_to_agent', toolStatus: 'done' },
      { role: 'tool_call', content: '', toolName: 'spawn_subagent', toolStatus: 'done' },
      {
        role: 'event',
        content: 'Researcher completed.',
        eventType: 'child_session',
        eventStatus: 'completed',
        eventReason: 'delegation_completed',
      },
    ] as AgentChatMessage[]);

    expect(timeline.steps.map((step) => [step.kind, step.title])).toEqual([
      ['a2a', 'A2A step'],
      ['subagent', 'Sub-agent step'],
      ['a2a', 'A2A session'],
    ]);
  });

  it('classifies runtime action lifecycle events as visible A2A steps', () => {
    const timeline = buildRunTimelineFromMessages([
      {
        role: 'event',
        content: '已委派给 Web3研究员，后台执行中。',
        eventType: 'runtime_action_started',
        eventStatus: 'running',
        eventNotificationSource: 'a2a',
        eventRuntimeTaskId: 'task-1',
        eventChildSessionId: 'child-1',
      },
    ] as AgentChatMessage[]);

    expect(timeline.status).toBe('running');
    expect(timeline.steps[0]).toMatchObject({
      kind: 'a2a',
      title: 'Action Started',
      status: 'running',
      summary: '已委派给 Web3研究员，后台执行中。',
    });
  });

  it('treats the run as delivered once an assistant summary exists even if background steps are still running', () => {
    const timeline = buildRunTimelineFromMessages([
      {
        role: 'event',
        content: 'Subagent regulatory-expert is running in the background.',
        eventType: 'runtime_action_started',
        eventStatus: 'running',
        eventNotificationSource: 'subagent_wake',
        eventRuntimeTaskId: 'run-subagent-1',
        eventChildSessionId: 'child-subagent-1',
      },
      {
        role: 'assistant',
        content: '收到，4 个 subagent 已成功后台启动。',
        timestamp: '2026-07-03T06:48:00Z',
      },
    ] as AgentChatMessage[]);

    expect(timeline.status).toBe('done');
    expect(timeline.answerMessageId).toBe('answer-1');
    expect(timeline.steps[0]).toMatchObject({
      kind: 'subagent',
      status: 'running',
    });
  });

  it('classifies task notifications by their completion source', () => {
    const timeline = buildRunTimelineFromMessages([
      {
        role: 'event',
        content: 'Workflow run completed.',
        eventType: 'agent_task_notification',
        eventStatus: 'completed',
        eventNotificationSource: 'workflow',
      },
      {
        role: 'event',
        content: 'Subagent completed.',
        eventType: 'agent_task_notification',
        eventStatus: 'completed',
        eventNotificationSource: 'subagent_wake',
      },
      {
        role: 'event',
        content: 'Team member completed.',
        eventType: 'agent_task_notification',
        eventStatus: 'completed',
        eventNotificationSource: 'agent_team',
      },
      {
        role: 'event',
        content: 'Delegated employee completed.',
        eventType: 'agent_task_notification',
        eventStatus: 'completed',
        eventNotificationSource: 'a2a',
      },
    ] as AgentChatMessage[]);

    expect(timeline.steps.map((step) => step.kind)).toEqual([
      'workflow',
      'subagent',
      'agent_team',
      'a2a',
    ]);
  });

  it('keeps file changes as artifact timeline events without turning them into deliverable cards', () => {
    const timeline = buildRunTimelineFromMessages([
      {
        role: 'event',
        content: 'file_changes',
        eventType: 'file_changes',
        eventTitle: 'File Changes',
        eventStatus: 'info',
      },
    ] as AgentChatMessage[]);

    expect(timeline.steps).toHaveLength(1);
    expect(timeline.steps[0]).toMatchObject({
      kind: 'artifact',
      title: 'File Changes',
      status: 'done',
    });
  });

  it('keeps agent team, team member, subagent, and background-agent events as distinct step kinds', () => {
    const timeline = buildRunTimelineFromMessages([
      {
        role: 'event',
        content: 'Team container created.',
        eventType: 'agent_task_notification',
        eventStatus: 'running',
        eventNotificationSource: 'agent_team',
      },
      {
        role: 'event',
        content: 'Team member completed.',
        eventType: 'team_member',
        eventStatus: 'completed',
        eventNotificationSource: 'team_member',
      },
      {
        role: 'event',
        content: 'Subagent completed.',
        eventType: 'agent_task_notification',
        eventStatus: 'completed',
        eventNotificationSource: 'subagent_wake',
      },
      {
        role: 'event',
        content: 'Background completion observer running.',
        eventType: 'runtime_action_started',
        eventStatus: 'running',
        eventNotificationSource: 'background_agent',
      },
    ] as AgentChatMessage[]);

    expect(timeline.steps.map((step) => step.kind)).toEqual([
      'agent_team',
      'team_member',
      'subagent',
      'background_agent',
    ]);
  });
});
