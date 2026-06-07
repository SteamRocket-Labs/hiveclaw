import { describe, expect, it } from 'vitest';

import {
  CHAT_SOCKET_KEEPALIVE_INTERVAL_MS,
  buildChatSocketKeepaliveMessage,
  buildRuntimeSummary,
  computeComposerHeight,
  getCompactionDisplayContent,
  getRuntimeEventMessage,
  getTransportNotice,
  normalizeStoredChatMessage,
} from './chatRuntime';

describe('chatRuntime helpers', () => {
  it('keeps websocket sessions alive while a user is waiting for output', () => {
    expect(CHAT_SOCKET_KEEPALIVE_INTERVAL_MS).toBeGreaterThan(0);
    expect(CHAT_SOCKET_KEEPALIVE_INTERVAL_MS).toBeLessThanOrEqual(30_000);
    expect(buildChatSocketKeepaliveMessage()).toEqual({ type: 'ping' });
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
    expect(display.visible).toBe('Hide the recovery summary from the default chat transcript while preserving details.');
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
