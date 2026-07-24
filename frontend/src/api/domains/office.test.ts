import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

describe('office API adapter', () => {
  beforeEach(() => {
    vi.resetModules();
  });

  afterEach(() => {
    vi.restoreAllMocks();
  });

  it('builds authenticated agent-scoped Office preview URLs without query credentials', async () => {
    vi.doMock('../core', async () => {
      const actual = await vi.importActual<typeof import('../core')>('../core');
      return {
        ...actual,
        getBlob: vi.fn(),
      };
    });

    const { officeApi } = await import('./office');
    const { getBlob } = await import('../core');
    vi.mocked(getBlob).mockResolvedValue(new Blob(['preview'], { type: 'text/html' }));

    await officeApi.getWorkspacePreview('agent-1', 'workspace/demo.docx');
    await officeApi.getArtifactPreview('agent-1', 'artifact-1', {
      operatorView: true,
      reason: 'Agent session administration',
    });

    expect(getBlob).toHaveBeenCalledWith('/agents/agent-1/office/preview?path=workspace%2Fdemo.docx');
    expect(getBlob).toHaveBeenCalledWith(
      '/agents/agent-1/office/artifacts/artifact-1/preview?operator_view=true&operator_reason=Agent+session+administration',
    );
    expect(String(vi.mocked(getBlob).mock.calls)).not.toContain('token=');
  });
});
