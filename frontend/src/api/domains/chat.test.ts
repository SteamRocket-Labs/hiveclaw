import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

describe('chat API adapter', () => {
  beforeEach(() => {
    vi.resetModules();
  });

  afterEach(() => {
    vi.restoreAllMocks();
  });

  it('builds branch and lineage URLs with transcript event anchors', async () => {
    vi.doMock('../core', async () => {
      const actual = await vi.importActual<typeof import('../core')>('../core');
      return {
        ...actual,
        get: vi.fn(),
        post: vi.fn(),
      };
    });

    const { chatApi } = await import('./chat');
    const { get, post } = await import('../core');
    vi.mocked(get).mockResolvedValue({} as never);
    vi.mocked(post).mockResolvedValue({} as never);

    await chatApi.branchSession('agent-1', 'session-1', {
      mode: 'edit',
      anchor_event_id: 'event-1',
      content: 'replacement',
      display_content: 'replacement',
      start_run: true,
    });
    await chatApi.listSessionBranches('agent-1', 'session-1');
    await chatApi.getSessionLineage('agent-1', 'session-1');
    await chatApi.getSessionIndex('agent-1', 'session-1');

    expect(post).toHaveBeenCalledWith('/agents/agent-1/sessions/session-1/branches', {
      mode: 'edit',
      anchor_event_id: 'event-1',
      content: 'replacement',
      display_content: 'replacement',
      start_run: true,
    });
    expect(get).toHaveBeenNthCalledWith(1, '/agents/agent-1/sessions/session-1/branches');
    expect(get).toHaveBeenNthCalledWith(2, '/agents/agent-1/sessions/session-1/lineage');
    expect(get).toHaveBeenNthCalledWith(3, '/agents/agent-1/sessions/session-1/index');
  });

  it('builds transcript windowing URLs: tail first screen, older paging, incremental contract', async () => {
    vi.doMock('../core', async () => {
      const actual = await vi.importActual<typeof import('../core')>('../core');
      return {
        ...actual,
        get: vi.fn(),
      };
    });

    const { chatApi } = await import('./chat');
    const { get } = await import('../core');
    vi.mocked(get).mockResolvedValue([] as never);

    await chatApi.getSessionTranscript('agent-1', 'session-1', { direction: 'backward', limit: 100 });
    await chatApi.getSessionTranscript('agent-1', 'session-1', { beforeSequence: 57, limit: 100 });
    await chatApi.getSessionTranscript('agent-1', 'session-1', { afterSequence: 41 });
    await chatApi.getSessionTranscript('agent-1', 'session-1');

    expect(get).toHaveBeenNthCalledWith(
      1,
      '/agents/agent-1/sessions/session-1/transcript?direction=backward&limit=100',
      undefined,
    );
    expect(get).toHaveBeenNthCalledWith(
      2,
      '/agents/agent-1/sessions/session-1/transcript?before_sequence=57&limit=100',
      undefined,
    );
    expect(get).toHaveBeenNthCalledWith(3, '/agents/agent-1/sessions/session-1/transcript?after_sequence=41', undefined);
    expect(get).toHaveBeenNthCalledWith(4, '/agents/agent-1/sessions/session-1/transcript', undefined);
  });

  it('sends structured attachment metadata when starting a session run', async () => {
    vi.doMock('../core', async () => {
      const actual = await vi.importActual<typeof import('../core')>('../core');
      return {
        ...actual,
        post: vi.fn(),
      };
    });

    const { chatApi } = await import('./chat');
    const { post } = await import('../core');
    vi.mocked(post).mockResolvedValue({} as never);

    await chatApi.startSessionRun('agent-1', 'session-1', {
      content: '[File: bank.pdf]\nFull text',
      display_content: '[file:bank.pdf]',
      file_name: 'bank.pdf',
      attachments: [{ name: 'bank.pdf', path: 'workspace/bank.pdf' }],
    });

    expect(post).toHaveBeenCalledWith('/agents/agent-1/sessions/session-1/runs', {
      content: '[File: bank.pdf]\nFull text',
      display_content: '[file:bank.pdf]',
      file_name: 'bank.pdf',
      attachments: [{ name: 'bank.pdf', path: 'workspace/bank.pdf' }],
    });
  });

  it('creates a session and starts its first run through the atomic run endpoint', async () => {
    vi.doMock('../core', async () => {
      const actual = await vi.importActual<typeof import('../core')>('../core');
      return {
        ...actual,
        post: vi.fn(),
      };
    });

    const { chatApi } = await import('./chat');
    const { post } = await import('../core');
    vi.mocked(post).mockResolvedValue({ session: { id: 'session-1' }, run: { run_id: 'run-1' } } as never);

    await chatApi.createSessionRun('agent-1', {
      title: 'Session 07-01 03:16',
      content: 'hello',
      display_content: 'hello',
      permission_mode: 'bypassPermissions',
    });

    expect(post).toHaveBeenCalledWith('/agents/agent-1/sessions/runs', {
      title: 'Session 07-01 03:16',
      content: 'hello',
      display_content: 'hello',
      permission_mode: 'bypassPermissions',
    });
  });
});
