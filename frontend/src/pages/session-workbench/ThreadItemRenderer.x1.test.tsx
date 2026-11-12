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
  expect(details.textContent).toContain('批准后这次操作才会执行，完成后结果会回到这里。');
  expect(details.textContent).not.toContain('影响工作区以外');
  expect(container.querySelector('pre')).toBeNull();
  expect(screen.queryByRole('button', { name: '完整参数' })).toBeNull();
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
