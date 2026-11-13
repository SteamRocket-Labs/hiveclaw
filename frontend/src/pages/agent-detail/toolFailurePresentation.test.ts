import type { TFunction } from 'i18next';
import { expect, it } from 'vitest';
import zh from '../../i18n/zh.json';
import { translateFromCatalog } from '../../test/i18nMock';
import { toolFailureForDisplay } from './toolFailurePresentation';

const t = ((key: string, fallback?: string) => translateFromCatalog(zh, key, fallback)) as TFunction;

it.each(['configuration_required', 'auth_required'])('shows the administrator recovery for stored MCP failures (%s)', (errorClass) => {
  const envelope = { ok: false, provider: 'mcp', error_class: errorClass, diagnostic_hint: '请管理员检查应用连接地址。' };
  const result = toolFailureForDisplay(`<tool_error>${JSON.stringify(envelope)}</tool_error>`, t, 'failed');
  expect(result).toBe('应用连接暂时不可用，请联系管理员检查连接设置。');
  expect(result).not.toContain('提供');
});

it('keeps the existing personal-account failure presentation', () => {
  const result = toolFailureForDisplay({ ok: false, provider: 'mcp', error_class: 'auth_required' }, t, 'failed');
  expect(result).toBe(zh.agent.chat.disclosure.failure.permission);
});

it('X3d-4 uses a native read refusal’s typed reason even when its prose claims permission or a missing file', () => {
  for (const message of ['Permission denied; file not found.', 'The source changed.', 'No problem.']) {
    const error = { ok: false, tool_name: 'read_file', error_class: 'auth_or_permission',
      reason_code: 'workspace_input_capture_unavailable', message };
    const result = toolFailureForDisplay(`<tool_error>${JSON.stringify(error)}</tool_error>`, t, 'failed');
    expect(result).toBe('这次未能取得可读取的文件内容。');
  }
});
