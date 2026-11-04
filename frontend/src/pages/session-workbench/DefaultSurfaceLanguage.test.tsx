// @vitest-environment jsdom
/**
 * B2: the default screen speaks the member's language and shows only what
 * they sent. Internal vocabulary (plan 2.3 + production findings 2026-11-04)
 * must not appear in the default timeline's zh copy or its rendered markup.
 */
import React from 'react';
import { renderToStaticMarkup } from 'react-dom/server';
import { describe, expect, it, vi } from 'vitest';

import zhMessages from '../../i18n/zh.json';

function zhLookup(key: string): string | undefined {
  const value = key.split('.').reduce<unknown>((node, part) => {
    if (node && typeof node === 'object' && part in (node as Record<string, unknown>)) {
      return (node as Record<string, unknown>)[part];
    }
    return undefined;
  }, zhMessages);
  return typeof value === 'string' ? value : undefined;
}

const zhT = (_key: string, fallback?: string | Record<string, unknown>, options?: Record<string, unknown>) => {
  const text = zhLookup(_key) ?? (typeof fallback === 'string' ? fallback : _key);
  return Object.entries(options || {}).reduce(
    (acc, [name, value]) => acc.replace(new RegExp(`{{\\s*${name}\\s*}}`, 'g'), String(value)),
    text,
  );
};

vi.mock('react-i18next', () => ({
  useTranslation: () => ({ t: zhT, i18n: { language: 'zh' } }),
}));

import { parseUserAttachment } from '../agent-detail/chatAttachments';
import RunDisclosureBlock from '../agent-detail/RunDisclosureBlock';
import { buildRunTimelineFromMessages } from '../agent-detail/chatDisclosureReducer';
import { ThreadItemRenderer } from './ThreadItemRenderer';
import { sessionTitleForUser } from './sessionTitlePresentation';
import { SessionWorkbenchHeader } from './SessionWorkbenchChrome';
import type { ThreadItem } from '../../api/domains/threadItems.generated';
import type { AgentChatMessage } from '../agent-detail/chatRuntime';

