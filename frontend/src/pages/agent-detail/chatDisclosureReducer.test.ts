import { describe, expect, it } from 'vitest';

import {
  buildRunTimelineFromMessages,
  getDisclosureStepSummary,
  isDisclosureStepMessage,
} from './chatDisclosureReducer';
import type { AgentChatMessage } from './chatRuntime';

describe('chatDisclosureReducer', () => {
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

    expect(timeline.summary).toBe('Read 2 files · Searched web 1 time · Ran 1 command');
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
      summary: '已委派给 Web3研究员，后台执行中。 · child:child-1 · run:task-1',
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
      'subagent',
      'a2a',
    ]);
  });
});
