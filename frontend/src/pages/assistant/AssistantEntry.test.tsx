// @vitest-environment jsdom
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { MemoryRouter, Route, Routes, useLocation } from 'react-router-dom';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import AssistantEntry from './AssistantEntry';
import AssistantPanel from './AssistantPanel';
import { assistantScopeStillCurrent, entryShouldNavigate } from './assistantScope';
import type { AssistantBinding } from '../../api/domains/assistant';
import { useAuthStore } from '../../stores';

vi.mock('react-i18next', () => ({
  useTranslation: () => ({ t: (key: string) => key }),
}));

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

function binding(overrides: Partial<AssistantBinding> = {}): AssistantBinding {
  return {
    schema: 'hive.assistant_binding.v1',
    state: 'unbound',
    scope: { user_id: 'user-1', tenant_id: 'tenant-1' },
    agent: null,
    readiness: { ready: false, worker: 'available', model: 'unconfigured', workspace: 'missing', blockers: ['unbound'] },
    next_actions: ['open', 'select', 'home', 'employees', 'team', 'messages'],
    entry: { kind: 'setup', path: '/assistant', agent_id: null, session_id: null },
    candidates: [
      { id: 'agent-root', name: 'Root', role_description: '', selected: false },
      { id: 'agent-second', name: 'Ledger', role_description: '', selected: false },
    ],
    binding_grants_access: false,
    applied: false,
    created: false,
    legacy_provenance: 'not_established',
    history_retained: true,
    prior_agent_id: null,
    message: 'My Assistant is not bound yet.',
    ...overrides,
  };
}

function workingBinding(tenant = 'tenant-1'): AssistantBinding {
  return binding({
    state: 'active', scope: { user_id: 'user-1', tenant_id: tenant },
    agent: { id: `agent-${tenant}`, name: `${tenant} assistant`, role_description: '', status: 'idle', primary_model_id: 'approved', model_label: 'Approved' },
    readiness: { ready: true, worker: 'available', model: 'ok', workspace: 'ok', blockers: [] },
    next_actions: ['continue', 'first_work'],
    entry: { kind: 'employee', path: `/agents/agent-${tenant}`, agent_id: `agent-${tenant}`, session_id: null },
  });
}

function LocationProbe() {
  const location = useLocation();
  return <div data-testid="location">{location.pathname}</div>;
}

describe('assistant scope', () => {
  it('discards a response after the company or account changes', () => {
    expect(assistantScopeStillCurrent({
      startedUserId: 'user-1',
      startedTenantId: 'tenant-1',
      currentUserId: 'user-1',
      currentTenantId: 'tenant-2',
      payloadUserId: 'user-1',
      payloadTenantId: 'tenant-1',
    })).toBe(false);
    expect(entryShouldNavigate({ kind: 'setup', path: '/assistant' })).toBe(false);
    expect(entryShouldNavigate({ kind: 'session', path: '/agents/a/sessions/b' })).toBe(true);
  });
});

describe('AssistantPanel recovery', () => {
  it.each([
    ['ok', 'Company approved model'],
    ['forbidden', null],
    ['unconfigured', null],
  ])('keeps the fallback panel free of readiness diagnostics: %s', (model, label) => {
    const onOpen = vi.fn();
    render(<MemoryRouter><AssistantPanel
      payload={binding({ readiness: {
        ready: false, worker: 'available', model, model_label: label, workspace: 'missing', blockers: [],
      } })}
      loading={false} onOpen={onOpen} onSelect={vi.fn()} onRetry={vi.fn()}
    /></MemoryRouter>);
    expect(screen.getByText('assistant.empty')).toBeTruthy();
    expect(screen.queryByText('assistant.model')).toBeNull();
    expect(screen.queryByText('assistant.readiness')).toBeNull();
    expect(screen.queryByText('assistant.ready')).toBeNull();
    expect(onOpen).not.toHaveBeenCalled();
    expect(screen.getByRole('button', { name: 'assistant.open' })).toHaveProperty('disabled', false);
  });

  it('shows server identity without diagnostics links and does not preselect an employee', () => {
    const markup = render(
      <MemoryRouter>
        <AssistantPanel
          payload={binding({
            state: 'provision_incomplete',
            agent: {
              id: 'agent-1', name: 'Ledger', role_description: 'Research', status: 'idle',
              primary_model_id: 'model-1', model_label: 'Approved model',
            },
            readiness: { ready: false, worker: 'unavailable', model: 'ok', workspace: 'missing', blockers: ['workspace_missing', 'worker_unavailable'] },
            next_actions: ['continue', 'first_work', 'home', 'employees', 'team', 'messages'],
            entry: { kind: 'employee', path: '/agents/agent-1', agent_id: 'agent-1', session_id: null },
          })}
          loading={false}
          onOpen={vi.fn()}
          onSelect={vi.fn()}
          onRetry={vi.fn()}
        />
      </MemoryRouter>,
    );

    expect(screen.getByRole('status').textContent).toContain('Ledger');
    expect(screen.getByRole('status').textContent).toContain('assistant.pending');
    expect(screen.getByRole('status').textContent).not.toContain('Approved model');
    expect(screen.getByRole('status').textContent).not.toContain('assistant.notReady');
    expect(screen.getByRole('status').textContent).not.toContain('assistant.workerUnavailable');
    expect(markup.container.querySelector('input[type="radio"]:checked')).toBeNull();
    // The quick-links nav block is gone from the fallback panel (P3).
    expect(screen.queryByRole('link', { name: 'assistant.team' })).toBeNull();
    expect(screen.queryByRole('link', { name: 'assistant.messages' })).toBeNull();
    expect(screen.queryByRole('link', { name: 'assistant.home' })).toBeNull();
    expect(screen.queryByRole('link', { name: 'assistant.employees' })).toBeNull();
  });
});

