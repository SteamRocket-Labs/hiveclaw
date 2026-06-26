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
      ['file', 'read_file', 'done', 'collapsed'],
      ['compaction', 'Context Compacted', 'done', 'collapsed'],
    ]);
    expect(timeline.steps[1].summary).toContain('path: frontend/src/pages/agent-detail/AgentChatSection.tsx');
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
    expect(getDisclosureStepSummary(message)).toBe('query: Hive chat runtime disclosure');

    const timeline = buildRunTimelineFromMessages([message], { now: new Date('2026-06-22T10:00:10Z') });

    expect(timeline.status).toBe('running');
    expect(timeline.steps[0]).toMatchObject({
      kind: 'search',
      title: 'web_search',
      status: 'running',
      summary: 'query: Hive chat runtime disclosure',
    });
  });

  it('summarizes tool discovery without exposing raw select queries', () => {
    const timeline = buildRunTimelineFromMessages([
      {
        role: 'tool_call',
        content: '',
        toolName: 'tool_search',
        toolArgs: { query: 'select:deep_research_run' },
        toolStatus: 'done',
      },
    ]);

    expect(timeline.steps[0]).toMatchObject({
      kind: 'tool',
      title: 'Loading tools',
      summary: 'Checking available tools',
    });
    expect(timeline.steps[0].summary).not.toContain('select:deep_research_run');
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

  it('classifies workflow, subagent, trigger, and deep research runtime sources as first-class steps', () => {
    const messages: AgentChatMessage[] = [
      { role: 'tool_call', content: '', toolName: 'start_workflow', toolStatus: 'running' },
      { role: 'tool_call', content: '', toolName: 'spawn_subagent', toolStatus: 'done' },
      { role: 'tool_call', content: '', toolName: 'set_trigger', toolStatus: 'done' },
      { role: 'event', content: 'Schedule created', eventType: 'schedule', eventStatus: 'created', eventScheduleId: 'schedule-1' },
      {
        role: 'tool_call',
        content: '',
        toolName: 'deep_research_start',
        toolStatus: 'done',
        toolMeta: { kind: 'deep_research', status: 'running', taskId: 'research-1', qualityGates: {}, gaps: [] },
      },
    ] as AgentChatMessage[];

    const timeline = buildRunTimelineFromMessages(messages);

    expect(timeline.steps.map((step) => step.kind)).toEqual([
      'workflow',
      'subagent',
      'trigger',
      'trigger',
      'deep_research',
    ]);
    expect(timeline.status).toBe('running');
  });
});