// ---------------------------------------------------------------------------
// The internal-word list: plan of record section 2.3 plus the words the
// production run surfaced on the default screen (brief, 2026-11-04).
// ---------------------------------------------------------------------------
const BANNED_WORDS = [
  '主体', '对账', '回执', '来源绑定', '租约', '隔离', '入账',
  'runtime', 'token', 'trace', 'schema', 'quarantine', 'workspace',
  'Agent', 'agent', 'A2A',
];
const SNAKE_CASE_RE = /[a-z]+_[a-z_]+/;
const PATH_RE = /workspace\/|(?:^|\s)\/(?:tmp|usr|home|var)\//;
const JSON_RE = /\[\{|"\w+"\s*:|\{"\w/;

function stripMarkup(markup: string): string {
  // Content behind a collapsed <details> disclosure is the operator affordance
  // (B2 keeps specifics there); the default surface is everything else.
  return markup.replace(/<details[\s\S]*?<\/details>/g, ' ').replace(/<[^>]*>/g, ' ');
}

function findInternalWords(text: string): string[] {
  const found: string[] = [];
  for (const word of BANNED_WORDS) {
    if (text.includes(word)) found.push(word);
  }
  if (SNAKE_CASE_RE.test(text)) found.push(`snake_case:${text.match(SNAKE_CASE_RE)![0]}`);
  if (PATH_RE.test(text)) found.push('file-path');
  if (JSON_RE.test(text)) found.push('raw-json');
  return found;
}

// The default timeline's own copy namespaces (the conversation screen a
// member sees; operator panels have their own surfaces outside this scan).
const DEFAULT_SCREEN_KEY_ROOTS = [
  'agent.chat',
  'agent.upload',
  'agent.phase',
  'layout',
  'nav',
  'common',
  'sessionWorkbench.history',
  'sessionWorkbench.threadItem.type',
  'sessionWorkbench.threadItem.status',
  'sessionWorkbench.threadItem.value',
  'sessionWorkbench.threadItem.failure',
  'sessionWorkbench.threadItem.permissionNeeded',
  'sessionWorkbench.threadItem.permissionDecision',
  'sessionWorkbench.threadItem.field',
  'sessionWorkbench.threadItem.details',
  'sessionWorkbench.threadItem.approvalActions',
  'sessionWorkbench.threadItem.retryTurn',
  'sessionWorkbench.threadItem.thisTool',
  'sessionWorkbench.waitingGuidance',
  'sessionWorkbench.status',
];

function flattenLeaves(node: unknown, prefix: string, out: Array<[string, string]>): void {
  if (node && typeof node === 'object') {
    for (const [key, value] of Object.entries(node as Record<string, unknown>)) {
      flattenLeaves(value, prefix ? `${prefix}.${key}` : key, out);
    }
    return;
  }
  if (typeof node === 'string') out.push([prefix, node]);
}

describe('default screen language (B2)', () => {
  it('keeps internal vocabulary out of the zh copy the default timeline uses', () => {
    const failures: string[] = [];
    for (const root of DEFAULT_SCREEN_KEY_ROOTS) {
      const node = root.split('.').reduce<unknown>((acc, part) => {
        if (acc && typeof acc === 'object' && part in (acc as Record<string, unknown>)) {
          return (acc as Record<string, unknown>)[part];
        }
        return undefined;
      }, zhMessages);
      if (node === undefined) continue;
      const leaves: Array<[string, string]> = [];
      flattenLeaves(node, root, leaves);
      for (const [key, value] of leaves) {
        for (const hit of findInternalWords(value)) {
          failures.push(`${key}: ${hit}`);
        }
      }
    }
    expect(failures, `internal vocabulary found:\n${failures.join('\n')}`).toEqual([]);
  });

  it('renders the attachment bubble as the file name and the user sentence only', () => {
    const message: AgentChatMessage = {
      role: 'user',
      content: '[File: sales.csv]\nFile location: workspace/uploads/sales.csv (for read_file/read_document tools)\n\n| date |\n| --- |\n\nQuestion: [E2E] 读这个文件，告诉我一共几行数据',
      sessionItem: {
        kind: 'human_input',
        id: 'item-1',
        occurredAt: '2026-11-04T12:00:00Z',
        visibility: { audience: 'user' },
        lifecycle: 'accepted',
        actor: { type: 'user', id: 'u1' },
        payload: {
          content_parts: [
            {
              type: 'text',
              text: '[File: sales.csv]\nFile location: workspace/uploads/sales.csv (for read_file/read_document tools)\n\n| date |\n| --- |\n\nQuestion: [E2E] 读这个文件，告诉我一共几行数据',
              display_content: '[📎 sales.csv]\n[E2E] 读这个文件，告诉我一共几行数据',
              file_name: 'sales.csv',
            },
            { type: 'attachment_ref', attachment: { name: 'sales.csv', path: 'workspace/uploads/sales.csv' } },
          ],
        },
      } as unknown as AgentChatMessage['sessionItem'],
    };
    const parsed = parseUserAttachment(message, 'agent-1');
    expect(parsed.content).toBe('[📎 sales.csv]\n[E2E] 读这个文件，告诉我一共几行数据');
    const bubbleMarkup = renderToStaticMarkup(
      <div>
        <div>{parsed.content}</div>
        {parsed.inputAttachments?.map((file) => (
          <span key={file.path} className="session-tui-file-chip">{file.name}</span>
        ))}
      </div>,
    );
    expect(stripMarkup(bubbleMarkup)).toContain('[E2E] 读这个文件');
    expect(findInternalWords(stripMarkup(bubbleMarkup))).toEqual([]);
  });

  it('never shows a raw JSON session title and recovers the first user text for stored JSON titles', () => {
    const jsonTitle = JSON.stringify([
      { display_content: '[📎 sales.csv]\n[E2E] 读这个文件，告诉我一共几行数据', file_name: 'sales.csv', text: '[File: sales.csv]…internal…', type: 'text' },
      { type: 'attachment_ref', attachment: { name: 'sales.csv', path: 'workspace/uploads/sales.csv' } },
    ]);
    const recovered = sessionTitleForUser({ id: 's1', title: jsonTitle }, '新对话');
    expect(recovered).toContain('[E2E] 读这个文件');
    expect(recovered.startsWith('[{')).toBe(false);

    const plain = sessionTitleForUser({ id: 's2', title: '整理季度报告' }, '新对话');
    expect(plain).toBe('整理季度报告');
  });

  it('renders the approval card in user words with no raw tool name or JSON on the default surface', () => {
    const item: ThreadItem = {
      schema: 'hive.thread_item.v1',
      schema_version: 1,
      id: 'item-approval',
      sequence: 4,
      thread_id: 's1',
      session_id: 's1',
      item_type: 'approval_request',
      item_status: 'waiting_user',
      actor_type: 'user',
      event_type: 'permission',
      role: 'system',
      visibility_scope: 'direct_user',
      listed_surface: 'chat',
      content: '',
      parts: [],
      metadata: {},
      item_data: {
        permission_request_id: 'permission-7',
        tool_name: 'create_employee_from_request',
        arguments: { name: '周报助手' },
        expires_at: '2026-11-04T20:06:21Z',
        destructive: false,
      },
      user_action: {
        kind: 'resolve_approval',
        label: '确认后继续',
        expires_at: '2026-11-04T20:06:21Z',
        impact: '可撤销或只读操作',
        details: [{ label: 'name', value: '周报助手' }],
      },
      audience: 'user',
    } as unknown as ThreadItem;
    const markup = renderToStaticMarkup(
      <ThreadItemRenderer item={item} approvalActions={<button type="button">本次允许</button>} />,
    );
    const text = stripMarkup(markup);
    expect(text).toContain('需要你确认');
    expect(text).toContain('创建数字员工');
    // The raw tool name and raw JSON stay off the default surface (they may
    // live behind the card's collapsed details disclosure).
    expect(markup).not.toContain('create_employee_from_request');
    expect(stripMarkup(markup)).not.toMatch(/\{"/);
  });

  it('renders tool steps, run summary and states in zh with no internal vocabulary', () => {
    const messages: AgentChatMessage[] = [
      { role: 'tool_call', content: '', id: 't1', toolName: 'read_file', toolArgs: { path: 'workspace/uploads/sales.csv' }, toolStatus: 'done', toolResult: 'Read sales.csv (84 chars)', timestamp: '2026-11-04T12:00:01Z' },
      { role: 'tool_call', content: '', id: 't2', toolName: 'read_context_resource', toolArgs: { resource_id: 'mem-1' }, toolStatus: 'done', toolResult: 'ok', timestamp: '2026-11-04T12:00:02Z' },
      { role: 'tool_call', content: '', id: 't3', toolName: 'execute_code', toolArgs: { command: 'python analyze.py' }, toolStatus: 'running', toolResult: 'ok', timestamp: '2026-11-04T12:00:03Z' },
      { role: 'tool_call', content: '', id: 't4', toolName: 'write_file', toolArgs: { path: 'workspace/report.md' }, toolStatus: 'done', toolResult: 'Written to workspace/report.md (3072 chars)', timestamp: '2026-11-04T12:00:04Z' },
    ];
    const timeline = buildRunTimelineFromMessages(messages, { turnStartedAt: '2026-11-04T12:00:00Z', activeRun: true });
    const markup = renderToStaticMarkup(<RunDisclosureBlock timeline={timeline} />);
    const text = stripMarkup(markup);
    expect(text).toContain('读取');
    expect(text).toContain('运行命令');
    expect(findInternalWords(text)).toEqual([]);
  });

  it('renders the failure state in member-safe copy without promising steps that are not shown', () => {
    const failedHeader = renderToStaticMarkup(
      <SessionWorkbenchHeader model={{ title: '读文件', status: 'failed' } as never} />,
    );
    expect(stripMarkup(failedHeader)).toContain('失败');
    expect(failedHeader).not.toContain('查看下方');

    const errorItem: ThreadItem = {
      schema: 'hive.thread_item.v1',
      schema_version: 1,
      id: 'item-error',
      sequence: 5,
      thread_id: 's1',
      session_id: 's1',
      item_type: 'error',
      item_status: 'failed',
      actor_type: 'system',
      event_type: 'runtime_failure',
      role: 'system',
      visibility_scope: 'direct_user',
      listed_surface: 'chat',
      content: '',
      parts: [],
      metadata: {},
      item_data: { code: 'llm_model_missing', retryable: false },
      audience: 'user',
    } as unknown as ThreadItem;
    const markup = renderToStaticMarkup(<ThreadItemRenderer item={errorItem} />);
    const text = stripMarkup(markup);
    expect(text).toContain('联系管理员');
    expect(text).not.toContain('权限与设置');
    expect(findInternalWords(text)).toEqual([]);
  });

  it('keeps the stop notice from lingering as bookkeeping copy once the run is stopped', async () => {
    // The banner copy itself carries no internal bookkeeping words…
    expect(zhLookup('agent.chat.stopRecorded')).not.toContain('入账');
    expect(zhLookup('agent.chat.inputRecorded')).not.toContain('入账');
    // …and a settled run hides the stop-pending notice.
    const { settledRunHidesNotice } = await import('./SessionWorkbenchChrome');
    const stopNotice = zhLookup('agent.chat.stopRecorded')!;
    expect(settledRunHidesNotice(stopNotice, 'cancelled', stopNotice)).toBe(true);
    expect(settledRunHidesNotice(stopNotice, 'running', stopNotice)).toBe(false);
    expect(settledRunHidesNotice('其他提示', 'cancelled', stopNotice)).toBe(false);
  });
});
