import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

describe('external principal API adapter', () => {
  beforeEach(() => vi.resetModules());
  afterEach(() => vi.restoreAllMocks());

  it('uses tenant-scoped list and explicit link/unlink decisions', async () => {
    vi.doMock('../core', async () => {
      const actual = await vi.importActual<typeof import('../core')>('../core');
      return { ...actual, get: vi.fn(), post: vi.fn() };
    });
    const { externalPrincipalsApi } = await import('./externalPrincipals');
    const { get, post } = await import('../core');
    vi.mocked(get).mockResolvedValue([] as never);
    vi.mocked(post).mockResolvedValue({} as never);

    await externalPrincipalsApi.list({
      tenantId: 'tenant-1',
      provider: 'slack',
      status: 'active',
      linked: false,
    });
    await externalPrincipalsApi.link('principal-1', 'user-1', 'accepted invitation', 'tenant-1');
    await externalPrincipalsApi.unlink('principal-1', 'admin revoked mapping', 'tenant-1');

    expect(get).toHaveBeenCalledWith(
      '/enterprise/external-principals?tenant_id=tenant-1&provider=slack&status=active&linked=false',
    );
    expect(post).toHaveBeenNthCalledWith(
      1,
      '/enterprise/external-principals/principal-1/link?tenant_id=tenant-1',
      { user_id: 'user-1', reason: 'accepted invitation' },
    );
    expect(post).toHaveBeenNthCalledWith(
      2,
      '/enterprise/external-principals/principal-1/unlink?tenant_id=tenant-1',
      { reason: 'admin revoked mapping' },
    );
  });
});
