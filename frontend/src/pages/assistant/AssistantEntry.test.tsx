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
  it('shows server identity and does not preselect an employee', () => {
    const markup = render(
      <MemoryRouter>
        <AssistantPanel
          payload={binding({
            state: 'active',
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
          onFirstWork={vi.fn()}
        />
      </MemoryRouter>,
    );

    expect(screen.getByRole('status').textContent).toContain('Ledger');
    expect(screen.getByRole('status').textContent).toContain('Approved model');
    expect(screen.getByRole('status').textContent).toContain('assistant.notReady');
    expect(screen.getByRole('status').textContent).toContain('assistant.workerUnavailable');
    expect(markup.container.querySelector('input[type="radio"]:checked')).toBeNull();
    expect(screen.getByRole('link', { name: 'assistant.team' }).getAttribute('href')).toBe('/team');
    expect(screen.getByRole('link', { name: 'assistant.messages' }).getAttribute('href')).toBe('/messages');
    expect(screen.getByRole('link', { name: 'assistant.home' }).getAttribute('href')).toBe('/home');
    expect(screen.getByRole('link', { name: 'assistant.employees' }).getAttribute('href')).toBe('/agents');
  });

  it('submits explicit first work once and does not open an assistant while rendering', async () => {
    const onOpen = vi.fn();
    let release: (() => void) | undefined;
    const onFirstWork = vi.fn(() => new Promise<void>((resolve) => {
      release = resolve;
    }));
    render(
      <MemoryRouter>
        <AssistantPanel
          payload={binding({
            state: 'active',
            agent: {
              id: 'agent-1', name: 'My Assistant', role_description: '', status: 'idle',
              primary_model_id: 'model-1', model_label: 'Approved model',
            },
            readiness: { ready: true, worker: 'available', model: 'ok', workspace: 'ok', blockers: [] },
            next_actions: ['continue', 'first_work', 'home'],
            entry: { kind: 'employee', path: '/agents/agent-1', agent_id: 'agent-1', session_id: null },
          })}
          loading={false}
          onOpen={onOpen}
          onSelect={vi.fn()}
          onRetry={vi.fn()}
          onFirstWork={onFirstWork}
        />
      </MemoryRouter>,
    );

    expect(onOpen).not.toHaveBeenCalled();
    fireEvent.change(screen.getByLabelText('assistant.firstWork'), { target: { value: 'Write the weekly note' } });
    const send = screen.getByRole('button', { name: 'assistant.send' });
    fireEvent.click(send);
    fireEvent.click(send);
    expect(onFirstWork).toHaveBeenCalledTimes(1);
    await act(async () => release?.());
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


  it('keeps the first work and shows its refusal', async () => {
    vi.stubGlobal('fetch', vi.fn(async (_url: string, init?: RequestInit) => new Response(JSON.stringify(
      init?.method === 'POST' ? { detail: { message: 'Execution is denied' } } : workingBinding(),
    ), { status: init?.method === 'POST' ? 403 : 200, headers: { 'content-type': 'application/json' } })));
    const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    render(<MemoryRouter><QueryClientProvider client={client}><AssistantEntry mode="assistant" /></QueryClientProvider></MemoryRouter>);
    const draft = await screen.findByLabelText('assistant.firstWork');
    fireEvent.change(draft, { target: { value: 'Keep this request' } });
    fireEvent.click(screen.getByRole('button', { name: 'assistant.send' }));
    expect((await screen.findByRole('alert')).textContent).toContain('Execution is denied');
    expect(draft).toHaveProperty('value', 'Keep this request');
    expect(screen.getByRole('button', { name: 'assistant.send' })).toHaveProperty('disabled', false);
  });

  it('does not navigate to old company work after the first work response arrives', async () => {
    useAuthStore.setState({ user: { id: 'user-1', role: 'platform_admin', tenant_id: null } as never });
    let finish: ((response: Response) => void) | undefined;
    vi.stubGlobal('fetch', vi.fn(async (_url: string, init?: RequestInit) => {
      if (init?.method === 'POST') return new Promise<Response>((resolve) => { finish = resolve; });
      const tenant = new Headers(init?.headers).get('X-Tenant-Id') || '';
      return new Response(JSON.stringify(workingBinding(tenant)), { status: 200, headers: { 'content-type': 'application/json' } });
    }));
    const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    render(<MemoryRouter initialEntries={['/assistant']}><QueryClientProvider client={client}><Routes>
      <Route path="/assistant" element={<AssistantEntry mode="assistant" />} />
      <Route path="/agents/:id/sessions/:sessionId" element={<LocationProbe />} />
    </Routes></QueryClientProvider></MemoryRouter>);
    fireEvent.change(await screen.findByLabelText('assistant.firstWork'), { target: { value: 'Work in company one' } });
    fireEvent.click(screen.getByRole('button', { name: 'assistant.send' }));
    await waitFor(() => expect(finish).toBeTypeOf('function'));
    act(() => {
      localStorage.setItem('current_tenant_id', 'tenant-2');
      window.dispatchEvent(new StorageEvent('storage', { key: 'current_tenant_id', newValue: 'tenant-2' }));
    });
    await act(async () => finish?.(new Response(JSON.stringify({ session: { id: 'company-one-session' }, run: {} }),
      { status: 201, headers: { 'content-type': 'application/json' } })));
    expect(screen.queryByTestId('location')).toBeNull();
    await screen.findByText('tenant-2 assistant');
  });

});
