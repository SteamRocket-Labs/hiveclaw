import { renderToStaticMarkup } from 'react-dom/server';
import { describe, expect, it, vi } from 'vitest';

vi.mock('react-i18next', () => ({
  useTranslation: () => ({
    t: (_key: string, fallback?: string | Record<string, unknown>, options?: Record<string, unknown>) => {
      if (_key === 'sessionWorkbench.threadItem.failure.quotaExhausted') {
        return '模型额度或余额不足，请联系管理员检查额度，或切换模型后重试。';
      }
      const value = typeof fallback === 'string' ? fallback : String(options?.defaultValue || _key);
      return value.replace('{{status}}', String(options?.status || '')).replace('{{count}}', String(options?.count || ''));
    },
  }),
}));

import type { ThreadItem, ThreadItemType } from '../../api/domains/threadItems.generated';
import { ThreadItemInspector } from './ThreadItemInspector';
import { shouldRenderThreadItemInConversation, ThreadItemRenderer } from './ThreadItemRenderer';

const DATA_BY_TYPE: Record<ThreadItemType, Record<string, unknown>> = {
  user_message: {},
  agent_message: {},
  reasoning: {},
  tool_call: { tool_name: 'read_file', arguments: { path: 'a.md' } },
  tool_result: { event_type: 'tool_result', tool_name: 'read_file', success: true },
  approval_request: {
    permission_request_id: 'permission-7',
    tool_name: 'write_file',
    arguments: { path: 'report.md' },
    permission_mode: 'default',
    risk_class: 'controlled_write',
    expires_at: '2026-07-10T12:30:00Z',
    allow_session_allowed: false,
    destructive: false,
  },
  approval_decision: { permission_request_id: 'permission-7', action: 'allow_once' },
  plan: { plan_id: 'plan-1', phase: 'confirmed' },
  workflow_activity: { workflow_run_id: 'workflow-1', label: 'Review' },
  subagent_activity: { runtime_task_id: 'task-1', target_agent_name: 'researcher' },
  agent_team_activity: { runtime_task_id: 'task-team', member_name: 'reviewer' },
  peer_a2a_activity: { runtime_task_id: 'task-a2a', target_agent_name: 'researcher', read_only: true },
  context_compaction: {
    original_message_count: 120,
    kept_message_count: 24,
    continuity_sections_injected: ['Task Ledger'],
  },
  artifact: { artifact_id: 'artifact-1', path: 'report.md', action: 'updated' },
  boundary: { phase: 'cancelled', reason: 'user_stop' },
  warning: { code: 'semantic_retrieval_unavailable', reason: 'Partial context', retryable: true },
  error: { code: 'provider_timeout', reason: 'Timed out', retryable: true },
  event: { event_type: 'hook_progress', title: 'Hook' },
};

function item(itemType: ThreadItemType): ThreadItem {
  return {
    schema: 'hive.thread_item.v1',
    schema_version: 1,
    id: `item-${itemType}`,
    sequence: 1,
    item_type: itemType,
    item_status: itemType === 'approval_request' ? 'waiting_user' : 'succeeded',
    actor_type: 'system',
    event_type: itemType,
    type: itemType,
    role: 'system',
    visibility_scope: 'direct_user',
    listed_surface: 'chat',
    content: `${itemType} content`,
    parts: [],
    metadata: {},
    evidence_refs: [{ kind: 'transcript_event', id: `item-${itemType}` }],
    item_data: DATA_BY_TYPE[itemType],
    audience: 'operator',
    user_summary: `${itemType} summary`,
    user_action: null,
    operator_details: {
      item_data: DATA_BY_TYPE[itemType],
      metadata: {},
      evidence_refs: [{ kind: 'transcript_event', id: `item-${itemType}` }],
      links: {},
    },
  } as ThreadItem;
}

