import React from 'react';
import { renderToStaticMarkup } from 'react-dom/server';
import { describe, expect, it, vi } from 'vitest';

const routeSearch = vi.hoisted(() => ({ value: '' }));

vi.mock('react-router-dom', () => ({
  useParams: () => ({ id: 'agent-1' }),
  useLocation: () => ({ search: routeSearch.value }),
  Navigate: ({ to }: { to: string }) => <div data-target={to} />,
}));

describe('Chat route', () => {
  it('redirects legacy chat route to the unified agent detail chat tab', async () => {
    vi.stubGlobal('localStorage', {
      getItem: vi.fn(() => ''),
      setItem: vi.fn(),
      removeItem: vi.fn(),
      clear: vi.fn(),
      key: vi.fn(),
      length: 0,
    } as unknown as Storage);

    const { default: Chat } = await import('./Chat');
    const markup = renderToStaticMarkup(<Chat />);

    expect(markup).toContain('/agents/agent-1#chat');
  });

  it('preserves an explicit session query when redirecting legacy chat routes', async () => {
    routeSearch.value = '?session_id=session-1';

    const { default: Chat } = await import('./Chat');
    const markup = renderToStaticMarkup(<Chat />);

    expect(markup).toContain('/agents/agent-1?session_id=session-1#chat');
    routeSearch.value = '';
  });
});
