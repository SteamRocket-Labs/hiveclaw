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

    expect(post).toHaveBeenCalledWith('/agents/agent-1/sessions/session-1/branches', {
      mode: 'edit',
      anchor_event_id: 'event-1',
      content: 'replacement',
      display_content: 'replacement',
      start_run: true,
    });
    expect(get).toHaveBeenNthCalledWith(1, '/agents/agent-1/sessions/session-1/branches');
    expect(get).toHaveBeenNthCalledWith(2, '/agents/agent-1/sessions/session-1/lineage');
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
});
