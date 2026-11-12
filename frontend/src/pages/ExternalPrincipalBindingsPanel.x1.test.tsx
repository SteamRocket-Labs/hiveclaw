// @vitest-environment jsdom
import { cleanup, render } from '@testing-library/react';
import { afterEach, expect, it, vi } from 'vitest';

import zh from '../i18n/zh.json';
import { translateFromCatalog } from '../test/i18nMock';
import ExternalPrincipalBindingsPanel from './ExternalPrincipalBindingsPanel';

vi.mock('react-i18next', () => ({
  useTranslation: () => ({ t: (key: string, fallback?: string, values?: Record<string, unknown>) =>
    translateFromCatalog(zh, key, fallback, values) }),
}));

afterEach(cleanup);

it('shows plain channel names, keeping the provider code only as an attribute', () => {
  const { container } = render(<ExternalPrincipalBindingsPanel
    principals={[
      { id: 'p1', provider: 'WECHAT_PERSONAL', display_name: 'WeChat o9cq805q', subject_id: 'o9cq805q@im.wechat', status: 'pending_verification' },
      { id: 'p2', provider: 'telegram', display_name: 'tg-user', subject_id: '12345', status: 'pending_verification' },
      { id: 'p3', provider: 'feishu', display_name: '飞书用户', subject_id: 'ou_1', status: 'pending_verification' },
    ] as never}
    users={[]} loading={false} busyPrincipalId={null} onUnlink={() => undefined}
  />);
  const labels = Array.from(container.querySelectorAll('.external-principal-provider')).map((el) => el.textContent);
  expect(labels).toEqual(['微信', 'Telegram', '飞书']);
  expect(container.textContent).not.toContain('WECHAT_PERSONAL');
  expect(container.querySelector('[data-provider="WECHAT_PERSONAL"]')).toBeTruthy();
});

it('the zh admin storage and originals copy carries no internal vocabulary', () => {
  expect(zh.enterprise.storage.scope).not.toContain('待对账');
  expect(zh.enterprise.storage.scope).not.toContain('孤立');
  expect(zh.enterprise.storage.effective).not.toContain('孤立');
  expect(zh.enterprise.legacyCompanyFiles.emptyBody).not.toContain('隔离区');
  expect(zh.enterprise.originals.open).toBe('管理原始文件');
  expect(zh.enterprise.originals.uploading).not.toContain('待对账');
  expect(zh.enterprise.originals.deleting).not.toContain('待对账');
});