describe('AssistantEntry synthetic transport', () => {
  beforeEach(() => {
    localStorage.setItem('token', 'synthetic-token');
    localStorage.setItem('current_tenant_id', 'tenant-1');
    useAuthStore.setState({
      user: { id: 'user-1', tenant_id: 'tenant-1', role: 'member' } as never,
      token: 'synthetic-token',
    });
  });

  it('keeps setup on the page and navigates only for a real session (synthetic transport)', async () => {
    const calls: string[] = [];
    vi.stubGlobal('fetch', vi.fn(async (url: string, init?: RequestInit) => {
      calls.push(`${init?.method || 'GET'} ${url}`);
      const payload = binding();
      return new Response(JSON.stringify(payload), { status: 200, headers: { 'content-type': 'application/json' } });
    }));
    const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    render(
      <MemoryRouter initialEntries={['/']}>
        <QueryClientProvider client={client}>
          <Routes>
            <Route path="/" element={<AssistantEntry mode="entry" />} />
            <Route path="/agents/:id/sessions/:sessionId" element={<LocationProbe />} />
          </Routes>
        </QueryClientProvider>
      </MemoryRouter>,
    );

    expect(await screen.findByText('assistant.empty')).toBeTruthy();
    expect(calls).toEqual(['GET /api/assistant?expected_user_id=user-1&expected_tenant_id=tenant-1']);
    expect(screen.queryByTestId('location')).toBeNull();
  });

  it('opens the current session returned by GET and does not create one (synthetic transport)', async () => {
    vi.stubGlobal('fetch', vi.fn(async () => new Response(JSON.stringify(binding({
      state: 'active',
      agent: {
        id: 'agent-1', name: 'My Assistant', role_description: '', status: 'idle',
        primary_model_id: 'model-1', model_label: 'Approved model',
      },
      readiness: { ready: true, worker: 'available', model: 'ok', workspace: 'ok', blockers: [] },
      entry: { kind: 'session', path: '/agents/agent-1/sessions/session-1', agent_id: 'agent-1', session_id: 'session-1' },
    })), { status: 200, headers: { 'content-type': 'application/json' } })));
    const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    render(
      <MemoryRouter initialEntries={['/']}>
        <QueryClientProvider client={client}>
          <Routes>
            <Route path="/" element={<AssistantEntry mode="entry" />} />
            <Route path="/agents/:id/sessions/:sessionId" element={<LocationProbe />} />
          </Routes>
        </QueryClientProvider>
      </MemoryRouter>,
    );

    await waitFor(() => expect(screen.getByTestId('location').textContent).toBe('/agents/agent-1/sessions/session-1'));
  });

  it('shows a refused opening without losing the recovery controls', async () => {
    vi.stubGlobal('fetch', vi.fn(async (_url: string, init?: RequestInit) => new Response(JSON.stringify(
      init?.method === 'POST' ? { detail: { message: 'Workspace could not be prepared' } } : binding(),
    ), { status: init?.method === 'POST' ? 503 : 200, headers: { 'content-type': 'application/json' } })));
    const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    render(<MemoryRouter><QueryClientProvider client={client}><AssistantEntry mode="assistant" /></QueryClientProvider></MemoryRouter>);
    await screen.findByText('assistant.empty');
    fireEvent.click(screen.getByRole('button', { name: 'assistant.open' }));
    expect((await screen.findByRole('alert')).textContent).toContain('Workspace could not be prepared');
    expect(screen.getByRole('button', { name: 'assistant.open' })).toHaveProperty('disabled', false);
  });

  it('changes the company through the actual selector event before consuming more detail', async () => {
    useAuthStore.setState({ user: { id: 'user-1', role: 'platform_admin', tenant_id: null } as never });
    const companies: string[] = [];
    vi.stubGlobal('fetch', vi.fn(async (_url: string, init?: RequestInit) => {
      const tenant = new Headers(init?.headers).get('X-Tenant-Id') || '';
      companies.push(tenant);
      return new Response(JSON.stringify(binding({
        scope: { user_id: 'user-1', tenant_id: tenant },
        agent: { id: `agent-${tenant}`, name: `${tenant} private employee`, role_description: '', status: 'idle', primary_model_id: null, model_label: null },
      })), { status: 200, headers: { 'content-type': 'application/json' } });
    }));
    const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    render(<MemoryRouter><QueryClientProvider client={client}><AssistantEntry mode="assistant" /></QueryClientProvider></MemoryRouter>);
    await screen.findByText('tenant-1 private employee');
    act(() => {
      localStorage.setItem('current_tenant_id', 'tenant-2');
      window.dispatchEvent(new StorageEvent('storage', { key: 'current_tenant_id', newValue: 'tenant-2' }));
    });
    await screen.findByText('tenant-2 private employee');
    expect(screen.queryByText('tenant-1 private employee')).toBeNull();
    expect(companies).toEqual(['tenant-1', 'tenant-2']);
  });

  it('retries incomplete provisioning through its write action', async () => {
    const requests: string[] = [];
    vi.stubGlobal('fetch', vi.fn(async (url: string, init?: RequestInit) => {
      requests.push(`${init?.method || 'GET'} ${url}`);
      return new Response(JSON.stringify(binding({ state: 'provision_incomplete', next_actions: ['retry'] })),
        { status: 200, headers: { 'content-type': 'application/json' } });
    }));
    const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    render(<MemoryRouter><QueryClientProvider client={client}><AssistantEntry mode="assistant" /></QueryClientProvider></MemoryRouter>);
    await screen.findByText('assistant.pending');
    fireEvent.click(screen.getByRole('button', { name: 'assistant.retry' }));
    await waitFor(() => expect(requests.some((request) => request.startsWith('POST /api/assistant/open'))).toBe(true));
  });


  it('opens the assistant chat page when the binding is active but has no conversation yet', async () => {
    vi.stubGlobal('fetch', vi.fn(async () => new Response(JSON.stringify(binding({
      state: 'active',
      agent: {
        id: 'agent-1', name: 'My Assistant', role_description: '', status: 'idle',
        primary_model_id: 'model-1', model_label: 'Approved model',
      },
      readiness: { ready: true, worker: 'available', model: 'ok', workspace: 'ok', blockers: [] },
      entry: { kind: 'setup', path: '/assistant', agent_id: null, session_id: null },
    })), { status: 200, headers: { 'content-type': 'application/json' } })));
    const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    render(
      <MemoryRouter initialEntries={['/']}>
        <QueryClientProvider client={client}>
          <Routes>
            <Route path="/" element={<AssistantEntry mode="entry" />} />
            <Route path="/agents/:id" element={<LocationProbe />} />
          </Routes>
        </QueryClientProvider>
      </MemoryRouter>,
    );

    await waitFor(() => expect(screen.getByTestId('location').textContent).toBe('/agents/agent-1'));
  });

  it('keeps an unresolvable assistant on the minimal panel without a first-work form', async () => {
    vi.stubGlobal('fetch', vi.fn(async () => new Response(JSON.stringify(workingBinding()), { status: 503, headers: { 'content-type': 'application/json' } })));
    const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    render(<MemoryRouter><QueryClientProvider client={client}><AssistantEntry mode="assistant" /></QueryClientProvider></MemoryRouter>);
    expect(await screen.findByText('assistant.unavailable')).toBeTruthy();
    expect(screen.queryByLabelText('assistant.firstWork')).toBeNull();
    expect(screen.getByRole('button', { name: 'assistant.retry' })).toHaveProperty('disabled', false);
  });

});
