// @vitest-environment jsdom
import { readFileSync } from 'node:fs';
import { cleanup, fireEvent, render } from '@testing-library/react';
import { afterEach, expect, it, vi } from 'vitest';
import zh from '../../i18n/zh.json';
import { translateFromCatalog } from '../../test/i18nMock';
import { createSessionEventStore } from '../session-workbench/sessionEventStore';
import { buildRunTimelineFromMessages } from './chatDisclosureReducer';
import type { ChatTranscriptEventPayload } from './chatRuntime';
import { consumeSessionEnvelope, projectSessionEventStoreToMessages } from './sessionEventConsumer';
import RunDisclosureBlock from './RunDisclosureBlock';

vi.mock('react-i18next', () => ({ useTranslation: () => ({
  t: (key: string, fallback?: string, values?: Record<string, unknown>) => translateFromCatalog(zh, key, fallback, values),
}) }));
afterEach(cleanup);

it.skipIf(!process.env.Q3A_COMMAND_EVENTS)('projects the background command member API response into its real row', () => {
  const fixture = JSON.parse(readFileSync(process.env.Q3A_COMMAND_EVENTS!, 'utf8')) as {
    events: ChatTranscriptEventPayload[];
    expected: { exit_code: number | null; timed_out: boolean };
    run_id: string;
  };
  let store = createSessionEventStore();
  for (const envelope of fixture.events) store = consumeSessionEnvelope(envelope, store, 0).store!;
  const messages = projectSessionEventStoreToMessages(store);
  const timeline = buildRunTimelineFromMessages(messages);
  expect(timeline).not.toBeNull();
  const commands = timeline!.steps.filter(step => step.kind === 'command');
  expect(commands).toHaveLength(1);
  expect(commands[0].status).toBe('failed');
  expect(commands[0].details?.executionResult).toEqual(fixture.expected);
  const { container } = render(<RunDisclosureBlock timeline={timeline} />);
  for (const selector of ['.run-disclosure-header', '.run-tool-group-toggle', '.run-step-toggle', '.run-tool-history-toggle']) {
    for (const toggle of container.querySelectorAll<HTMLElement>(selector)) {
      if (toggle.getAttribute('aria-expanded') !== 'true') fireEvent.click(toggle);
    }
  }
  const outcome = fixture.expected.timed_out ? '结果尚未确认。' : '失败';
  expect(container.textContent).toContain(`运行命令。${outcome}`);
  expect(container.textContent).not.toContain('Q3A_COMMAND_STDOUT');
  expect(container.textContent).not.toMatch(/exit_code|timed_out|exec sleep/);
});
