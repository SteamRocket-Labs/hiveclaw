// @vitest-environment jsdom
/**
 * B2: the default screen speaks the member's language and shows only what
 * they sent. Internal vocabulary (plan 2.3 + production findings 2026-11-04)
 * must not appear in the default timeline's zh copy or its rendered markup.
 */
import React from 'react';
import { renderToStaticMarkup } from 'react-dom/server';
import { cleanup, fireEvent, render, screen } from '@testing-library/react';
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
import SessionPermissionActions from '../agent-detail/SessionPermissionActions';
import { ArtifactCards } from '../agent-detail/ArtifactSurface';
import { ThreadItemRenderer, shouldRenderThreadItemInConversation } from './ThreadItemRenderer';
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
  'Delegation', 'Team', 'Workers', 'Workflow', 'Activity', 'BACKGROUND',
  'snapshot', 'My Assistant', 'Send Email', 'Copy', 'Collapse Sidebar', 'Scroll to bottom', 'Open ',
];
const SNAKE_CASE_RE = /[a-z]+_[a-z_]+/;
const PATH_RE = /workspace\/|(?:^|\s)\/(?:tmp|usr|home|var)\//;
const JSON_RE = /\[\{|"\w+"\s*:|\{"\w/;

function decodeEntities(text: string): string {
  return text
    .replace(/&#x([0-9a-f]+);/gi, (_, hex) => String.fromCodePoint(parseInt(hex, 16)))
    .replace(/&#(\d+);/g, (_, dec) => String.fromCodePoint(Number(dec)))
    .replace(/&quot;/g, '"')
    .replace(/&lt;/g, '<')
    .replace(/&gt;/g, '>')
    .replace(/&nbsp;/g, ' ')
    .replace(/&#39;/g, "'")
    .replace(/&amp;/g, '&');
}

/**
 * What a member actually sees: a closed `<details>` keeps only its summary;
 * an `open` one (or content the renderer prints unconditionally) is visible.
 * Nested disclosures resolve innermost-first. Markup is then stripped and
 * HTML entities decoded so escaped bytes cannot hide internal words.
 */
function visibleSurface(markup: string): string {
  let html = markup;
  // Resolve disclosures innermost-first: an open <details> contributes its
  // body; a closed one keeps only its <summary> (the visible affordance).
  const innermost = /<details\b[^>]*>(?:(?!<details\b)[\s\S])*?<\/details>/;
  for (let match = html.match(innermost); match; match = html.match(innermost)) {
    const block = match[0];
    const body = block.replace(/^<details\b[^>]*>/, '').replace(/<\/details>$/, '');
    const replacement = /^<details\b[^>]*\bopen\b/.test(block)
      ? body
      : (body.match(/<summary[^>]*>([\s\S]*?)<\/summary>/)?.[1] ?? ' ');
    html = html.replace(block, () => replacement);
  }
  return decodeEntities(html.replace(/<[^>]*>/g, ' '));
}

function findInternalWords(text: string): string[] {
  const found: string[] = [];
  for (const word of BANNED_WORDS) {
    if (word === 'Team' ? /\bTeam\b/.test(text) : text.includes(word)) found.push(word);
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
  'sessionWorkbench.rightPanel',
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

// The production multi-part input the E2E member sent (2026-11-04).
const E2E_TEXT = '[File: sales.csv]\nFile location: workspace/uploads/sales.csv (for read_file/read_document tools)\n\n| date |\n| --- |\n\nQuestion: [E2E] 读这个文件，告诉我一共几行数据';
const E2E_DISPLAY = '[📎 sales.csv]\n[E2E] 读这个文件，告诉我一共几行数据';
const E2E_TEXT_PART = {
  type: 'text',
  text: E2E_TEXT,
  display_content: E2E_DISPLAY,
  file_name: 'sales.csv',
};
const E2E_PARTS = [
  E2E_TEXT_PART,
  { type: 'attachment_ref', attachment: { name: 'sales.csv', path: 'workspace/uploads/sales.csv' } },
];

function sortedJson(value: unknown): string {
  // Mirrors the backend's `_runtime_content` encoding (sort_keys=True,
  // ensure_ascii=False, compact separators).
  const sort = (node: unknown): unknown => {
    if (Array.isArray(node)) return node.map(sort);
    if (node && typeof node === 'object') {
      return Object.fromEntries(Object.entries(node as Record<string, unknown>)
        .sort(([a], [b]) => (a < b ? -1 : a > b ? 1 : 0))
        .map(([key, val]) => [key, sort(val)]));
    }
    return node;
  };
  return JSON.stringify(sort(value));
}

/** The historical `chat_sessions.title`: first 40 code points of the JSON. */
function storedTitle(parts: unknown): string {
  return Array.from(sortedJson(parts)).slice(0, 40).join('');
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

  it('renders the attachment bubble as the file chip and the user sentence, each shown once', () => {
    const message: AgentChatMessage = {
      role: 'user',
      content: E2E_TEXT,
      sessionItem: {
        kind: 'human_input',
        id: 'item-1',
        occurredAt: '2026-11-04T12:00:00Z',
        visibility: { audience: 'user' },
        lifecycle: 'accepted',
        actor: { type: 'user', id: 'u1' },
        payload: { content_parts: E2E_PARTS },
      } as unknown as AgentChatMessage['sessionItem'],
    };
    const parsed = parseUserAttachment(message, 'agent-1');
    // The marker line is the chip's job now: the bubble keeps the sentence.
    expect(parsed.content).toBe('[E2E] 读这个文件，告诉我一共几行数据');

    // The real attachment card component (not hand-made markup).
    const chip = (
      <ArtifactCards agentId="agent-1" artifacts={parsed.inputAttachments} viewerUserId="u1" />
    );
    const chipMarkup = renderToStaticMarkup(chip);
    const bubbleMarkup = renderToStaticMarkup(
      <div>
        {parsed.content && <div>{parsed.content}</div>}
        {chip}
      </div>,
    );
    const text = visibleSurface(bubbleMarkup);
    expect(text).toContain('[E2E] 读这个文件');
    expect(text).toContain('sales.csv');
    // The file appears once: no duplicated `[📎 …]` marker line in the text.
    expect(text).not.toContain('[📎');
    expect(text.match(/sales\.csv/g) || []).toHaveLength(1);
    // The full file name stays addressable for the narrow-viewport tooltip.
    expect(chipMarkup).toContain('title="sales.csv"');
    expect(findInternalWords(text)).toEqual([]);
  });

  it('recovers titles from production-truncated JSON and drops attachment markers from every title', () => {
    // Production stored only the first 40 characters of the runtime JSON
    // (web_chat_runtime.py); recovery must work from that truncated prefix.
    const truncated = storedTitle(E2E_PARTS);
    expect(truncated.startsWith('[{"display_content":"')).toBe(true);
    expect(Array.from(truncated)).toHaveLength(40);
    const recovered = sessionTitleForUser({ id: 's1', title: truncated }, '新对话');
    expect(recovered.startsWith('[{')).toBe(false);
    expect(recovered).not.toContain('display_content');
    expect(recovered).toContain('[E2E');

    // Attachment-first production shape: the display text never fit; the
    // file name carries the title instead of raw JSON.
    const attachmentFirst = storedTitle([{ type: 'attachment_ref', attachment: { name: 'sales.csv', path: 'workspace/uploads/sales.csv' } }, E2E_TEXT_PART]);
    expect(attachmentFirst.startsWith('[{"attachment"')).toBe(true);
    expect(sessionTitleForUser({ id: 's2', title: attachmentFirst }, '新对话')).toBe('📎 sales.csv');

    // Full stored JSON (pre-truncation sources) still recovers the sentence.
    const jsonTitle = sortedJson(E2E_PARTS);
    expect(sessionTitleForUser({ id: 's3', title: jsonTitle }, '新对话')).toContain('[E2E] 读这个文件');

    // Current-session titles carry the leading `[📎 …]` marker: drop it, and
    // fall back to the file name when the member typed no sentence.
    expect(sessionTitleForUser({ id: 's4', title: '[📎 sales.csv] [E2E] 读这个文件，告诉我一共几行数据' }, '新对话'))
      .toBe('[E2E] 读这个文件，告诉我一共几行数据');
    expect(sessionTitleForUser({ id: 's5', title: '[📎 sales.csv] ' }, '新对话')).toBe('📎 sales.csv');
    expect(sessionTitleForUser({ id: 's6', title: '整理季度报告' }, '新对话')).toBe('整理季度报告');
  });

  it('recovers every runtime-JSON title shape from its truncated prefix, never as raw JSON (B2 r3)', () => {
    const truncate = (value: string): string => Array.from(value).slice(0, 40).join('');
    const sentence = '总结最近的季度销售数据，并给出下一季度的行动建议、风险提示和对应的负责人安排';

    // A `text`-first encoding (a text part without display_content and with
    // no file name of its own sorts `text` first).
    const textFirst = truncate(`[{"text":"${sentence}`);
    expect(textFirst.startsWith('[{"text":"')).toBe(true);
    expect(Array.from(textFirst)).toHaveLength(40);
    expect(sessionTitleForUser({ id: 't1', title: textFirst }, '新对话'))
      .toBe(Array.from(sentence).slice(0, 30).join(''));

    // A `file_name`-first encoding: the member's text never fit in the
    // prefix; the file name carries the title.
    const fileNameFirst = truncate(`[{"file_name":"sales.csv","text":"[File: sales.csv]\\nFile location: workspace/uploads/sales.csv\\n\\nQuestion: ${sentence}`);
    expect(fileNameFirst.startsWith('[{"file_name":"')).toBe(true);
    expect(sessionTitleForUser({ id: 't2', title: fileNameFirst }, '新对话')).toBe('📎 sales.csv');

    // Attachment-first: no text part is reached at all, the attachment's
    // name is the title.
    const attachmentFirst = truncate(sortedJson([{ type: 'attachment_ref', attachment: { name: 'sales.csv', path: 'workspace/uploads/sales.csv' } }, E2E_TEXT_PART]));
    expect(attachmentFirst.startsWith('[{"attachment"')).toBe(true);
    expect(sessionTitleForUser({ id: 't3', title: attachmentFirst }, '新对话')).toBe('📎 sales.csv');

    // A cut trailing escape (`\u4e` of 中) ends the value instead of
    // corrupting it.
    const cutEscape = '[{"display_content":"[📎 销售记录.xlsx]\\n第四季度汇总\\u4e';
    expect(sessionTitleForUser({ id: 't4', title: cutEscape }, '新对话')).toBe('第四季度汇总');

    // A runtime-JSON shape with nothing recoverable shows the plain default
    // title — never the raw JSON.
    const nothingRecoverable = truncate(sortedJson([{ type: 'attachment_ref', attachment: { path: 'workspace/uploads/sales.csv' } }]));
    expect(nothingRecoverable.startsWith('[{')).toBe(true);
    expect(sessionTitleForUser({ id: 't5', title: nothingRecoverable }, '新对话')).toBe('新对话');
    // And no JSON shape ever leaks through as the title.
    for (const shape of [textFirst, fileNameFirst, attachmentFirst, cutEscape, nothingRecoverable]) {
      const recovered = sessionTitleForUser({ id: `x-${shape.slice(0, 8)}`, title: shape }, '新对话');
      expect(recovered.startsWith('[{')).toBe(false);
      expect(recovered).not.toContain('"');
      expect(recovered).not.toContain('display_content');
    }
  });

  it('names the exact approval action; a delete or share is never called an edit', () => {
    const approvalFor = (toolName: string): string => {
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
          tool_name: toolName,
          arguments: { name: 'Q3 复盘文档' },
          expires_at: '2026-11-04T20:06:21Z',
          destructive: false,
        },
        user_action: {
          kind: 'resolve_approval',
          label: '确认后继续',
          expires_at: '2026-11-04T20:06:21Z',
          impact: '可撤销或只读操作',
          details: [{ label: 'name', value: 'Q3 复盘文档' }],
        },
        audience: 'user',
      } as unknown as ThreadItem;
      return visibleSurface(renderToStaticMarkup(<ThreadItemRenderer item={item} />));
    };

    expect(approvalFor('create_employee_from_request')).toContain('创建数字员工');
    expect(approvalFor('feishu_doc_delete')).toContain('删除飞书文档');
    expect(approvalFor('feishu_doc_share')).toContain('分享飞书文档');
    for (const text of [approvalFor('feishu_doc_delete'), approvalFor('feishu_doc_share')]) {
      expect(text).not.toContain('编辑飞书文档');
      expect(text).toContain('Q3 复盘文档');
    }
    // An unmapped Feishu tool stays neutral: it never guesses a verb.
    const unknown = approvalFor('feishu_doc_rename');
    expect(unknown).toContain('飞书文档');
    expect(unknown).not.toContain('编辑飞书文档');
    expect(unknown).not.toContain('删除飞书文档');
    expect(findInternalWords(approvalFor('feishu_doc_delete'))).toEqual([]);
  });

  it('puts the whole proposal inside the approval card\'s single disclosure (B2 r3)', () => {
    // The approver must be able to see the recipients and body before
    // allowing a send_email: the card keeps exactly one disclosure and its
    // body carries the full arguments (what the old 操作详情 showed), not
    // just the whitelisted summary fields.
    const emailArguments = {
      to: 'boss@example.com',
      subject: 'Q3 复盘',
      body: '各位好，附件是 Q3 复盘材料，请查收。',
    };
    const item: ThreadItem = {
      schema: 'hive.thread_item.v1',
      schema_version: 1,
      id: 'item-approval-email',
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
        permission_request_id: 'permission-9',
        tool_name: 'send_email',
        arguments: emailArguments,
        expires_at: '2026-11-04T20:06:21Z',
        destructive: false,
      },
      user_action: {
        kind: 'resolve_approval',
        label: '确认后继续',
        expires_at: '2026-11-04T20:06:21Z',
        impact: '可撤销或只读操作',
        details: [{ label: 'subject', value: 'Q3 复盘' }],
      },
      audience: 'user',
    } as unknown as ThreadItem;
    const element = (
      <ThreadItemRenderer
        item={item}
        approvalArguments={emailArguments}
        approvalActions={(
          <SessionPermissionActions
            permissionRequest={{
              permission_request_id: 'permission-9',
              session_id: 's1',
              tool_name: 'send_email',
              arguments: emailArguments,
              allow_session_allowed: true,
              created_at: '2026-11-04T12:00:00Z',
            } as never}
            showArgumentDetails={false}
            t={zhT as never}
          />
        )}
      />
    );
    const markup = renderToStaticMarkup(element);
    // Still exactly one disclosure on the card.
    expect(markup.match(/<details\b/g) || []).toHaveLength(1);
    // Closed, the member's visible surface shows the request but none of the
    // proposal's content: recipients, subject and body wait behind the open.
    const closed = visibleSurface(markup);
    expect(closed).toContain('需要你确认');
    expect(closed).not.toContain('boss@example.com');
    expect(closed).not.toContain('Q3 复盘');
    expect(closed).not.toContain('请查收');
    // B7: raw parameters need their own explicit reveal inside the details.
    const view = render(element);
    fireEvent.click(screen.getByText('详情'));
    expect(view.container.querySelector('pre')).toBeNull();
    const summary = view.container.querySelector('.thread-item-disclosure')!.textContent!;
    expect(summary).toContain('boss@example.com');
    expect(summary).toContain('Q3 复盘');
    expect(summary).toContain('请查收');
    fireEvent.click(screen.getByRole('button', { name: '完整参数' }));
    const body = view.container.querySelector('pre')!.textContent!;
    expect(body).toContain('"to"');
    expect(body).toContain('boss@example.com');
    expect(body).toContain('Q3 复盘');
    expect(body).toContain('请查收');
    cleanup();
  });

  it('renders one disclosure on the approval card, not two', () => {
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
        tool_name: 'send_email',
        arguments: { to: 'a@b.c', subject: 'Q3' },
        expires_at: '2026-11-04T20:06:21Z',
        destructive: false,
      },
      user_action: {
        kind: 'resolve_approval',
        label: '确认后继续',
        expires_at: '2026-11-04T20:06:21Z',
        impact: '可撤销或只读操作',
        details: [],
      },
      audience: 'user',
    } as unknown as ThreadItem;
    const markup = renderToStaticMarkup(
      <ThreadItemRenderer
        item={item}
        approvalActions={(
          <SessionPermissionActions
            permissionRequest={{
              permission_request_id: 'permission-7',
              session_id: 's1',
              tool_name: 'send_email',
              arguments: { to: 'a@b.c', subject: 'Q3' },
              allow_session_allowed: true,
              created_at: '2026-11-04T12:00:00Z',
            } as never}
            showArgumentDetails={false}
            t={zhT as never}
          />
        )}
      />,
    );
    expect(markup.match(/<details\b/g) || []).toHaveLength(1);
    expect(visibleSurface(markup)).toContain('需要你确认');
  });

  it('keeps commands, arguments and output off the default layer of a failed run', () => {
    const messages: AgentChatMessage[] = [
      { role: 'tool_call', content: '', id: 't1', toolName: 'read_file', toolArgs: { path: 'workspace/uploads/sales.csv' }, toolStatus: 'done', toolResult: 'Read sales.csv (84 chars)', timestamp: '2026-11-04T12:00:01Z' },
      { role: 'tool_call', content: '', id: 't2', toolName: 'execute_code', toolArgs: { command: 'python workspace/analyze.py' }, toolStatus: 'done', toolMeta: { kind: 'runtime_step', stepId: 'step-2', toolCallId: 'call-2', status: 'failed', durationMs: 1200, visibility: 'collapsed' }, toolResult: 'Written to workspace/report.md\nok', timestamp: '2026-11-04T12:00:02Z' },
    ];
    const timeline = buildRunTimelineFromMessages(messages, { turnStartedAt: '2026-11-04T12:00:00Z', activeRun: true });
    const markup = renderToStaticMarkup(<RunDisclosureBlock timeline={timeline} />);
    const text = visibleSurface(markup);
    // The localized action and its state stay on the default layer…
    expect(text).toContain('读取');
    expect(text).toContain('运行命令');
    expect(text).toContain('失败');
    // …while the command line, its arguments and its output wait behind the
    // step's own disclosure.
    expect(text).not.toContain('python');
    expect(text).not.toContain('analyze.py');
    expect(text).not.toContain('Written to');
    expect(text).not.toContain('$');
    expect(findInternalWords(text)).toEqual([]);
  });

  it('names known steps in the approval card words, folds rows that name nothing, and localizes sub-agent steps', () => {
    const messages: AgentChatMessage[] = [
      { role: 'tool_call', content: '', id: 't1', toolName: 'create_employee_from_request', toolArgs: { name: '周报助手' }, toolStatus: 'done', toolResult: 'ok', timestamp: '2026-11-04T12:00:01Z' },
      { role: 'tool_call', content: '', id: 't2', toolName: 'read_context_resource', toolArgs: { resource_id: 'mem-1' }, toolStatus: 'done', toolResult: 'ok', timestamp: '2026-11-04T12:00:02Z' },
      { role: 'tool_call', content: '', id: 't3', toolName: 'spawn_subagent', toolArgs: { task: '检查数据' }, toolMeta: { kind: 'runtime_step', stepId: 'step-3', toolCallId: 'call-3', status: 'running', durationMs: 800, visibility: 'collapsed' }, toolStatus: 'running', toolResult: 'ok', timestamp: '2026-11-04T12:00:03Z' },
    ];
    const timeline = buildRunTimelineFromMessages(messages, { turnStartedAt: '2026-11-04T12:00:00Z', activeRun: true });
    const markup = renderToStaticMarkup(<RunDisclosureBlock timeline={timeline} />);
    const text = visibleSurface(markup);
    // The creation step uses the same words as the approval card.
    expect(text).toContain('创建数字员工');
    // Sub-agent steps are zh (the old key mismatch rendered English).
    expect(text).toContain('子任务');
    expect(text).not.toContain('Sub-agent');
    // A retrieval row that names nothing is not a row: no generic "工具"
    // label and no orphan "已调用工具" group, but the step count still
    // carries it.
    expect(text).not.toContain('工具');
    expect(text).not.toContain('已调用工具');
    expect(text).toContain('3 个步骤');
    expect(findInternalWords(text)).toEqual([]);
  });

  it('keeps every step reachable in the opened details, with its original tool name and result (B2 r3)', () => {
    // Nameless steps (no user-words action, no summary) fold off the default
    // layer, but opening the run history must still surface every one of
    // them, identified by its original tool name, with its result reachable.
    const messages: AgentChatMessage[] = [
      { role: 'tool_call', content: '', id: 't1', toolName: 'read_context_resource', toolArgs: { ref: 'index' }, toolStatus: 'done', toolResult: 'INDEX CONTENT 42', timestamp: '2026-11-04T12:00:01Z' },
      { role: 'tool_call', content: '', id: 't2', toolName: 'call_mcp_tool', toolArgs: { tool_name: 'weather' }, toolStatus: 'done', toolResult: 'MCP RESULT ok', timestamp: '2026-11-04T12:00:02Z' },
    ];
    const timeline = buildRunTimelineFromMessages(messages, { turnStartedAt: '2026-11-04T12:00:00Z' });
    render(<RunDisclosureBlock timeline={timeline} />);
    // The run finished: its disclosure starts folded. Open it like a user.
    fireEvent.click(document.querySelector('button.run-disclosure-header')!);
    // The folded nameless steps live behind the quiet "…" group: open it.
    const group = screen.getByTestId('run-disclosure-tool-group') as HTMLDetailsElement;
    group.open = true;
    // Every step is there, identified by its original tool name.
    expect(screen.getByText('read_context_resource')).toBeTruthy();
    expect(screen.getByText('call_mcp_tool')).toBeTruthy();
    // The row's own details keep args and result next to the tool id.
    fireEvent.click(screen.getByRole('button', { name: /call_mcp_tool/ }));
    expect(document.body.textContent).toContain('MCP RESULT ok');
    expect(document.body.textContent).toContain('weather');
    cleanup();
  });

  it('keeps internal run-phase boundary cards off the default timeline', () => {
    const boundaryItem = {
      schema: 'hive.thread_item.v1',
      schema_version: 1,
      id: 'item-boundary',
      sequence: 6,
      thread_id: 's1',
      session_id: 's1',
      item_type: 'boundary',
      item_status: 'waiting_user',
      actor_type: 'system',
      event_type: 'run_started',
      role: 'system',
      visibility_scope: 'direct_user',
      listed_surface: 'chat',
      content: '',
      parts: [],
      metadata: {},
      item_data: { phase: 'waiting_approval', reason: null },
      user_action: { kind: 'review', label: '查看并处理', details: [] },
      user_summary: '任务状态已更新。',
      audience: 'user',
    } as unknown as ThreadItem;
    expect(shouldRenderThreadItemInConversation(boundaryItem, false)).toBe(false);
    // The approval card remains the interactive surface it always was.
    const approvalItem = { ...boundaryItem, item_type: 'approval_request' } as ThreadItem;
    expect(shouldRenderThreadItemInConversation(approvalItem, false)).toBe(true);
  });

  it('renders the failure state in member-safe copy without promising steps that are not shown', () => {
    const failedHeader = renderToStaticMarkup(
      <SessionWorkbenchHeader model={{ title: '读文件', status: 'failed' } as never} />,
    );
    expect(visibleSurface(failedHeader)).toContain('失败');
    expect(failedHeader).not.toContain('查看下方');

    const errorItem = (retryable: boolean): ThreadItem => ({
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
      item_data: { code: 'llm_model_missing', retryable },
      audience: 'user',
    } as unknown as ThreadItem);
    const fatalMarkup = renderToStaticMarkup(<ThreadItemRenderer item={errorItem(false)} />);
    const fatal = visibleSurface(fatalMarkup);
    expect(fatal).toContain('联系管理员');
    expect(fatal).not.toContain('权限与设置');
    // Retryability is an internal label pair: the member sees a retry action
    // when retry is possible and nothing otherwise — never a 重试/不可重试 row.
    expect(fatal).not.toContain('不可重试');
    expect(fatalMarkup).not.toContain('<dt>');
    const retryable = visibleSurface(renderToStaticMarkup(
      <ThreadItemRenderer item={errorItem(true)} actions={<button type="button">重试本轮</button>} />,
    ));
    expect(retryable).toContain('重试本轮');
    expect(retryable).not.toContain('不可重试');
    expect(findInternalWords(fatal)).toEqual([]);
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
