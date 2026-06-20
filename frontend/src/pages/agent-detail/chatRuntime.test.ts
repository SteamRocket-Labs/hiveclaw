import { describe, expect, it } from 'vitest';

import {
  CHAT_SOCKET_KEEPALIVE_INTERVAL_MS,
  buildChatSocketKeepaliveMessage,
  buildRuntimeSummary,
  applyStreamingChunkEvent,
  computeComposerHeight,
  getCompactionDisplayContent,
  getRuntimeEventMessage,
  getTransportNotice,
  applySessionActiveRunState,
  applySessionActiveRunObservedState,
  appendToolCallMessage,
  applyRuntimeDoneEvent,
  extractArtifactParts,
  normalizeStoredChatMessage,
  applyTranscriptEvent,
  createEmptyTranscriptReplayState,
} from './chatRuntime';

describe('chatRuntime helpers', () => {
  it('replays durable transcript events idempotently across history and websocket', () => {
    const initial = createEmptyTranscriptReplayState();
    const event = {
      id: 'evt-1',
      sequence: 10,
      type: 'assistant_message',
      event_type: 'assistant_message',
      actor_type: 'assistant',
      role: 'assistant',
      content: 'Report is ready.',
      parts: [{ type: 'text', text: 'Report is ready.' }],
      created_at: '2026-06-20T12:00:00Z',
    };

    const fromHistory = applyTranscriptEvent(initial, event);
    const fromSocket = applyTranscriptEvent(fromHistory, event);

    expect(fromHistory.messages).toHaveLength(1);
    expect(fromSocket.messages).toEqual(fromHistory.messages);
    expect(fromSocket.ui).toEqual({ isWaiting: false, isStreaming: false });
  });

  it('treats blocking tool-card transcript events as terminal user-awaiting states', () => {
    const state = {
      ...createEmptyTranscriptReplayState(),
      messages: [{ role: 'assistant' as const, content: '', thinking: 'Need more input', _streaming: true } as any],
      ui: { isWaiting: true, isStreaming: true },
    };

    const next = applyTranscriptEvent(state, {
      id: 'evt-tool',
      sequence: 11,
      type: 'tool_result',
      event_type: 'tool_result',
      actor_type: 'agent',
      role: 'tool_call',
      content: JSON.stringify({
        status: 'awaiting_user_clarification',
        blocking: true,
        questions: [{ question: 'Cadence?', options: [{ label: 'Weekly' }] }],
      }),
      metadata: { tool_name: 'ask_user_question' },
      created_at: '2026-06-20T12:00:01Z',
    });

    expect(next.messages).toHaveLength(1);
    expect(next.messages[0]).toMatchObject({
      role: 'tool_call',
      toolName: 'ask_user_question',
      toolMeta: { kind: 'user_clarification', blocking: true },
    });
    expect(next.ui).toEqual({ isWaiting: false, isStreaming: false });
  });

  it('keeps websocket sessions alive while a user is waiting for output', () => {
    expect(CHAT_SOCKET_KEEPALIVE_INTERVAL_MS).toBeGreaterThan(0);
    expect(CHAT_SOCKET_KEEPALIVE_INTERVAL_MS).toBeLessThanOrEqual(30_000);
    expect(buildChatSocketKeepaliveMessage()).toEqual({ type: 'ping' });
  });

  it('clears stale waiting state when the backend reports no active run', () => {
    const result = applySessionActiveRunState(
      { 'agent-1:session-1': { runId: 'run-1', status: 'running' } },
      { 'agent-1:session-1': { isWaiting: true, isStreaming: false } },
      'agent-1:session-1',
      null,
    );

    expect(result.activeRuns).toEqual({});
    expect(result.uiStates).toEqual({});
  });

  it('marks a session waiting when an active run is observed', () => {
    const result = applySessionActiveRunState(
      {},
      {},
      'agent-1:session-1',
      { runId: 'run-1', status: 'running' },
    );

    expect(result.activeRuns).toEqual({ 'agent-1:session-1': { runId: 'run-1', status: 'running' } });
    expect(result.uiStates).toEqual({ 'agent-1:session-1': { isWaiting: true, isStreaming: false } });
  });

  it('does not let active-run polling overwrite an already streaming transcript', () => {
    const result = applySessionActiveRunObservedState(
      {},
      { 'agent-1:session-1': { isWaiting: false, isStreaming: true } },
      'agent-1:session-1',
      { runId: 'run-1', status: 'running' },
    );

    expect(result.activeRuns).toEqual({ 'agent-1:session-1': { runId: 'run-1', status: 'running' } });
    expect(result.uiStates).toEqual({ 'agent-1:session-1': { isWaiting: false, isStreaming: true } });
  });

  it('resets the streaming assistant when a chunk tombstone arrives', () => {
    const current = [{ role: 'assistant' as const, content: 'partial ', _streaming: true } as any];

    const reset = applyStreamingChunkEvent(current, { type: 'chunk', content: '', reset: true });
    const retried = applyStreamingChunkEvent(reset, { type: 'chunk', content: 'partial answer' });

    expect(reset).toEqual([{ role: 'assistant', content: '', _streaming: true }]);
    expect(retried).toEqual([{ role: 'assistant', content: 'partial answer', _streaming: true }]);
  });

  it('maps compaction runtime events into event messages', () => {
    const message = getRuntimeEventMessage({
      type: 'session_compact',
      summary: 'Trimmed older turns and kept the latest working set.',
      title: 'Context Compacted',
      status: 'info',
      original_message_count: 18,
      kept_message_count: 6,
      continuity_sections_injected: ['Current State', 'Pending Work'],
    });

    expect(message).toMatchObject({
      role: 'event',
      eventType: 'session_compact',
      eventTitle: 'Context Compacted',
      eventStatus: 'info',
      content: 'Trimmed older turns and kept the latest working set.',
      originalMessageCount: 18,
      keptMessageCount: 6,
      continuitySectionsInjected: ['Current State', 'Pending Work'],
    });
  });

  it('maps deferred tool discovery deltas into event messages', () => {
    const message = getRuntimeEventMessage({
      type: 'deferred_tools_delta',
      message: 'Discovered deferred tools: web_search',
      status: 'info',
      tool_groups: [{ name: 'web_pack' }],
      trigger_tool: 'tool_search',
    });

    expect(message).toMatchObject({
      role: 'event',
      eventType: 'deferred_tools_delta',
      eventStatus: 'info',
      content: 'Discovered deferred tools: web_search',
      activatedToolGroupCount: 1,
      triggerTool: 'tool_search',
    });
  });

  it('maps enriched permission runtime events into event messages', () => {
    const message = getRuntimeEventMessage({
      type: 'permission',
      message: 'Approval is required before writing to the workspace.',
      title: 'Permission Gate',
      status: 'approval_required',
      tool_name: 'write_file',
      approval_id: 'approval-123',
      security_zone: 'workspace',
      capability: 'filesystem.write',
      approval_required: true,
      reason: 'Repository files will be modified.',
      next_step: 'Open Approvals to approve or reject this action.',
      retryable: true,
      retry_reason: 'auth',
    });

    expect(message).toMatchObject({
      role: 'event',
      eventType: 'permission',
      eventTitle: 'Permission Gate',
      eventStatus: 'approval_required',
      eventToolName: 'write_file',
      eventApprovalId: 'approval-123',
      eventSecurityZone: 'workspace',
      eventCapability: 'filesystem.write',
      eventApprovalRequired: true,
      eventReason: 'Repository files will be modified.',
      eventNextStep: 'Open Approvals to approve or reject this action.',
      eventRetryable: true,
      eventRetryReason: 'auth',
    });
  });

  it('treats websocket info events as transport notices instead of chat messages', () => {
    expect(
      getTransportNotice({
        type: 'info',
        content: 'Connection closed due to inactivity. Reconnect to continue.',
      }),
    ).toBe('Connection closed due to inactivity. Reconnect to continue.');
    expect(getRuntimeEventMessage({ type: 'info', content: 'ignored' })).toBeNull();
  });

  it('preserves stored event metadata from history payloads', () => {
    const message = normalizeStoredChatMessage({
      role: 'event',
      content: 'Context window compacted.',
      created_at: '2026-04-02T10:00:00Z',
      eventType: 'session_compact',
      eventTitle: 'Context Compacted',
      eventStatus: 'info',
      parts: [
        {
          type: 'event',
          original_message_count: 32,
          kept_message_count: 8,
        },
      ],
    });

    expect(message).toMatchObject({
      role: 'event',
      eventType: 'session_compact',
      eventTitle: 'Context Compacted',
      originalMessageCount: 32,
      keptMessageCount: 8,
      timestamp: '2026-04-02T10:00:00Z',
    });
  });

  it('extracts artifact parts from persisted assistant payloads', () => {
    const artifacts = extractArtifactParts({
      role: 'assistant',
      content: 'Report is ready.',
      artifacts: [
        {
          id: 'artifact-root',
          type: 'artifact',
          name: 'root-report.md',
          path: 'workspace/root-report.md',
          preview_kind: 'markdown',
        },
      ],
      parts: [
        { type: 'text', text: 'Report is ready.' },
        {
          type: 'artifact',
          artifact_id: 'artifact-part',
          name: 'report.md',
          path: 'workspace/report.md',
          preview_kind: 'markdown',
          source: 'workspace_write',
        },
      ],
    });

    expect(artifacts).toEqual([
      {
        id: 'artifact-root',
        name: 'root-report.md',
        path: 'workspace/root-report.md',
        previewKind: 'markdown',
        source: undefined,
        mimeType: undefined,
        size: undefined,
      },
      {
        id: 'artifact-part',
        name: 'report.md',
        path: 'workspace/report.md',
        previewKind: 'markdown',
        source: 'workspace_write',
        mimeType: undefined,
        size: undefined,
      },
    ]);

    expect(normalizeStoredChatMessage({
      role: 'assistant',
      content: 'Report is ready.',
      parts: [
        {
          type: 'artifact',
          id: 'artifact-part',
          name: 'report.md',
          path: 'workspace/report.md',
          preview_kind: 'markdown',
        },
      ],
    })).toMatchObject({
      role: 'assistant',
      artifacts: [
        {
          id: 'artifact-part',
          name: 'report.md',
          path: 'workspace/report.md',
          previewKind: 'markdown',
        },
      ],
    });
  });

  it('rebuilds structured tool cards from persisted tool_call history', () => {
    const message = normalizeStoredChatMessage({
      id: 'tool-1',
      role: 'tool_call',
      content: '',
      toolName: 'ask_user_question',
      toolStatus: 'done',
      toolResult: JSON.stringify({
        status: 'awaiting_user_clarification',
        blocking: true,
        questions: [
          {
            question: 'Which cadence should this employee use?',
            header: 'Cadence',
            options: [{ label: 'Weekly', description: 'Run every week' }],
          },
        ],
      }),
      created_at: '2026-06-20T12:00:00Z',
    });

    expect(message).toMatchObject({
      id: 'tool-1',
      role: 'tool_call',
      toolName: 'ask_user_question',
      toolMeta: {
        kind: 'user_clarification',
        blocking: true,
        questions: [
          {
            question: 'Which cadence should this employee use?',
            header: 'Cadence',
            options: [{ label: 'Weekly', description: 'Run every week' }],
          },
        ],
      },
    });
  });

  it('does not append an empty assistant bubble for terminal tool-card done events', () => {
    const current = [
      {
        role: 'tool_call' as const,
        content: '',
        toolName: 'ask_user_question',
        toolStatus: 'done' as const,
        toolResult: '{"status":"awaiting_user_clarification","questions":[{"question":"Scope?","options":[{"label":"A"}]}]}',
      },
    ];

    expect(applyRuntimeDoneEvent(current, { type: 'done', content: '' })).toBe(current);
  });

  it('removes dangling thinking placeholders when a terminal tool card arrives', () => {
    const current = [
      { role: 'assistant' as const, content: '', thinking: 'Need a cadence.', _streaming: true } as any,
    ];
    const toolMessage = normalizeStoredChatMessage({
      role: 'tool_call',
      toolName: 'ask_user_question',
      toolStatus: 'done',
      toolResult: JSON.stringify({
        status: 'awaiting_user_clarification',
        questions: [{ question: 'Cadence?', options: [{ label: 'Weekly' }] }],
      }),
    });

    expect(appendToolCallMessage(current, toolMessage)).toEqual([toolMessage]);
  });

  it('normalizes persisted system compaction events from JSON content', () => {
    const message = normalizeStoredChatMessage({
      role: 'system',
      content: JSON.stringify({
        type: 'session_compact',
        summary: 'Compacted older turns and kept the active work context.',
        original_message_count: 42,
        kept_message_count: 9,
        continuity_sections_injected: ['Current Work'],
      }),
      created_at: '2026-05-18T08:00:00Z',
    });

    expect(message).toMatchObject({
      role: 'event',
      eventType: 'session_compact',
      eventTitle: 'Context Compacted',
      content: 'Compacted older turns and kept the active work context.',
      originalMessageCount: 42,
      keptMessageCount: 9,
      continuitySectionsInjected: ['Current Work'],
      timestamp: '2026-05-18T08:00:00Z',
    });
  });

  it('normalizes legacy assistant recovery summaries into compaction events', () => {
    const message = normalizeStoredChatMessage({
      role: 'assistant',
      content: [
        '**Primary Request and Intent:** Repair the web chat after context compression.',
        '**Tool Outcomes:** Search ran successfully.',
        '**Current Work:** Hide the recovery summary from the default chat transcript.',
        '**Recovery Context:** Raw session log available at logs/ for full detail',
      ].join('\n'),
      created_at: '2026-05-18T08:05:00Z',
    });

    expect(message).toMatchObject({
      role: 'event',
      eventType: 'session_compact',
      eventTitle: 'Context Compacted',
      content: expect.stringContaining('Hide the recovery summary'),
      timestamp: '2026-05-18T08:05:00Z',
    });
  });

  it('keeps raw compaction summaries out of the default visible event body', () => {
    const display = getCompactionDisplayContent([
      '**Primary Request and Intent:** Repair the web chat after context compression.',
      '**Tool Outcomes:** Search ran successfully.',
      '**Current Work:** Hide the recovery summary from the default chat transcript while preserving details.',
      '**Recovery Context:** Raw session log available at logs/ for full detail',
    ].join('\n'));

    expect(display.compacted).toBe(true);
    expect(display.visible).toBe('');
    expect(display.visible).not.toContain('Recovery Context');
    expect(display.details).toContain('Recovery Context');
  });

  it('clamps composer height to the configured min and max', () => {
    expect(computeComposerHeight(20)).toBe(44);
    expect(computeComposerHeight(96)).toBe(96);
    expect(computeComposerHeight(260)).toBe(160);
  });

  it('prefers backend runtime estimates and model metadata when available', () => {
    const summary = buildRuntimeSummary({
      persistedSummary: {
        activated_tool_groups: ['web-runtime'],
        used_tools: ['search_query'],
        blocked_capabilities: [],
        compaction_count: 1,
        permission_event_count: 2,
        team_memory_hit_count: 3,
        last_tool_budget_event: {
          reason: 'pre_compaction_truncation',
          tool_name: 'web_fetch',
          created_at: '2026-04-02T10:05:00Z',
        },
        last_retry_reason: 'auth',
        last_team_memory_hit: {
          workspace_key: 'workspace-alpha',
          query: 'deploy checklist',
          matched_keys: ['deploy-playbook'],
          created_at: '2026-04-02T10:00:00Z',
        },
        model: {
          label: 'Claude Sonnet',
          provider: 'anthropic',
          name: 'claude-sonnet-4',
          context_window_tokens: 200000,
        },
        runtime: {
          connected: true,
          estimated_input_tokens: 4200,
          remaining_tokens_estimate: 195800,
        },
      },
      activeModel: {
        label: 'GPT-5.4',
        provider: 'openai',
        model: 'gpt-5.4',
        max_input_tokens: 128000,
      },
      agentPrimaryModelId: 'fallback-model',
      agentContextWindowSize: 32000,
      messages: [{ role: 'user', content: 'hello world' }],
      connected: false,
    });

    expect(summary.model).toMatchObject({
      label: 'Claude Sonnet',
      provider: 'anthropic',
      name: 'claude-sonnet-4',
      context_window_tokens: 200000,
    });
    expect(summary.runtime).toMatchObject({
      connected: true,
      estimated_input_tokens: 4200,
      remaining_tokens_estimate: 195800,
    });
    expect(summary.activated_tool_groups).toEqual(['web-runtime']);
    expect('activated_packs' in summary).toBe(false);
    expect(summary.permission_event_count).toBe(2);
    expect(summary.team_memory_hit_count).toBe(3);
    expect(summary.last_tool_budget_event).toMatchObject({
      reason: 'pre_compaction_truncation',
      tool_name: 'web_fetch',
    });
    expect(summary.last_retry_reason).toBe('auth');
    expect(summary.last_team_memory_hit).toMatchObject({
      workspace_key: 'workspace-alpha',
      matched_keys: ['deploy-playbook'],
    });
  });
});
