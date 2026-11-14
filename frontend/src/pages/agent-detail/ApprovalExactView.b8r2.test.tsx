// @vitest-environment jsdom
import { cleanup, fireEvent, render, screen } from '@testing-library/react';
import { afterEach, expect, it, vi } from 'vitest';
import zh from '../../i18n/zh.json';
import { translateFromCatalog } from '../../test/i18nMock';
import { ThreadItemRenderer } from '../session-workbench/ThreadItemRenderer';
import SessionPermissionActions from './SessionPermissionActions';

const t = (key: string, fallback: string) => translateFromCatalog(zh, key, fallback);
vi.mock('react-i18next', () => ({ useTranslation: () => ({
  t: (key: string, fallback?: any, values?: any) => translateFromCatalog(zh, key, fallback, values),
  i18n: { language: 'zh' },
}) }));
afterEach(cleanup);

const cases = [
  { tool: 'reply_email', args: { message_id: '<abc@corp.test>', body: '好的' } },
  { tool: 'mcp_move', args: { from: '/docs/contract.pdf', to: 's3://public-bucket/contract.pdf',
    recipient: 'decoy@example.test', forward_to: 'actual@example.test' } },
  { tool: 'feishu_base_record_upsert', args: { fields: { 金额: 100, 状态: '已付款', to: '2026-12-31' } } },
  { tool: 'create_employee_from_request', args: { name: '周报助手', role_description: '整理周报',
    core_outputs: ['周报'], boundaries: '不得发送邮件', dispatch_once: false, first_task: '起草周报' } },
];

it.each(cases.flatMap(value => ['thread', 'standalone'].map(view => ({ ...value, view }))))
('B8-r2 shows the exact $tool proposal in the $view fold', ({ tool, args, view }) => {
  const request = { permission_request_id: 'approval', tool_name: tool, arguments: args };
  const original = JSON.stringify(request);
  if (view === 'thread') {
    render(<ThreadItemRenderer item={{ id: 'approval', item_type: 'approval_request', item_status: 'waiting_user',
      item_data: request, audience: 'user', event_type: 'permission', metadata: {} } as never} approvalArguments={args} />);
    fireEvent.click(screen.getByText('详情'));
    fireEvent.click(screen.getByRole('button', { name: '查看将要执行的完整内容' }));
  } else {
    render(<SessionPermissionActions permissionRequest={request} t={t} />);
    fireEvent.click(screen.getByText('操作详情'));
  }
  const text = screen.getByLabelText('操作详情').textContent!;
  expect(text).toBe(JSON.stringify(args, null, 2));
  expect(JSON.parse(text)).toEqual(args);
  expect(JSON.stringify(request)).toBe(original);
});
