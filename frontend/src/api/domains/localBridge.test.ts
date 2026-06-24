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
        del: vi.fn(),
        get: vi.fn(),
        getBlob: vi.fn(),
        post: vi.fn(),
        upload: vi.fn(),
      };
    });

    const { localBridgeApi } = await import('./localBridge');
    const { del, get, getBlob, post, upload } = await import('../core');
    vi.mocked(post).mockResolvedValue({} as never);
    vi.mocked(get).mockResolvedValue({} as never);
    vi.mocked(del).mockResolvedValue({} as never);
    vi.mocked(getBlob).mockResolvedValue(new Blob(['file']) as never);
    vi.mocked(upload).mockResolvedValue({} as never);

    await localBridgeApi.approvePairing('HIVE-ABCD');
    await localBridgeApi.getInstallGuide();
    await localBridgeApi.getDefaultChannelSession();
    await localBridgeApi.createChannelSession({ source: 'web', title: 'Local chat' });
    await localBridgeApi.sendChannelMessage('session-1', {
      content: 'hello local codex',
      metadata: { purpose: 'smoke' },
    });
    await localBridgeApi.getChannelTimeline('session-1');
    await localBridgeApi.createBrowserChannelWsTicket('session-1');
    await localBridgeApi.listChannelEvents('session-1');
    await localBridgeApi.listWorkspaceFiles('workspace/uploads');
    await localBridgeApi.readWorkspaceFile('workspace/uploads/report.md');
    await localBridgeApi.downloadWorkspaceFile('workspace/uploads/report.md');
    await localBridgeApi.uploadWorkspaceFile(new File(['hello'], 'hello.md'));
    await localBridgeApi.listAgentChannelSessions('agent-local-1');
    await localBridgeApi.getAgentDefaultChannelSession('agent-local-1');
    await localBridgeApi.createAgentChannelSession('agent-local-1', { title: 'New local chat' });
    await localBridgeApi.getAgentChannelSession('agent-local-1', 'chat-session-1');
    await localBridgeApi.sendAgentChannelMessage('agent-local-1', 'session-2', {
      content: 'hello bound local agent',
      metadata: { purpose: 'detail_chat' },
    });
    await localBridgeApi.deleteAgentChannelSession('agent-local-1', 'session-2');

    expect(post).toHaveBeenNthCalledWith(1, '/local-bridge/pairings/HIVE-ABCD/approve');
    expect(get).toHaveBeenNthCalledWith(1, '/local-bridge/install-guide');
    expect(post).toHaveBeenNthCalledWith(2, '/local-agents/sessions/default');
    expect(post).toHaveBeenNthCalledWith(3, '/local-agents/sessions', {
      source: 'web',
      title: 'Local chat',
    });
    expect(post).toHaveBeenNthCalledWith(4, '/local-agents/sessions/session-1/messages', {
      content: 'hello local codex',
      attachments: [],
      metadata: { purpose: 'smoke' },
    });
    expect(get).toHaveBeenNthCalledWith(2, '/local-agents/sessions/session-1/timeline');
    expect(post).toHaveBeenNthCalledWith(5, '/local-agents/sessions/session-1/ws-ticket');
    expect(get).toHaveBeenNthCalledWith(3, '/local-agents/sessions/session-1/events');
    expect(get).toHaveBeenNthCalledWith(4, '/local-agents/workspace/files?path=workspace%2Fuploads');
    expect(get).toHaveBeenNthCalledWith(5, '/local-agents/workspace/content?path=workspace%2Fuploads%2Freport.md');
    expect(getBlob).toHaveBeenCalledWith('/local-agents/workspace/download?path=workspace%2Fuploads%2Freport.md');
    expect(upload).toHaveBeenCalledWith('/chat/upload', expect.any(File));
    expect(get).toHaveBeenNthCalledWith(6, '/agents/agent-local-1/local-agent/sessions');
    expect(post).toHaveBeenNthCalledWith(6, '/agents/agent-local-1/local-agent/sessions/default');
    expect(post).toHaveBeenNthCalledWith(7, '/agents/agent-local-1/local-agent/sessions', {
      source: 'web',
      title: 'New local chat',
    });
    expect(get).toHaveBeenNthCalledWith(7, '/agents/agent-local-1/local-agent/sessions/chat-session-1');
    expect(post).toHaveBeenNthCalledWith(8, '/agents/agent-local-1/local-agent/sessions/session-2/messages', {
      content: 'hello bound local agent',
      attachments: [],
      metadata: { purpose: 'detail_chat' },
    });
    expect(del).toHaveBeenCalledWith('/agents/agent-local-1/local-agent/sessions/session-2');
  });
});
