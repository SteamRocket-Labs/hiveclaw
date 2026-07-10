import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

describe('enterprise AI asset API adapter', () => {
  beforeEach(() => vi.resetModules());
  afterEach(() => vi.restoreAllMocks());

  it('uses catalog, revision, rollback, and reconciliation endpoints', async () => {
    vi.doMock('../core', async () => {
      const actual = await vi.importActual<typeof import('../core')>('../core');
      return { ...actual, get: vi.fn(), post: vi.fn() };
    });

    const { aiAssetsApi } = await import('./aiAssets');
    const { get, post } = await import('../core');
    vi.mocked(get).mockResolvedValue([] as never);
    vi.mocked(post).mockResolvedValue({} as never);

    await aiAssetsApi.list({ assetType: 'skill', lifecycleStatus: 'active' });
    await aiAssetsApi.detail('asset-1');
    await aiAssetsApi.revisions('asset-1', 20);
    await aiAssetsApi.revision('asset-1', 3);
    await aiAssetsApi.rollback('asset-1', 2);
    await aiAssetsApi.reconcile('asset-1');

    expect(get).toHaveBeenNthCalledWith(
      1,
      '/enterprise/ai-assets?asset_type=skill&lifecycle_status=active',
    );
    expect(get).toHaveBeenNthCalledWith(2, '/enterprise/ai-assets/asset-1');
    expect(get).toHaveBeenNthCalledWith(3, '/enterprise/ai-assets/asset-1/revisions?limit=20');
    expect(get).toHaveBeenNthCalledWith(4, '/enterprise/ai-assets/asset-1/revisions/3');
    expect(post).toHaveBeenNthCalledWith(1, '/enterprise/ai-assets/asset-1/rollback', { target_version: 2 });
    expect(post).toHaveBeenNthCalledWith(2, '/enterprise/ai-assets/asset-1/reconcile');
  });
});
