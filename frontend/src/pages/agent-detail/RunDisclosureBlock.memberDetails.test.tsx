// @vitest-environment jsdom
import { cleanup, fireEvent, render, screen } from '@testing-library/react';
import { afterEach, expect, it, vi } from 'vitest';

import zh from '../../i18n/zh.json';
import { translateFromCatalog } from '../../test/i18nMock';
import RunDisclosureBlock from './RunDisclosureBlock';

vi.mock('react-i18next', () => ({
  useTranslation: () => ({ t: (key: string, fallback?: string, values?: Record<string, unknown>) =>
    translateFromCatalog(zh, key, fallback, values) }),
}));

afterEach(cleanup);

it.each(['send_message_to_agent', 'save_memory', 'unknown_tool'])(
  'shows a member label for %s even after its details are expanded', (toolName) => {
    const details = { toolName, result: '{"private_field":"PRIVATE_RAW_RESULT"}', rawResult: 'PRIVATE_RAW_RESULT', meta: null };
    const { container } = render(<RunDisclosureBlock timeline={{ id: 'run', status: 'running', steps: [{
      id: 'step', kind: 'tool', title: 'Tool call', status: 'running', visibility: 'visible', details,
    }] }} />);
    fireEvent.click(screen.getByTestId('run-disclosure-tool-group-toggle'));
    const button = container.querySelector('.run-tool-history-toggle') as HTMLButtonElement;
    fireEvent.click(button);
    expect(button.getAttribute('aria-expanded')).toBe('true');
    expect(container.querySelector('.run-detail-pre')).toBeNull();
    expect(container.textContent).not.toMatch(/toolName|rawResult|PRIVATE_RAW_RESULT|send_message_to_agent|unknown_tool/);
    expect(container.textContent).toMatch(/工具|交给其他数字员工|保存记忆/);
    expect(container.querySelector('.run-tool-history-item')?.querySelector('p')).toBeNull();
  },
);

it('shows a plain-text failure reason once when a generic step is expanded', () => {
  const { container } = render(<RunDisclosureBlock timeline={{ id: 'failed-run', status: 'failed', steps: [{
    id: 'failed-tool', kind: 'tool', title: 'Working', status: 'failed', visibility: 'visible',
    details: { toolName: 'send_message_to_agent', args: { agent_id: 'private-id' },
      result: '对方数字员工暂不可用', rawResult: { internal_error: 'private-error' } },
  }] }} />);
  const row = container.querySelector('.run-step-toggle') as HTMLButtonElement;
  expect(container.textContent).not.toContain('对方数字员工暂不可用');
  fireEvent.click(row);
  expect(screen.getAllByText('对方数字员工暂不可用')).toHaveLength(1);
  expect(container.querySelectorAll('p')).toHaveLength(1);
  expect(container.textContent).not.toMatch(/send_message_to_agent|agent_id|rawResult|internal_error|private-/);
});

it('keeps the dedicated command output renderer', () => {
  render(<RunDisclosureBlock timeline={{ id: 'command-run', status: 'running', steps: [{
    id: 'cmd', kind: 'command', title: 'Run command', status: 'done', visibility: 'visible',
    details: { command: 'wc -l report.md', output: '106', exit_code: 0 },
  }] }} />);
  fireEvent.click(screen.getByRole('button', { name: /运行命令/ }));
  expect(screen.getByText('106')).toBeTruthy();
});

it('does not turn a generic tool payload into command output', () => {
  const { container } = render(<RunDisclosureBlock timeline={{ id: 'generic', status: 'running', steps: [{
    id: 'tool', kind: 'tool', title: 'Working', status: 'running', visibility: 'visible',
    details: { toolName: 'unknown_tool', output: 'PRIVATE_OUTPUT', command: 'PRIVATE_COMMAND' },
  }] }} />);
  fireEvent.click(screen.getByTestId('run-disclosure-tool-group-toggle'));
  expect(container.querySelector('.session-tui-exec-output')).toBeNull();
  fireEvent.click(container.querySelector('.run-tool-history-toggle') as HTMLButtonElement);
  expect(container.textContent).not.toMatch(/PRIVATE_OUTPUT|PRIVATE_COMMAND|unknown_tool/);
});