describe('ThreadItemRenderer', () => {
  it('renders every discriminated variant with an explicit technical-details control', () => {
    for (const itemType of Object.keys(DATA_BY_TYPE) as ThreadItemType[]) {
      const markup = renderToStaticMarkup(<ThreadItemRenderer item={item(itemType)} onSelect={() => undefined} />);
      expect(markup).toContain(`data-thread-item-type="${itemType}"`);
      expect(markup).toContain('data-thread-item-status=');
      expect(markup).toContain('data-testid="thread-item-technical-details"');
      expect(markup).not.toContain('role="button"');
      expect(markup).not.toContain('tabindex="0"');
    }
  });

  it('renders the runtime_failure error card with the safe humanized quota message (Codex finding 1)', () => {
    // The canonical thread item produced by the session-event consumer for a
    // run-scoped runtime_failure: typed failure fields only, no assistant
    // message, no natural-language scanning.
    const failure: ThreadItem = {
      ...item('error'),
      id: 'failure-item-1',
      item_type: 'error',
      item_status: 'failed',
      event_type: 'runtime_failure',
      content: '[LLM Error] AI 模型额度或余额不足，请联系管理员检查账户余额、模型额度或切换模型。',
      user_summary: 'Model quota or balance is insufficient. Ask an administrator to check quota, or switch models and retry.',
      audience: 'user',
      item_data: { code: 'quota_exhausted', reason: 'provider_error', retryable: true, retry_reason: null },
    } as ThreadItem;

    expect(shouldRenderThreadItemInConversation(failure, false)).toBe(true);
    const markup = renderToStaticMarkup(<ThreadItemRenderer item={failure} onSelect={() => undefined} />);
    expect(markup).toContain('data-thread-item-type="error"');
    expect(markup).toContain('额度或余额不足');
    expect(markup).not.toContain('Model quota or balance');
    expect(markup).not.toContain('[LLM Error]');
    expect(markup).not.toContain('#1');
  });

  it('keeps non-blocking memory degradation out of the conversation while retaining it for operators', () => {
    const warning = {
      ...item('warning'),
      event_type: 'memory_context_degraded',
      audience: 'user',
      operator_details: null,
      user_action: null,
    } as ThreadItem;

    expect(shouldRenderThreadItemInConversation(warning, false)).toBe(false);
    expect(shouldRenderThreadItemInConversation({ ...warning, audience: 'operator' } as ThreadItem, true)).toBe(true);
  });

  it('shows the approval subject, safe impact, expiry, and action slot without raw governance data', () => {
    const approval = item('approval_request');
    const markup = renderToStaticMarkup(
      <ThreadItemRenderer item={approval} approvalActions={<button type="button">Allow once</button>} />,
    );

    expect(markup).toContain('write_file');
    expect(markup).toContain('2026-07-10T12:30:00Z');
    expect(markup).toContain('Allow once');
    expect(markup).toContain('waiting_user');
    expect(markup).not.toContain('report.md');
    expect(markup).not.toContain('controlled_write');
    expect(markup).not.toContain('permission-7');
  });

  it('exposes selected evidence identifiers and typed detail in the inspector', () => {
    const approval = item('approval_request');
    const markup = renderToStaticMarkup(<ThreadItemInspector item={approval} onClose={() => undefined} />);

    expect(markup).toContain('data-testid="thread-item-inspector"');
    expect(markup).toContain('item-approval_request');
    expect(markup).toContain('permission-7');
    expect(markup).toContain('hive.thread_item.v1');
    expect(markup).toContain('transcript_event');
    expect(markup).toContain('aria-label=');
    expect(markup).not.toContain('<details open=""');
  });

  it('renders only user summary/action and never offers the technical inspector on user projection', () => {
    const approval = {
      ...item('approval_request'),
      audience: 'user',
      user_summary: '需要你的确认：Write final report',
      user_action: {
        kind: 'resolve_approval',
        token: 'permission-7',
        label: '确认后继续',
        impact: '可撤销或只读操作',
        details: [{ label: 'path', value: 'reports/final.md' }],
      },
      operator_details: null,
    } as ThreadItem;
    const markup = renderToStaticMarkup(
      <ThreadItemRenderer item={approval} onSelect={() => undefined} approvalActions={<button>Allow once</button>} />,
    );

    expect(markup).toContain('需要你的确认：Write final report');
    expect(markup).toContain('reports/final.md');
    expect(markup).toContain('可撤销或只读操作');
    expect(markup).toContain('Allow once');
    expect(markup).not.toContain('permission-7');
    expect(markup).not.toContain('controlled_write');
    expect(markup).not.toContain('data-testid="thread-item-technical-details"');
  });

  it('does not render an inspector payload for a user projection', () => {
    const userItem = { ...item('error'), audience: 'user', operator_details: null } as ThreadItem;
    const markup = renderToStaticMarkup(<ThreadItemInspector item={userItem} />);

    expect(markup).toContain('Technical details are available only in Operator View.');
    expect(markup).not.toContain('provider_timeout');
    expect(markup).not.toContain('item-error');
  });

  it('keeps runtime mechanics in the status surface while preserving user actions in conversation', () => {
    const userItem = (itemType: ThreadItemType) => ({
      ...item(itemType),
      audience: 'user',
      operator_details: null,
    } as ThreadItem);

    expect(shouldRenderThreadItemInConversation(userItem('tool_call'), false)).toBe(false);
    expect(shouldRenderThreadItemInConversation(userItem('workflow_activity'), false)).toBe(false);
    expect(shouldRenderThreadItemInConversation(userItem('context_compaction'), false)).toBe(false);
    expect(shouldRenderThreadItemInConversation(userItem('event'), false)).toBe(false);
    expect(shouldRenderThreadItemInConversation(userItem('artifact'), false)).toBe(false);
    expect(shouldRenderThreadItemInConversation(userItem('approval_request'), false)).toBe(true);
    expect(shouldRenderThreadItemInConversation(userItem('warning'), false)).toBe(true);
    expect(shouldRenderThreadItemInConversation(userItem('error'), false)).toBe(true);
    expect(shouldRenderThreadItemInConversation(userItem('plan'), false)).toBe(true);
    expect(shouldRenderThreadItemInConversation(item('event'), true)).toBe(true);
  });
});
