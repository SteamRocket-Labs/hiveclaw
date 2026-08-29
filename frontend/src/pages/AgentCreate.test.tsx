// @vitest-environment jsdom

import { renderToStaticMarkup } from 'react-dom/server';
import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { ApiError } from '../api/core';

const { createSession, getHrAgent } = vi.hoisted(() => ({
  createSession: vi.fn(),
  getHrAgent: vi.fn(),
}));

vi.mock('react-i18next', () => ({
  useTranslation: () => ({
    t: (key: string, fallback?: string) => fallback ?? key,
  }),
}));

vi.mock('react-router-dom', () => ({
  useNavigate: () => vi.fn(),
}));

vi.mock('../api/domains/agents', () => ({
  agentApi: {
    getHrAgent,
  },
}));

vi.mock('../api/domains/chat', () => ({
  chatApi: {
    createSession,
  },
}));

import AgentCreate from './AgentCreate';

describe('AgentCreate HR-only creation path', () => {
  beforeEach(() => {
    createSession.mockReset();
    getHrAgent.mockReset();
  });

  it('exposes only the HR Agent creation path', () => {
    const markup = renderToStaticMarkup(<AgentCreate />);

    expect(markup).toContain('Create digital employee');
    expect(markup).toContain('HR Agent');
    expect(markup).toContain('Capability governance');
    expect(markup).toContain('Use HR Agent for guided creation');
    expect(markup).not.toContain('Creation method');
    expect(markup).not.toContain('Blank employee');
    expect(markup).not.toContain('Company template');
    expect(markup).not.toContain('Natural language assistant');
    expect(markup).not.toContain('Employee identity');
    expect(markup).not.toContain('Create employee');
  });

  it('keeps temporary service failures user-readable and retryable', async () => {
    getHrAgent.mockRejectedValueOnce(new ApiError(502, 'HTTP 502'));

    render(<AgentCreate />);
    fireEvent.click(screen.getByRole('button', { name: /Use HR Agent for guided creation/i }));

    const alert = await screen.findByRole('alert');
    expect(alert.textContent).toContain('HR Agent is temporarily unavailable');
    expect(alert.textContent).toContain('Nothing has been submitted');
    expect(alert.textContent).not.toContain('HTTP 502');
    const retryButton = screen.getByRole('button', { name: /Use HR Agent for guided creation/i });
    expect((retryButton as HTMLButtonElement).disabled).toBe(false);

    getHrAgent.mockResolvedValueOnce({ id: 'hr-agent' });
    createSession.mockResolvedValueOnce({ id: 'new-session' });
    fireEvent.click(retryButton);
    await waitFor(() => expect(getHrAgent).toHaveBeenCalledTimes(2));
  });
});
