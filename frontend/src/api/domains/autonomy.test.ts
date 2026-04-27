import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

describe('autonomy/objective API adapters', () => {
  beforeEach(() => {
    vi.resetModules();
  });

  afterEach(() => {
    vi.restoreAllMocks();
  });

  it('builds agent-scoped autonomy URLs without exposing diagnostics by default', async () => {
    vi.doMock('../core', async () => {
      const actual = await vi.importActual<typeof import('../core')>('../core');
      return {
        ...actual,
        get: vi.fn(),
        post: vi.fn(),
        patch: vi.fn(),
      };
    });

    const { autonomyApi } = await import('./autonomy');
    const { objectiveApi } = await import('./objectives');
    const { get, post, patch } = await import('../core');
    vi.mocked(get).mockResolvedValue({} as never);
    vi.mocked(post).mockResolvedValue({} as never);
    vi.mocked(patch).mockResolvedValue({} as never);

    await autonomyApi.getOverview('agent-1', { lookbackHours: 12 });
    await autonomyApi.getDiagnostics('agent-1', { lookbackHours: 6 });
    await autonomyApi.listRuntimeTasks('agent-1', { taskType: 'trigger', status: 'failed', diagnostics: true });
    await autonomyApi.getRuntimeArtifact('agent-1', 'task-1');
    await objectiveApi.approve('agent-1', 'objective-1', { reason: 'approved' });
    await objectiveApi.update('agent-1', 'objective-1', { status: 'completed', completion_evidence: 'workspace/report.md' });

    expect(get).toHaveBeenNthCalledWith(1, '/agents/agent-1/autonomy/overview?lookback_hours=12');
    expect(get).toHaveBeenNthCalledWith(2, '/agents/agent-1/autonomy/diagnostics?lookback_hours=6');
    expect(get).toHaveBeenNthCalledWith(
      3,
      '/agents/agent-1/runtime-tasks?task_type=trigger&status=failed&diagnostics=true',
    );
    expect(get).toHaveBeenNthCalledWith(4, '/agents/agent-1/runtime-artifacts/task-1');
    expect(post).toHaveBeenCalledWith('/agents/agent-1/objectives/objective-1/approve', { reason: 'approved' });
    expect(patch).toHaveBeenCalledWith('/agents/agent-1/objectives/objective-1', {
      status: 'completed',
      completion_evidence: 'workspace/report.md',
    });
  });
});
