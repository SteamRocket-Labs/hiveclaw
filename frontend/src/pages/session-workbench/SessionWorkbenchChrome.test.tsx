// @vitest-environment jsdom
import React from 'react';
import { cleanup, fireEvent, render, screen } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import { afterEach, expect, it, vi } from 'vitest';
import { SessionWorkbenchHeader } from './SessionWorkbenchChrome';
import { buildThreadTimeline } from './timelineModel';

vi.mock('react-i18next', () => ({ useTranslation: () => ({
  t: (_key: string, fallback: string) => fallback, i18n: { language: 'en' },
}) }));
afterEach(cleanup);

it.each([
  ['header', 'Use [REDACTED_SECRET] safely'], ['history', 'Use [REDACTED_SECRET] safely'],
  ['header', 'Prepare the meeting notes'], ['history', 'Prepare the meeting notes'],
])('renders the %s title by text: %s', (surface, title) => {
  const session = { id: 'session-1', title };
  const model = buildThreadTimeline({ activeSession: session, messages: [], isWaiting: false, isStreaming: false }).header;
  const { container } = render(<MemoryRouter><SessionWorkbenchHeader model={model}
    history={{ agentId: 'agent-1', sessions: [session], activeSessionId: session.id }} /></MemoryRouter>);
  fireEvent.click(screen.getByRole('button', { name: 'History' }));
  const titles = container.querySelectorAll(surface === 'header' ? '.swb-header-title' : '.swb-history-item-title');
  expect(titles).toHaveLength(1);
  for (const element of titles) {
    expect(element.textContent).toBe(title.replace('[REDACTED_SECRET]', 'Hidden key'));
    expect(element.querySelectorAll('.hidden-credential-mark')).toHaveLength(title.includes('[REDACTED_SECRET]') ? 1 : 0);
    expect(element.textContent).not.toContain('[REDACTED_SECRET]');
  }
});
