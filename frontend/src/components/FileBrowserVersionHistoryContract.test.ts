import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { describe, expect, it } from 'vitest';

function source(relativePath: string): string {
  return readFileSync(fileURLToPath(new URL(relativePath, import.meta.url)), 'utf8');
}

describe('FileBrowser version history live-path contract', () => {
  it('keeps history optional and sends exact-current-state restore requests', () => {
    const browser = source('./FileBrowser.tsx');
    const controller = source('./useFileVersionHistory.ts');

    expect(browser).toContain('versions?:');
    expect(browser).toContain('readVersion?:');
    expect(browser).toContain('restoreVersion?:');
    expect(browser).toContain('downloadVersion?:');
    expect(browser).toContain('<FileVersionHistoryPanel');
    expect(controller).toContain('buildFileVersionRestoreRequest(historyPage)');
  });

  it('wires only the Agent workspace adapter to the checkpoint version API', () => {
    const workspace = source('../pages/agent-detail/AgentWorkspaceSection.tsx');

    expect(workspace).toContain('versions: (path, offset, limit)');
    expect(workspace).toContain('fileApi.versions(agentId, path, offset, limit, authority)');
    expect(workspace).toContain('readVersion: (path, versionId)');
    expect(workspace).toContain('restoreVersion: (path, versionId, request)');
    expect(workspace).toContain('downloadVersion: (path, versionId)');
  });
});
