import { renderToStaticMarkup } from 'react-dom/server';
import { describe, expect, it, vi } from 'vitest';

vi.mock('react-i18next', () => ({
  useTranslation: () => ({
    t: (_key: string, fallback?: string) => fallback || _key,
  }),
}));

import { SessionTransportStatus } from './SessionTransportStatus';

describe('SessionTransportStatus', () => {
  it('renders no transport warning while realtime is connected', () => {
    expect(renderToStaticMarkup(<SessionTransportStatus phase="connected" />)).toBe('');
  });

  it('offers a manual reconnect without implying that the durable task stopped', () => {
    const reconnecting = renderToStaticMarkup(
      <SessionTransportStatus phase="reconnecting" attempt={2} onReconnect={vi.fn()} />,
    );
    const degraded = renderToStaticMarkup(
      <SessionTransportStatus phase="degraded" attempt={8} onReconnect={vi.fn()} />,
    );

    expect(reconnecting).toContain('Live updates reconnecting');
    expect(reconnecting).toContain('data-testid="session-transport-reconnect"');
    expect(degraded).toContain('The task is still running in the background');
    expect(degraded).toContain('durable history');
    expect(degraded).not.toContain('attempt 8');
  });

  it('distinguishes browser offline and expired authorization', () => {
    const offline = renderToStaticMarkup(<SessionTransportStatus phase="offline" />);
    const authFailed = renderToStaticMarkup(<SessionTransportStatus phase="auth_failed" />);

    expect(offline).toContain('You are offline');
    expect(offline).toContain('catch up automatically');
    expect(offline).not.toContain('session-transport-reconnect');
    expect(authFailed).toContain('Sign in again');
    expect(authFailed).not.toContain('session-transport-reconnect');
  });

  it('gives the auth_failed state a typed recovery exit instead of a dead end', () => {
    const authFailed = renderToStaticMarkup(
      <SessionTransportStatus phase="auth_failed" onReload={() => {}} />,
    );

    expect(authFailed).toContain('Sign in again');
    expect(authFailed).toContain('data-testid="session-transport-reload"');
    expect(authFailed).toContain('Reload');
  });
});
