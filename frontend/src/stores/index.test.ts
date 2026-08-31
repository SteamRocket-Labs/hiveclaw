import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import type { User } from '../types';

const localStore: Record<string, string> = {};
const localStorageStub = {
  getItem: vi.fn((key: string) => (key in localStore ? localStore[key] : null)),
  setItem: vi.fn((key: string, value: string) => {
    localStore[key] = value;
  }),
  removeItem: vi.fn((key: string) => {
    delete localStore[key];
  }),
};

function makeUser(overrides: Partial<User> = {}): User {
  return {
    id: 'user-1',
    username: 'example-owner',
    email: 'example-owner@example.com',
    display_name: 'Example Owner',
    role: 'platform_admin',
    tenant_id: 'home-tenant',
    is_active: true,
    created_at: '2026-06-17T00:00:00Z',
    ...overrides,
  };
}

describe('auth store tenant selection', () => {
  beforeEach(() => {
    vi.resetModules();
    for (const key of Object.keys(localStore)) delete localStore[key];
    localStorageStub.getItem.mockClear();
    localStorageStub.setItem.mockClear();
    localStorageStub.removeItem.mockClear();
    vi.stubGlobal('localStorage', localStorageStub);
  });

  afterEach(() => {
    vi.restoreAllMocks();
    vi.unstubAllGlobals();
  });

  it('resets a stale selected tenant to the authenticated user tenant on login', async () => {
    localStore.current_tenant_id = 'stale-selected-tenant';
    const { useAuthStore } = await import('./index');

    useAuthStore.getState().setAuth(makeUser({ tenant_id: 'home-tenant' }), 'jwt-token');

    expect(localStore.token).toBe('jwt-token');
    expect(localStore.current_tenant_id).toBe('home-tenant');
    expect(localStorageStub.setItem).toHaveBeenCalledWith('current_tenant_id', 'home-tenant');
  });

  it('clears selected tenant when the authenticated user has no tenant yet', async () => {
    localStore.current_tenant_id = 'stale-selected-tenant';
    const { useAuthStore } = await import('./index');

    useAuthStore.getState().setAuth(makeUser({ tenant_id: undefined }), 'jwt-token');

    expect(localStore.token).toBe('jwt-token');
    expect(localStore.current_tenant_id).toBeUndefined();
    expect(localStorageStub.removeItem).toHaveBeenCalledWith('current_tenant_id');
  });
});
