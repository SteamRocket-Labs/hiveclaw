import { describe, expect, it } from 'vitest';

import {
  buildRuntimeSummary,
  computeComposerHeight,
  getRuntimeEventMessage,
  getTransportNotice,
  normalizeStoredChatMessage,
} from './chatRuntime';

describe('chatRuntime helpers', () => {
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

  it('clamps composer height to the configured min and max', () => {
    expect(computeComposerHeight(20)).toBe(44);
    expect(computeComposerHeight(96)).toBe(96);
    expect(computeComposerHeight(260)).toBe(160);
  });

  it('prefers backend runtime estimates and model metadata when available', () => {
    const summary = buildRuntimeSummary({
      persistedSummary: {
        activated_packs: ['web-pack'],
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
