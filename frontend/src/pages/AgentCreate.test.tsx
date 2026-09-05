// @vitest-environment jsdom

import { renderToStaticMarkup } from 'react-dom/server';
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { ApiError } from '../api/core';

const { createSession, getHrAgent, listCompanies } = vi.hoisted(() => ({
  createSession: vi.fn(),
  getHrAgent: vi.fn(),
  listCompanies: vi.fn(),
}));

const auth = vi.hoisted(() => ({ user: null as any }));

vi.mock('react-i18next', () => ({
  useTranslation: () => ({
    t: (key: string, fallback?: string, opts?: Record<string, unknown>) => {
      const template = fallback ?? key;
      if (!opts) return template;
      return template.replace(/\{\{(\w+)\}\}/g, (match, name) =>
        (name in opts ? String(opts[name]) : match));
    },
  }),
}));

vi.mock('react-router-dom', () => ({
  useNavigate: () => vi.fn(),
}));

vi.mock('../stores', () => ({
  useAuthStore: (selector?: any) => {
    const state = { user: auth.user };
    return typeof selector === 'function' ? selector(state) : state;
  },
}));

vi.mock('../api/domains/admin', () => ({
  adminApi: {
    listCompanies,
  },
  isActiveCompany: (company: { is_active?: boolean }) => company.is_active !== false,
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
    listCompanies.mockReset();
    auth.user = null;
    localStorage.clear();
  });

  afterEach(() => cleanup());

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

  it('offers an explicit company selector to a platform administrator without a selected company', async () => {
    auth.user = { id: 'platform-1', role: 'platform_admin', tenant_id: null };
    listCompanies.mockResolvedValue([
      { id: 'tenant-a', name: 'Company A', is_active: true },
      { id: 'tenant-b', name: 'Company B', is_active: false },
    ]);
    getHrAgent.mockResolvedValue({ id: 'hr-agent' });
    createSession.mockResolvedValue({ id: 'new-session' });

    render(<AgentCreate />);

    expect(await screen.findByText('Select a company first')).toBeTruthy();
    expect(screen.queryByRole('button', { name: /Use HR Agent for guided creation/i })).toBeNull();
    const select = await screen.findByRole('combobox', { name: 'Company' });
    // A disabled company is never offered as a valid selection.
    expect(screen.queryByRole('option', { name: 'Company B' })).toBeNull();

    fireEvent.change(select, { target: { value: 'tenant-a' } });
    fireEvent.click(screen.getByRole('button', { name: /Continue with this company/i }));

    expect(localStorage.getItem('current_tenant_id')).toBe('tenant-a');
    await waitFor(() => expect(getHrAgent).toHaveBeenCalledTimes(1));
  });

  it('surfaces the company selector on a typed selection error instead of a generic retry', async () => {
    auth.user = { id: 'platform-1', role: 'platform_admin', tenant_id: null };
    localStorage.setItem('current_tenant_id', 'tenant-stale');
    listCompanies.mockResolvedValue([{ id: 'tenant-a', name: 'Company A', is_active: true }]);
    getHrAgent.mockRejectedValueOnce(new ApiError(400, 'No tenant assigned'));

    render(<AgentCreate />);
    fireEvent.click(screen.getByRole('button', { name: /Use HR Agent for guided creation/i }));

    expect(await screen.findByText('Select a company first')).toBeTruthy();
    expect(screen.queryByRole('alert')).toBeNull();
  });

  it('keeps the generic HR error for a member-facing failure, never a company selector', async () => {
    auth.user = { id: 'member-1', role: 'member', tenant_id: 'tenant-1' };
    localStorage.setItem('current_tenant_id', 'tenant-1');
    getHrAgent.mockRejectedValueOnce(new ApiError(400, 'No tenant assigned'));

    render(<AgentCreate />);
    fireEvent.click(screen.getByRole('button', { name: /Use HR Agent for guided creation/i }));

    const alert = await screen.findByRole('alert');
    expect(alert.textContent).toContain('Could not open HR Agent');
    expect(screen.queryByText('Select a company first')).toBeNull();
    expect(listCompanies).not.toHaveBeenCalled();
  });

  it('keeps an HR session authorization denial as a truthful error, never company re-selection', async () => {
    auth.user = { id: 'platform-1', role: 'platform_admin', tenant_id: 'tenant-1' };
    localStorage.setItem('current_tenant_id', 'tenant-1');
    getHrAgent.mockResolvedValueOnce({ id: 'hr-agent' });
    createSession.mockRejectedValueOnce(new ApiError(403, 'No access to this agent'));

    render(<AgentCreate />);
    fireEvent.click(screen.getByRole('button', { name: /Use HR Agent for guided creation/i }));

    const alert = await screen.findByRole('alert');
    expect(alert.textContent).toContain('Could not open HR Agent');
    expect(alert.textContent).toContain('No access to this agent');
    expect(screen.queryByText('Select a company first')).toBeNull();
    expect(listCompanies).not.toHaveBeenCalled();
  });

  it('after choosing a company, an HR session denial stays an error instead of looping back to selection', async () => {
    auth.user = { id: 'platform-1', role: 'platform_admin', tenant_id: null };
    listCompanies.mockResolvedValue([{ id: 'tenant-a', name: 'Company A', is_active: true }]);
    getHrAgent.mockResolvedValue({ id: 'hr-agent' });
    createSession.mockRejectedValue(new ApiError(403, 'No access to this agent'));

    render(<AgentCreate />);
    const select = await screen.findByRole('combobox', { name: 'Company' });
    fireEvent.change(select, { target: { value: 'tenant-a' } });
    fireEvent.click(screen.getByRole('button', { name: /Continue with this company/i }));

    const alert = await screen.findByRole('alert');
    expect(alert.textContent).toContain('No access to this agent');
    expect(screen.queryByText('Select a company first')).toBeNull();
    expect(listCompanies).toHaveBeenCalledTimes(1);
  });
});
