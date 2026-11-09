import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { get, post } from './request';
import i18n from '../../i18n';

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

  it.each([502, 503, 504])('uses plain Chinese for a gateway %s instead of raw HTTP or HTML', async status => {
    await i18n.changeLanguage('zh');
    vi.mocked(fetch).mockResolvedValue(new Response('<html>Gateway timeout</html>', { status }));
    await expect(get('/agents/bot/sessions/work')).rejects.toMatchObject({
      status, message: '服务暂时没有响应，请稍后再试。',
    });
  });

  it('uses a localized network failure while keeping aborts recognizable', async () => {
    await i18n.changeLanguage('zh');
    vi.mocked(fetch).mockRejectedValueOnce(new TypeError('Failed to fetch'));
    await expect(get('/agents')).rejects.toMatchObject({ message: '网络连接失败，请检查网络后重试。' });
    const aborted = new DOMException('Aborted', 'AbortError');
    vi.mocked(fetch).mockRejectedValueOnce(aborted);
    await expect(get('/agents')).rejects.toBe(aborted);
  });

  it('keeps a specific application error and its structured fields on a 503', async () => {
    await i18n.changeLanguage('zh');
    const detail = { message: '资料仍在处理，请稍后重试。', code: 'processing' };
    vi.mocked(fetch).mockResolvedValue(new Response(JSON.stringify({ detail }), { status: 503 }));
    await expect(get('/documents')).rejects.toMatchObject({ status: 503, message: detail.message, data: detail });
  });

  it('keeps gateway and network fallback messages English in the English UI', async () => {
    await i18n.changeLanguage('en');
    vi.mocked(fetch).mockResolvedValueOnce(new Response('', { status: 504 }));
    await expect(get('/agents')).rejects.toMatchObject({ message: 'The service is not responding. Please try again later.' });
    vi.mocked(fetch).mockRejectedValueOnce(new TypeError('Failed to fetch'));
    await expect(get('/agents')).rejects.toMatchObject({ message: 'Could not connect. Check your network and try again.' });
  });
});
