import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { get, post } from './request';

const localStore: Record<string, string> = {};
const localStorageStub = {
  getItem: vi.fn((key: string) => (key in localStore ? localStore[key] : null)),
  setItem: vi.fn((key: string, value: string) => {
    localStore[key] = value;
  }),
  removeItem: vi.fn((key: string) => {
    delete localStore[key];
  }),
  clear: vi.fn(() => {
    for (const key of Object.keys(localStore)) delete localStore[key];
  }),
};

describe('request auth errors', () => {
  beforeEach(() => {
    for (const key of Object.keys(localStore)) delete localStore[key];
    localStorageStub.getItem.mockClear();
    localStorageStub.setItem.mockClear();
    localStorageStub.removeItem.mockClear();
    localStorageStub.clear.mockClear();
    localStore.token = 'stale-token';

    vi.stubGlobal('localStorage', localStorageStub);
    vi.stubGlobal('window', {
      location: { href: '' },
    } as unknown as Window & typeof globalThis);
    vi.stubGlobal('fetch', vi.fn());
  });

  afterEach(() => {
    vi.restoreAllMocks();
    vi.unstubAllGlobals();
  });

  it('preserves backend 401 details for login instead of showing an expired session', async () => {
    vi.mocked(fetch).mockResolvedValue(new Response(
      JSON.stringify({ detail: 'Invalid credentials' }),
      { status: 401, headers: { 'Content-Type': 'application/json' } },
    ));

    await expect(post('/auth/login', { username: 'JiayuXu', password: 'bad-password' }))
      .rejects
      .toMatchObject({
        status: 401,
        detail: 'Invalid credentials',
        message: 'Invalid credentials',
      });

    expect(localStorageStub.removeItem).not.toHaveBeenCalled();
    expect(window.location.href).toBe('');
  });

  it('keeps protected 401 responses as expired sessions and redirects to login', async () => {
    vi.mocked(fetch).mockResolvedValue(new Response(
      JSON.stringify({ detail: 'Not authenticated' }),
      { status: 401, headers: { 'Content-Type': 'application/json' } },
    ));

    await expect(get('/auth/me')).rejects.toMatchObject({
      status: 401,
      detail: 'Session expired',
      message: 'Session expired',
    });

    expect(localStorageStub.removeItem).toHaveBeenCalledWith('token');
    expect(localStorageStub.removeItem).toHaveBeenCalledWith('user');
    expect(window.location.href).toBe('/login');
  });
});
