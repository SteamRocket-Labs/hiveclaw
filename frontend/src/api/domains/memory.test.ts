import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

describe('memoryApi', () => {
  beforeEach(() => {
    vi.resetModules();
  });

  afterEach(() => {
    vi.restoreAllMocks();
  });

  it('builds shared memory URLs with URLSearchParams', async () => {
    vi.doMock('../core', async () => {
      const actual = await vi.importActual<typeof import('../core')>('../core');
      return {
        ...actual,
        get: vi.fn(),
        put: vi.fn(),
        del: vi.fn(),
      };
    });

    const { memoryApi } = await import('./memory');
    const { get, put, del } = await import('../core');
    vi.mocked(get).mockResolvedValue([]);
    vi.mocked(put).mockResolvedValue({ key: 'deploy-playbook' } as never);
    vi.mocked(del).mockResolvedValue({ deleted: true, key: 'deploy-playbook', workspace_key: 'workspace' } as never);

    await memoryApi.listShared('workspace', 'tenant-1');
    await memoryApi.getShared('deploy-playbook', 'workspace', 'tenant-1');
    await memoryApi.searchShared('workspace', 'deploy checklist', 'tenant-1');
    await memoryApi.upsertShared(
      {
        workspace_key: 'workspace',
        key: 'deploy-playbook',
        title: 'Deploy Playbook',
        content: 'Use a canary rollout first.',
        mode: 'append',
        base_revision: 3,
      },
      'tenant-1',
    );
    await memoryApi.deleteShared('deploy-playbook', 'workspace', 'tenant-1');

    expect(get).toHaveBeenNthCalledWith(1, '/enterprise/memory/shared?workspace_key=workspace&tenant_id=tenant-1');
    expect(get).toHaveBeenNthCalledWith(2, '/enterprise/memory/shared/deploy-playbook?workspace_key=workspace&tenant_id=tenant-1');
    expect(get).toHaveBeenNthCalledWith(3, '/enterprise/memory/shared/search?workspace_key=workspace&query=deploy+checklist&tenant_id=tenant-1');
    expect(put).toHaveBeenCalledWith('/enterprise/memory/shared?tenant_id=tenant-1', {
      workspace_key: 'workspace',
      key: 'deploy-playbook',
      title: 'Deploy Playbook',
      content: 'Use a canary rollout first.',
      mode: 'append',
      base_revision: 3,
    });
    expect(del).toHaveBeenCalledWith('/enterprise/memory/shared/deploy-playbook?workspace_key=workspace&tenant_id=tenant-1');
  });
});
