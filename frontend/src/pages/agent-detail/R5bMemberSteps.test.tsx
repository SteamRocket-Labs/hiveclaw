// @vitest-environment jsdom
import { cleanup, fireEvent, render } from '@testing-library/react';
import { afterEach, expect, it, vi } from 'vitest';
import type { TFunction } from 'i18next';
import zh from '../../i18n/zh.json';
import { translateFromCatalog } from '../../test/i18nMock';
import RunDisclosureBlock from './RunDisclosureBlock';
import { toolStepLabel } from './toolStepLabel';

const t = (key: string, fallback?: string, values?: Record<string, unknown>) => translateFromCatalog(zh, key, fallback, values);
vi.mock('react-i18next', () => ({ useTranslation: () => ({ t: (key: string, fallback?: string, values?: Record<string, unknown>) =>
  translateFromCatalog(zh, key, fallback, values) }) }));
afterEach(cleanup);

it.each([
  ['save_memory', '保存记忆'], ['search_memory', '查找记忆'], ['load_memory', '读取记忆'],
  ['update_memory', '更新记忆'], ['retire_memory', '忘记记忆'],
  ['create_employee_from_request', '创建数字员工'], ['create_digital_employee', '创建数字员工'],
  ['preview_agent_blueprint', '预览数字员工'], ['unknown_new_tool', '工具'],
  ['constructor', '工具'], ['__proto__', '工具'], ['toString', '工具'],
])('gives %s a Chinese label even with a stored ellipsis', (name, label) => {
  expect(toolStepLabel(name, t as unknown as TFunction, '…')).toBe(label);
});

it.each(['…', '...', 'private_raw_label', '', 'constructor', '__proto__'])('uses a Chinese generic fallback for %s', sentinel => {
  expect(toolStepLabel(null, t as unknown as TFunction, sentinel)).toBe('工具');
});

it.each(['timeout', 'auth_or_permission', 'quota_or_billing', 'rate_limited', 'not_found', 'bad_arguments',
  'tool_execution_error', 'provider_unavailable', 'approval_required', 'cancelled', 'configuration_required',
  'unsupported_file_type', 'authority_context_unavailable', 'future_error_class', 'constructor'])(
  'shows one plain Chinese explanation for expanded %s steps', errorClass => {
    const raw = `❌ run_command failed.\n\nHint: worker state unknown.\n\n<tool_error>${JSON.stringify({
      ok: false, tool_name: 'run_command', error_class: errorClass,
      message: 'run_command exceeded the 120 second time limit.', provider: 'runtime',
      http_status: 403, retryable: false,
    })}</tool_error>`;
    const details = { toolName: 'run_command', result: raw, rawResult: raw, args: { command: 'PRIVATE_COMMAND' } };
    const { container } = render(<RunDisclosureBlock timeline={{ id: 'run', status: 'failed', steps: [{
      id: 'step', kind: 'command', title: 'Run command', status: 'failed', visibility: 'visible', details,
    }] }} />);
    fireEvent.click(container.querySelector('.run-step-toggle') as HTMLButtonElement);
    const explanation = container.querySelector('.run-step-body p');
    expect(explanation).toBeTruthy();
    expect(container.querySelectorAll('.run-step-body p')).toHaveLength(1);
    expect(explanation?.textContent).toMatch(/[一-鿿]/);
    expect(container.textContent).not.toMatch(/tool_error|run_command|worker|Hint|PRIVATE_COMMAND|http_status|403|[{}<>]/);
    if (errorClass === 'timeout') expect(explanation?.textContent).toBe('操作超过 120 秒没有完成，结果未知。');
    expect(details.result).toBe(raw);
  },
);

it('does not expose a malformed error envelope', () => {
  const { container } = render(<RunDisclosureBlock timeline={{ id: 'broken', status: 'failed', steps: [{
    id: 'step', kind: 'tool', title: '…', status: 'failed', visibility: 'visible',
    details: { result: '<tool_error>{BROKEN_PRIVATE_DIAGNOSTICS}</tool_error>' },
  }] }} />);
  fireEvent.click(container.querySelector('.run-step-toggle') as HTMLButtonElement);
  expect(container.textContent).toContain('操作未能完成');
  expect(container.textContent).not.toMatch(/BROKEN|tool_error|…/);
});

it('does not interpret a successful file body as a tool failure', () => {
  const { container } = render(<RunDisclosureBlock timeline={{ id: 'file', status: 'running', steps: [{
    id: 'step', kind: 'file', title: 'Read file', status: 'done', visibility: 'visible',
    details: { result: JSON.stringify({ ok: true, content: '<tool_error>{"error_class":"timeout"}</tool_error>' }) },
  }] }} />);
  fireEvent.click(container.querySelector('.run-tool-group-toggle') as HTMLElement);
  fireEvent.click(container.querySelector('.run-tool-history-toggle') as HTMLButtonElement);
  expect(container.textContent).not.toContain('操作等待超时');
  expect(container.textContent).not.toContain('操作未能完成');
});
