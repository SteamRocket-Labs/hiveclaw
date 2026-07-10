import { renderToStaticMarkup } from 'react-dom/server';
import { describe, expect, it, vi } from 'vitest';

vi.mock('react-i18next', () => ({
  useTranslation: () => ({
    t: (_key: string, fallback?: string | Record<string, unknown>, options?: Record<string, unknown>) => {
      const value = typeof fallback === 'string' ? fallback : String(options?.defaultValue || _key);
      return value.replace('{{status}}', String(options?.status || '')).replace('{{count}}', String(options?.count || ''));
    },
  }),
}));

import type { ThreadItem, ThreadItemType } from '../../api/domains/threadItems.generated';
import { ThreadItemInspector } from './ThreadItemInspector';
import { ThreadItemRenderer } from './ThreadItemRenderer';

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
  context_compaction: {
    original_message_count: 120,
    kept_message_count: 24,
    continuity_sections_injected: ['Task Ledger'],
  },
  artifact: { artifact_id: 'artifact-1', path: 'report.md', action: 'updated' },
  boundary: { phase: 'cancelled', reason: 'user_stop' },
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

  it('shows the approval subject, arguments, risk, expiry, and action slot', () => {
    const approval = item('approval_request');
    const markup = renderToStaticMarkup(
      <ThreadItemRenderer item={approval} approvalActions={<button type="button">Allow once</button>} />,
    );

    expect(markup).toContain('write_file');
    expect(markup).toContain('report.md');
    expect(markup).toContain('controlled_write');
    expect(markup).toContain('2026-07-10T12:30:00Z');
    expect(markup).toContain('Allow once');
    expect(markup).toContain('waiting_user');
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
});
