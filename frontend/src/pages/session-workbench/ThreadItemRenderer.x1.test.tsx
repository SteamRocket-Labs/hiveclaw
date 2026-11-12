// @vitest-environment jsdom
import { cleanup, fireEvent, render, screen } from '@testing-library/react';
import { afterEach, expect, it } from 'vitest';


import { vi } from 'vitest';
import zh from '../../i18n/zh.json';
import { translateFromCatalog } from '../../test/i18nMock';
import { extractArtifactParts } from '../agent-detail/chatRuntime';
import { gitlineCheckpointLabel } from '../agent-detail/SessionLineageSurface';
import { ThreadItemRenderer } from './ThreadItemRenderer';
import type { ThreadItem } from '../../api/domains/threadItems.generated';

vi.mock('react-i18next', () => ({
  useTranslation: () => ({
    t: (key: string, fallback?: string, values?: Record<string, unknown>) =>
      translateFromCatalog(zh, key, fallback, values),
    i18n: { language: 'zh' },
  }),
}));

const t = (key: string, fallback: string) => String(key.split('.').reduce<any>((node, part) => node?.[part], zh) ?? fallback);

afterEach(cleanup);

function approvalItem(tool: string, args: Record<string, unknown>): ThreadItem {
  return {
    id: 'approval-x1', item_type: 'approval_request', item_status: 'waiting_user',
    item_data: { tool_name: tool, arguments: args, destructive: false }, audience: 'user',
    event_type: 'permission', metadata: {},
  } as unknown as ThreadItem;
}

it('a plaza post approval says what is sent and where, without raw JSON or 工具', () => {
  const args = { content: '[E2E] 第十五轮测试帖，请忽略' };
  const { container } = render(<ThreadItemRenderer item={approvalItem('plaza_create_post', args)} approvalArguments={args} />);
  expect(container.textContent).toContain('需要你确认：发布帖子到公司广场');
  fireEvent.click(screen.getByText('详情'));
  const details = container.querySelector('.thread-item-disclosure')!;
  expect(details.textContent).toContain('内容');
  expect(details.textContent).toContain('[E2E] 第十五轮测试帖，请忽略');
  expect(details.textContent).not.toContain('工具');
  // Plaza is fully shown and internally scoped, so the calm sentence stays
  // and the fold carries the complete arguments behind a plain label.
  expect(details.textContent).toContain('批准后这次操作才会执行，完成后结果会回到这里。');
  // Restored (X1-r3 ruling 4): the caution names effects a fully covered
  // plaza post does not have, so the card shows exactly one of the two.
  expect(details.textContent).not.toContain('影响工作区以外');
  expect(container.querySelector('pre')).toBeNull();
  expect(screen.queryByRole('button', { name: '完整参数' })).toBeNull();
  const fold = screen.getByRole('button', { name: '查看将要执行的完整内容' });
  fireEvent.click(fold);
  expect(container.querySelector('pre')?.textContent).toContain('[E2E] 第十五轮测试帖，请忽略');
});

