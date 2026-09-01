import { describe, expect, it, vi } from 'vitest';

vi.mock('../core', () => ({
  get: vi.fn(async (url: string) => ({ url })),
  getBlob: vi.fn(async (url: string) => new Blob([url])),
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
    const { get, getBlob } = await import('../core');
    const { fileApi } = await import('./files');
    await fileApi.readArtifact('agent-1', 'artifact-1');
    await fileApi.downloadArtifact('agent-1', 'artifact-1');

    expect(vi.mocked(get).mock.calls[0][0]).toBe('/agents/agent-1/files/artifacts/artifact-1/content');
    expect(vi.mocked(getBlob).mock.calls.at(-1)?.[0]).toBe(
      '/agents/agent-1/files/artifacts/artifact-1/download',
    );
  });

  it('adds explicit operator-view authority to workspace and chat artifact reads', async () => {
    const { get, put, del } = await import('../core');
    const { fileApi } = await import('./files');
    const authority = { operatorView: true, reason: 'Agent workspace administration' };

    await fileApi.list('agent-1', 'workspace', authority);
    await fileApi.read('agent-1', 'workspace/report.md', authority);
    await fileApi.write('agent-1', 'workspace/report.md', 'updated', authority);
    await fileApi.delete('agent-1', 'workspace/report.md', authority);

    expect(vi.mocked(get).mock.calls.at(-2)?.[0]).toContain('operator_view=true');
    expect(vi.mocked(get).mock.calls.at(-2)?.[0]).toContain('operator_reason=Agent+workspace+administration');
    expect(vi.mocked(get).mock.calls.at(-1)?.[0]).toContain('operator_view=true');
    expect(vi.mocked(put).mock.calls.at(-1)?.[0]).toContain('operator_view=true');
    expect(vi.mocked(del).mock.calls.at(-1)?.[0]).toContain('operator_view=true');

    await fileApi.readArtifact('agent-1', 'artifact-1', authority);
    expect(vi.mocked(get).mock.calls.at(-1)?.[0]).toContain('operator_view=true');
    await fileApi.downloadArtifact('agent-1', 'artifact-1', authority);
    const { getBlob } = await import('../core');
    expect(vi.mocked(getBlob).mock.calls.at(-1)?.[0]).toContain('operator_view=true');
  });

  it('refuses operator view without an exact audit reason', async () => {
    const { fileApi } = await import('./files');

    expect(() => fileApi.list('agent-1', 'workspace', { operatorView: true })).toThrow(
      'Operator View requires an audit reason',
    );
    expect(() => fileApi.read('agent-1', 'workspace/report.md', {
      operatorView: true,
      reason: '   ',
    })).toThrow('Operator View requires an audit reason');
  });
});
