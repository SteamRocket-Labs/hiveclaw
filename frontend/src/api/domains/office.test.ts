import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

describe('office API adapter', () => {
  beforeEach(() => {
    vi.resetModules();
  });

  afterEach(() => {
    vi.restoreAllMocks();
  });

  it('builds agent-scoped ONLYOFFICE URLs', async () => {
    vi.doMock('../core', async () => {
      const actual = await vi.importActual<typeof import('../core')>('../core');
      return {
        ...actual,
        get: vi.fn(),
        post: vi.fn(),
      };
    });

    const { officeApi } = await import('./office');
    const { get, post } = await import('../core');
    vi.mocked(get).mockResolvedValue({ enabled: false, reason: 'onlyoffice_not_configured' } as never);
    vi.mocked(post).mockResolvedValue({ status: 'ok', path: 'workspace/demo.docx' } as never);

    await officeApi.getEditorConfig('agent-1', 'workspace/demo.docx', 'edit');
    await officeApi.createDocument('agent-1', { path: 'workspace/demo.docx', kind: 'docx' });
    await officeApi.forceSave('agent-1', 'workspace/demo.docx');

    expect(get).toHaveBeenCalledWith('/agents/agent-1/office/editor-config?path=workspace%2Fdemo.docx&mode=edit');
    expect(post).toHaveBeenCalledWith('/agents/agent-1/office/documents', {
      path: 'workspace/demo.docx',
      kind: 'docx',
    });
    expect(post).toHaveBeenCalledWith('/agents/agent-1/office/force-save', {
      path: 'workspace/demo.docx',
    });
  });
});
