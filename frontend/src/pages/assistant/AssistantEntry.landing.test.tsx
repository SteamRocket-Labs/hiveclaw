// @vitest-environment jsdom
import { StrictMode } from 'react';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { MemoryRouter, Route, Routes, useLocation } from 'react-router-dom';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import AssistantEntry from './AssistantEntry';
import { useAuthStore } from '../../stores';

vi.mock('react-i18next', () => ({ useTranslation: () => ({ t: (key: string) => key }) }));

const scope = { user_id: 'member', tenant_id: 'company' };
const unbound = {
  state: 'unbound', scope, agent: null, candidates: [],
  entry: { kind: 'setup', path: '/assistant' },
};
const own = {
  ...unbound, state: 'active', created: true,
  agent: { id: 'own', name: 'My Assistant' },
  entry: { kind: 'employee', path: '/agents/own', agent_id: 'own' },
};
const bots = [
  { id: 'old', name: 'Old', created_at: '2026-01-01', last_active_at: '2026-02-01' },
  { id: 'recent', name: 'Recent', created_at: '2025-01-01', last_active_at: '2026-11-05' },
];

function Location() {
  const location = useLocation();
  return <output data-testid="destination">{location.pathname}{location.hash}</output>;
}

function mount(path = '/', client = new QueryClient({ defaultOptions: { queries: { retry: false } } })) {
  return render(<StrictMode><QueryClientProvider client={client}><MemoryRouter initialEntries={[path]}>
    <Routes>
      <Route path="/" element={<AssistantEntry mode="entry" />} />
      <Route path="/assistant" element={<AssistantEntry mode="assistant" />} />
      <Route path="/agents/:id/*" element={<Location />} />
    </Routes>
  </MemoryRouter></QueryClientProvider></StrictMode>);
}

function transport(open: unknown = own, available: unknown[] = bots, read: unknown = unbound, status = 200) {
  return vi.stubGlobal('fetch', vi.fn(async (url: string, init?: RequestInit) => {
    const opening = url === '/api/assistant/open';
    const payload = opening ? open : url.startsWith('/api/agents/') ? available : read;
    return new Response(JSON.stringify(payload), {
      status: opening ? status : 200, headers: { 'content-type': 'application/json' },
    });
  }));
}

beforeEach(() => {
  localStorage.setItem('current_tenant_id', 'company');
  localStorage.setItem('token', 'local-token');
  useAuthStore.setState({ user: { id: 'member', tenant_id: 'company', role: 'member' } as never, token: 'local-token' });
});
afterEach(() => { cleanup(); vi.unstubAllGlobals(); });

describe('member conversation landing', () => {
  it.each(['/', '/assistant'])('provisions through the existing endpoint without a picker at %s', async (path) => {
    transport();
    mount(path);
    expect(screen.queryByRole('radio')).toBeNull();
    expect(screen.queryByRole('button', { name: 'assistant.open' })).toBeNull();
    await waitFor(() => expect(screen.getByTestId('destination').textContent).toBe('/agents/own#chat'));
    const writes = vi.mocked(fetch).mock.calls.filter(([url]) => url === '/api/assistant/open');
    expect(writes).toHaveLength(1);
    expect(JSON.parse(String(writes[0][1]?.body))).toEqual({ expected_user_id: 'member', expected_tenant_id: 'company' });
  });

  it('uses B5 to resolve the existing assistant latest conversation', async () => {
    transport(own, bots, { ...own, entry: { kind: 'session', path: '/agents/own/sessions/latest' } });
    mount();
    await waitFor(() => expect(screen.getByTestId('destination').textContent).toBe('/agents/own#chat'));
    expect(vi.mocked(fetch).mock.calls.some(([url]) => url === '/api/assistant/open')).toBe(false);
  });

  it.each(['model_unconfigured', 'model_forbidden', 'http_failure'])(
    'opens the first sidebar bot through B5 when provisioning returns %s', async (state) => {
      transport({ ...unbound, state }, bots, unbound, state === 'http_failure' ? 503 : 200);
      mount();
      await waitFor(() => expect(screen.getByTestId('destination').textContent).toBe('/agents/recent#chat'));
      expect(screen.queryByRole('radio')).toBeNull();
    },
  );

  it('opens an assistant still preparing when the sidebar contains its agent', async () => {
    transport({ ...own, state: 'provision_incomplete', entry: unbound.entry }, [...bots, own.agent]);
    mount();
    await waitFor(() => expect(screen.getByTestId('destination').textContent).toBe('/agents/own#chat'));
  });

  it('shows one admin sentence with no setup controls when there is no available bot', async () => {
    transport({ ...unbound, state: 'model_unconfigured' }, []);
    mount();
    expect(await screen.findByText('assistant.setupByAdmin')).toBeTruthy();
    expect(screen.queryByRole('button')).toBeNull();
    expect(screen.queryByRole('radio')).toBeNull();
    expect(screen.queryByRole('heading')).toBeNull();
  });

  it('keeps the explicit picker in settings and applies a chosen bot', async () => {
    transport(own, bots, { ...unbound, candidates: [{ id: 'recent', name: 'Recent' }] });
    // Selection uses the same write contract as before.
    const fetcher = vi.mocked(fetch);
    fetcher.mockImplementation(async (url, init) => new Response(JSON.stringify(
      url === '/api/assistant/select' ? { ...own, applied: true, agent: { id: 'recent' }, entry: { kind: 'employee', path: '/agents/recent' } }
        : { ...unbound, candidates: [{ id: 'recent', name: 'Recent' }] },
    ), { status: 200, headers: { 'content-type': 'application/json' } }));
    mount('/assistant?settings=true');
    fireEvent.click(await screen.findByRole('radio'));
    fireEvent.click(screen.getByRole('button', { name: 'assistant.select' }));
    await waitFor(() => expect(screen.getByTestId('destination').textContent).toBe('/agents/recent#chat'));
    expect(fetcher.mock.calls.some(([url]) => url === '/api/assistant/open')).toBe(false);
  });
});
