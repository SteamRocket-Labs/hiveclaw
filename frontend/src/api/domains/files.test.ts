import { describe, expect, it, vi } from 'vitest';

vi.mock('../core', () => ({
  get: vi.fn(async (url: string) => ({ url })),
  post: vi.fn(async (url: string, body?: unknown) => ({ url, body })),
  put: vi.fn(async (url: string, body?: unknown) => ({ url, body })),
  del: vi.fn(async (url: string) => ({ url })),
  upload: vi.fn(async (url: string) => ({ url })),
}));

vi.mock('../core/upload-progress', () => ({
  uploadFileWithProgress: vi.fn(() => ({ promise: Promise.resolve({}) })),
}));

describe('fileApi artifact snapshot adapter', () => {
  it('reads and downloads delivered artifacts by artifact id instead of mutable workspace path', async () => {
    const { get } = await import('../core');
    const { fileApi } = await import('./files');
    const storage = new Map<string, string>();

    vi.stubGlobal('localStorage', {
      getItem: (key: string) => storage.get(key) ?? null,
      setItem: (key: string, value: string) => {
        storage.set(key, value);
      },
    });
    localStorage.setItem('token', 'token-1');

    await fileApi.readArtifact('agent-1', 'artifact-1');

    expect(vi.mocked(get).mock.calls[0][0]).toBe('/agents/agent-1/files/artifacts/artifact-1/content');
    expect(fileApi.artifactDownloadUrl('agent-1', 'artifact-1')).toBe(
      '/api/agents/agent-1/files/artifacts/artifact-1/download?token=token-1',
    );
  });
});
