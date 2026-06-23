import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

describe('localBridge API adapter', () => {
  beforeEach(() => {
    vi.resetModules();
  });

  afterEach(() => {
    vi.restoreAllMocks();
  });

  it('builds user-scoped Local Agent Channel URLs', async () => {
    vi.doMock('../core', async () => {
      const actual = await vi.importActual<typeof import('../core')>('../core');
      return {
        ...actual,
        get: vi.fn(),
        getBlob: vi.fn(),
        post: vi.fn(),
        upload: vi.fn(),
      };
    });

    const { localBridgeApi } = await import('./localBridge');
    const { get, getBlob, post, upload } = await import('../core');
    vi.mocked(post).mockResolvedValue({} as never);
    vi.mocked(get).mockResolvedValue({} as never);
    vi.mocked(getBlob).mockResolvedValue(new Blob(['file']) as never);
    vi.mocked(upload).mockResolvedValue({} as never);

    await localBridgeApi.approvePairing('HIVE-ABCD');
    await localBridgeApi.createChannelSession({ source: 'web', title: 'Local chat' });
    await localBridgeApi.sendChannelMessage('session-1', {
      content: 'hello local codex',
      metadata: { purpose: 'smoke' },
    });
    await localBridgeApi.listChannelEvents('session-1');
    await localBridgeApi.listWorkspaceFiles('workspace/uploads');
    await localBridgeApi.readWorkspaceFile('workspace/uploads/report.md');
    await localBridgeApi.downloadWorkspaceFile('workspace/uploads/report.md');
    await localBridgeApi.uploadWorkspaceFile(new File(['hello'], 'hello.md'));

    expect(post).toHaveBeenNthCalledWith(1, '/local-bridge/pairings/HIVE-ABCD/approve');
    expect(post).toHaveBeenNthCalledWith(2, '/local-agents/sessions', {
      source: 'web',
      title: 'Local chat',
    });
    expect(post).toHaveBeenNthCalledWith(3, '/local-agents/sessions/session-1/messages', {
      content: 'hello local codex',
      attachments: [],
      metadata: { purpose: 'smoke' },
    });
    expect(get).toHaveBeenNthCalledWith(1, '/local-agents/sessions/session-1/events');
    expect(get).toHaveBeenNthCalledWith(2, '/local-agents/workspace/files?path=workspace%2Fuploads');
    expect(get).toHaveBeenNthCalledWith(3, '/local-agents/workspace/content?path=workspace%2Fuploads%2Freport.md');
    expect(getBlob).toHaveBeenCalledWith('/local-agents/workspace/download?path=workspace%2Fuploads%2Freport.md');
    expect(upload).toHaveBeenCalledWith('/chat/upload', expect.any(File));
  });
});
