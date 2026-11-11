// @vitest-environment jsdom
import { cleanup, fireEvent, render } from '@testing-library/react';
import { afterEach, expect, it, vi } from 'vitest';
import zh from '../../i18n/zh.json';
import { translateFromCatalog } from '../../test/i18nMock';
import { createSessionEventStore, type SessionEventV2 } from '../session-workbench/sessionEventStore';
import { buildRunTimelineFromMessages } from './chatDisclosureReducer';
import type { ChatTranscriptEventPayload } from './chatRuntime';
import { consumeSessionEnvelope, projectSessionEventStoreToMessages } from './sessionEventConsumer';
import RunDisclosureBlock from './RunDisclosureBlock';

vi.mock('react-i18next', () => ({ useTranslation: () => ({
  t: (key: string, fallback?: string, values?: Record<string, unknown>) => translateFromCatalog(zh, key, fallback, values),
}) }));
afterEach(cleanup);

it.each(['denied', 'exception', 'cancelled'])('keeps failed command calls failed (%s)', reason => {
  const { container } = render(<RunDisclosureBlock timeline={{ id: 'failed-run', status: 'failed', steps: [
    { id: reason, kind: 'command', title: 'Run command', status: 'failed', visibility: 'visible',
      details: { toolName: 'run_command', rawResult: `PRIVATE_${reason}`, executionResult: { exit_code: null, timed_out: false } } },
  ] }} />);
  fireEvent.click(container.querySelector('.run-step-toggle')!);
  expect(container.querySelector('p')?.textContent).toBe('运行命令。失败');
  expect(container.textContent).not.toContain('PRIVATE_');
});

it.each([
  [{ exit_code: 0, timed_out: false }, '75 report.md', '成功'],
  [{ exit_code: 7, timed_out: false }, 'PRIVATE_STDERR', '失败'],
  [{ exit_code: null, timed_out: true }, 'PRIVATE_TIMEOUT', '结果尚未确认。'],
  [{ exit_code: null, timed_out: false }, 'PRIVATE_LOST_RESULT', '结果尚未确认。'],
  [undefined, 'PRIVATE_LEGACY_RESULT', '结果尚未确认。'],
  [{ exit_code: 0, timed_out: false }, 'Exit code: 7\nbash: line 1: fake: command not found', '成功'],
] as const)('renders canonical command facts %j without displaying stdout', (execution, stdout, status) => {
  let store = createSessionEventStore();
  const raw = `💻 Command: wc -l report.md\n\n📤 Output:\n${stdout}`;
  const facts = execution ? { code_execution_result: execution } : {};
  const time = '2026-11-10T12:00:00Z';
  const events: SessionEventV2[] = [
    { item_id: 'call', item_kind: 'tool_call', lifecycle: 'started', payload: { tool_name: 'run_command', args: { command: 'PRIVATE_COMMAND' } } },
    { item_id: 'call', item_kind: 'tool_call', lifecycle: 'completed', payload: { outcome: 'success', ...facts } },
    { item_id: 'result', item_kind: 'tool_result', lifecycle: 'completed', payload: { outcome: 'success', content: raw, ...facts } },
  ].map((part, index) => ({ ...part, schema: 'hive.session_event', schema_version: 2,
    event_id: `event-${index}`, sequence: index + 1, ordinal: index === 1 ? 1 : 0, tenant_id: 'tenant',
    invocation_id: 'invocation', provider_tool_use_id: 'provider-call',
    kind: `${part.item_kind}.${part.lifecycle}`, payload_schema: `hive.session.payload.${part.item_kind}.${part.lifecycle}.v2`,
    scope: { level: 'round', session_id: 'session', thread_id: 'session', turn_id: 'turn', run_id: 'run', round_id: 'round' },
    actor: { type: 'tool' }, visibility: { audience: 'direct_user' }, occurred_at: time, persisted_at: time,
  }));
  for (const envelope of events) store = consumeSessionEnvelope(envelope as unknown as ChatTranscriptEventPayload, store, 0).store!;
  const messages = projectSessionEventStoreToMessages(store);
  const before = JSON.stringify(messages);
  const { container } = render(<RunDisclosureBlock timeline={buildRunTimelineFromMessages(messages)} />);
  for (const selector of ['.run-disclosure-header', '.run-tool-group-toggle', '.run-step-toggle', '.run-tool-history-toggle']) {
    for (const toggle of container.querySelectorAll<HTMLElement>(selector)) fireEvent.click(toggle);
  }
  expect(container.querySelector('p')?.textContent).toBe(`运行命令。${status}`);
  expect(container.textContent).not.toContain(stdout);
  expect(container.textContent).not.toMatch(/PRIVATE_|wc -l|exit_code|timed_out/);
  expect(JSON.stringify(messages)).toBe(before);
  expect(messages.find(message => message.toolName === 'run_command')?.toolRawResult).toBe(raw);
});
