import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

describe('HR creation draft API adapter', () => {
  beforeEach(() => vi.resetModules());
  afterEach(() => vi.restoreAllMocks());

  it('binds authenticated decisions to the agent, draft, version, and hash', async () => {
    vi.doMock('../core', async () => {
      const actual = await vi.importActual<typeof import('../core')>('../core');
      return { ...actual, get: vi.fn(), post: vi.fn() };
    });
    const { hrCreationApi } = await import('./hrCreation');
    const { get, post } = await import('../core');
    vi.mocked(get).mockResolvedValue({} as never);
    vi.mocked(post).mockResolvedValue({} as never);

    await hrCreationApi.get('hr-1', 'draft-1');
    await hrCreationApi.confirm('hr-1', 'draft-1', {
      blueprint_version: 2,
      blueprint_hash: 'sha256:canonical',
    });
    await hrCreationApi.reject('hr-1', 'draft-1');

    expect(get).toHaveBeenCalledWith('/agents/hr-1/hr-creation-drafts/draft-1');
    expect(post).toHaveBeenNthCalledWith(1, '/agents/hr-1/hr-creation-drafts/draft-1/confirm', {
      blueprint_version: 2,
      blueprint_hash: 'sha256:canonical',
    });
    expect(post).toHaveBeenNthCalledWith(2, '/agents/hr-1/hr-creation-drafts/draft-1/reject', {});
  });
});
