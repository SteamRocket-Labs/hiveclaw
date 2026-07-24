import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

describe('governance API adapter', () => {
  beforeEach(() => {
    vi.resetModules();
  });

  afterEach(() => {
    vi.restoreAllMocks();
  });

  it('uses tenant guardrail and platform feature-rollout endpoints', async () => {
    vi.doMock('../core', async () => {
      const actual = await vi.importActual<typeof import('../core')>('../core');
      return {
        ...actual,
        del: vi.fn(),
        get: vi.fn(),
        patch: vi.fn(),
        post: vi.fn(),
        put: vi.fn(),
      };
    });

    const { governanceApi } = await import('./governance');
    const { del, get, patch, post, put } = await import('../core');
    vi.mocked(del).mockResolvedValue(undefined as never);
    vi.mocked(get).mockResolvedValue([] as never);
    vi.mocked(patch).mockResolvedValue({} as never);
    vi.mocked(post).mockResolvedValue({} as never);
    vi.mocked(put).mockResolvedValue({} as never);

    await governanceApi.getGuardPolicy();
    await governanceApi.updateGuardPolicy({
      expected_version: 3,
      egress_guard: { tool_rules: [] },
    });
    await governanceApi.listFeatureFlags();
    await governanceApi.createFeatureFlag({
      key: 'runtime_continuity_v1',
      flag_type: 'boolean',
      enabled: true,
    });
    await governanceApi.updateFeatureFlag('flag-1', {
      enabled: false,
      expected_updated_at: '2026-07-02T00:00:00Z',
    });
    await governanceApi.deleteFeatureFlag('flag-1', '2026-07-02T00:00:00Z');

    expect(get).toHaveBeenNthCalledWith(1, '/guard-policies');
    expect(put).toHaveBeenCalledWith('/guard-policies', {
      expected_version: 3,
      egress_guard: { tool_rules: [] },
    });
    expect(get).toHaveBeenNthCalledWith(2, '/feature-flags/');
    expect(post).toHaveBeenCalledWith('/feature-flags/', {
      key: 'runtime_continuity_v1',
      flag_type: 'boolean',
      enabled: true,
    });
    expect(patch).toHaveBeenCalledWith('/feature-flags/flag-1', {
      enabled: false,
      expected_updated_at: '2026-07-02T00:00:00Z',
    });
    expect(del).toHaveBeenCalledWith(
      '/feature-flags/flag-1?expected_updated_at=2026-07-02T00%3A00%3A00Z',
    );
  });
});