it('maps every approval summary row from the tool\'s real schema keys (X1-r3 ruling 4)', () => {
  // The argument names below are exactly each tool's input_schema properties,
  // read from tool_spec_v1(...) in the backend handlers:
  // backend/app/tools/handlers/filesystem.py (run_command, write_file,
  // fs_write, edit_file, delete_file, execute_code), skills.py
  // (run_skill_tool), communication.py (send_web_message,
  // send_channel_message, send_feishu_message, send_message_to_agent,
  // delegate_to_agent), email.py (send_email, reply_email), hr.py
  // (create_employee_from_request), plaza.py (plaza_create_post,
  // plaza_add_comment). reply_email's recipient row stays unmapped by
  // controller instruction (report-only).
  const cases: Array<[string, Record<string, unknown>, Array<[string, string]>]> = [
    ['run_command', { command: 'git status' }, [['命令', 'git status']]],
    ['create_employee_from_request',
      { name: '周报助手', role_description: '整理周报', core_outputs: ['周报'], boundaries: '仅整理', first_task: '写周报' },
      [['名称', '周报助手'], ['第一项任务', '写周报']]],
    ['send_email',
      { to: 'boss@example.com', subject: 'Q3 复盘', body: '请查收。', cc: 'a@example.test', attachments: ['q3.xlsx'] },
      [['收件人', 'boss@example.com'], ['主题', 'Q3 复盘'], ['正文', '请查收。'], ['抄送', 'a@example.test'], ['附件', 'q3.xlsx']]],
    ['reply_email', { message_id: 'm-1', body: '请查收。' }, [['正文', '请查收。']]],
    ['plaza_create_post', { content: '[E2E] 测试帖，请忽略' }, [['内容', '[E2E] 测试帖，请忽略']]],
    ['plaza_add_comment', { post_id: 'post-1', content: '评论一句' }, [['内容', '评论一句']]],
    ['execute_code', { language: 'bash', code: 'env | grep -i key', timeout: 60 }, [['代码', 'env | grep -i key']]],
    ['run_skill_tool', { skill: 'report', script: 'scripts/build.py', args: ['--all'], timeout: 60 },
      [['名称', 'report'], ['路径', 'scripts/build.py'], ['参数', '--all']]],
    ['delegate_to_agent', { agent_name: '周报助手', message: '写周报', timeout_seconds: 600 },
      [['数字员工', '周报助手'], ['消息', '写周报']]],
    ['send_message_to_agent', { agent_name: '周报助手', message: '写周报', msg_type: 'consult' },
      [['数字员工', '周报助手'], ['消息', '写周报']]],
    ['send_channel_message', { message: '合同价 12 万' }, [['消息', '合同价 12 万']]],
    ['send_feishu_message', { member_name: '覃彤', user_id: 'ou_x', message: '合同价 12 万' },
      [['收件人', '覃彤'], ['消息', '合同价 12 万']]],
    // The Feishu recipient falls back member_name → user_id → open_id
    // (X1-r3 ruling 10, finding 3).
    ['send_feishu_message', { user_id: 'ou_x', message: '合同价 12 万' },
      [['收件人', 'ou_x'], ['消息', '合同价 12 万']]],
    ['send_feishu_message', { open_id: 'om_y', message: '合同价 12 万' },
      [['收件人', 'om_y'], ['消息', '合同价 12 万']]],
    ['send_web_message', { username: '覃彤', message: '合同价 12 万' },
      [['收件人', '覃彤'], ['消息', '合同价 12 万']]],
    ['write_file', { path: 'workspace/report.md', content: '# 报告' }, [['路径', 'workspace/report.md']]],
    ['fs_write', { mode: 'overwrite', path: 'workspace/report.md', content: '# 报告' }, [['路径', 'workspace/report.md']]],
    ['edit_file', { path: 'workspace/report.md', old_text: '旧', new_text: '新' }, [['路径', 'workspace/report.md']]],
    ['delete_file', { path: 'workspace/notes.md' }, [['路径', 'workspace/notes.md']]],
  ];
  for (const [tool, args, rows] of cases) {
    const { container } = render(<ThreadItemRenderer item={approvalItem(tool, args)} approvalArguments={args} />);
    const pairs = Array.from(container.querySelectorAll('.thread-item-details dt'))
      .map((dt) => [dt.textContent, dt.nextElementSibling?.textContent] as [string, string]);
    for (const row of rows) expect(pairs, tool).toContainEqual(row);
  }
});

it('shows the command\'s own working directory, not a hard-coded workspace (X1-r3 ruling 10)', () => {
  const args = { command: 'ls', cwd: '/etc' };
  const { container } = render(<ThreadItemRenderer item={approvalItem('run_command', args)} approvalArguments={args} />);
  const pairs = Array.from(container.querySelectorAll('.thread-item-details dt'))
    .map((dt) => [dt.textContent, dt.nextElementSibling?.textContent] as [string, string]);
  expect(pairs).toContainEqual(['命令', 'ls']);
  expect(pairs).toContainEqual(['工作目录', '/etc']);
  // Without a cwd the card keeps the workspace default.
  const plain = { command: 'ls' };
  const second = render(<ThreadItemRenderer item={approvalItem('run_command', plain)} approvalArguments={plain} />);
  const plainPairs = Array.from(second.container.querySelectorAll('.thread-item-details dt'))
    .map((dt) => [dt.textContent, dt.nextElementSibling?.textContent] as [string, string]);
  expect(plainPairs).toContainEqual(['工作目录', 'workspace/']);
});

it('a replayed upload keeps its file size for the card', () => {
  const parts = extractArtifactParts({
    parts: [{ type: 'artifact', path: 'uploads/sales-run15.csv', name: 'sales-run15.csv', preview_kind: 'csv', size: '956' }],
  });
  expect(parts[0].size).toBe(956);
  const alt = extractArtifactParts({
    parts: [{ type: 'artifact', path: 'uploads/sales-run15.csv', name: 'sales-run15.csv', preview_kind: 'csv', size_bytes: 956 }],
  });
  expect(alt[0].size).toBe(956);
});

it('the gitline node for an auto-resumed run reads as the receipt, not English instructions', () => {
  expect(gitlineCheckpointLabel('Runtime result integration page. This is internal runtime context…', t))
    .toBe('收到任务结果');
  expect(gitlineCheckpointLabel('帮我写周报', t)).toBe('帮我写周报');
});
