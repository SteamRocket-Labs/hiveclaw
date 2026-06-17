import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

describe('auth API adapter', () => {
  beforeEach(() => {
    vi.resetModules();
  });

  afterEach(() => {
    vi.restoreAllMocks();
  });

  it('normalizes the login identifier before posting credentials', async () => {
    vi.doMock('../core', async () => {
      const actual = await vi.importActual<typeof import('../core')>('../core');
      return {
        ...actual,
        post: vi.fn().mockResolvedValue({ access_token: 'jwt-stub' }),
      };
    });

    const { authApi } = await import('./auth');
    const { post } = await import('../core');

    await authApi.login({ username: ' SimonXu1212 ', password: 'secret' });

    expect(post).toHaveBeenCalledWith('/auth/login', {
      username: 'SimonXu1212',
      password: 'secret',
    });
  });
});
