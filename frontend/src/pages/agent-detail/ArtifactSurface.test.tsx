import React from 'react';
import { renderToStaticMarkup } from 'react-dom/server';
import { describe, expect, it, vi } from 'vitest';

import { ArtifactPreviewPanel, getArtifactOpenMode, getEffectiveArtifactPreviewKind, loadOfficeArtifactPreview } from './ArtifactSurface';
import { officeApi } from '../../api/domains/office';
import nginxConf from '../../../nginx.conf?raw';

vi.mock('react-i18next', () => ({
  useTranslation: () => ({ t: (_key: string, fallback?: string) => fallback || _key }),
}));

describe('Office artifact preview surface', () => {
  it('allows local blob frames in every deployed CSP without allowing external frames or weakening scripts', () => {
    const policies = [...nginxConf.matchAll(/add_header Content-Security-Policy "([^"]+)"/g)].map((match) => match[1]);
    expect(policies).toHaveLength(3);
    for (const policy of policies) {
      expect(policy.match(/(?:^|;)\s*frame-src ([^;]+);/)?.[1]).toBe("'self' blob:");
      expect(policy).toContain("script-src 'self';");
    }
  });

  it('recognizes a canonical snapshot storage reference even without legacy snapshot_hash', async () => {
    const request = vi.spyOn(officeApi, 'getArtifactPreview').mockResolvedValue(new Blob(['<html>Saved</html>']));
    try {
      const result = await loadOfficeArtifactPreview({ id: 'artifact-1', name: 'report.xlsx', path: 'workspace/report.xlsx',
        snapshotStoragePath: 'runtime_artifacts/chat_artifact_snapshots/session/run/revision.xlsx' }, 'agent-1');
      expect(result.usingSnapshot).toBe(true);
      expect(result.legacyCurrentFileFallback).toBe(false);
      expect(request).toHaveBeenCalledWith('agent-1', 'artifact-1', undefined);
      URL.revokeObjectURL(result.url!);
    } finally { request.mockRestore(); }
  });
  it('routes Office artifacts into the inspector instead of direct download', () => {
    expect(getArtifactOpenMode({
      name: 'deck.pptx',
      path: 'workspace/deck.pptx',
      previewKind: 'office',
    })).toBe('inspector_preview');
    expect(getEffectiveArtifactPreviewKind({
      name: 'report.docx',
      path: 'workspace/report.docx',
    })).toBe('office');
  });

  it('wires snapshot and current Workspace Office previews through authenticated endpoints', async () => {
    const fsModuleId = 'node:fs';
    const { readFileSync } = (await import(/* @vite-ignore */ fsModuleId)) as {
      readFileSync: (path: URL, encoding: string) => string;
    };
    const chatSource = readFileSync(new URL('./AgentChatSection.tsx', import.meta.url), 'utf8');
    const artifactSource = readFileSync(new URL('./ArtifactSurface.tsx', import.meta.url), 'utf8');
    const previewSource = readFileSync(new URL('./useArtifactPreview.ts', import.meta.url), 'utf8');

    expect(artifactSource).toContain('officeApi.getArtifactPreview');
    expect(artifactSource).toContain('officeApi.getWorkspacePreview');
    expect(artifactSource).toContain('artifact.id');
    expect(chatSource).toContain('onOpenDocument={openArtifact}');
    expect(previewSource).toContain('URL.revokeObjectURL(url)');
  });

  it('renders Office HTML in a credential-isolated iframe', () => {
    const markup = renderToStaticMarkup(
      <ArtifactPreviewPanel
        preview={{
          artifact: {
            id: 'artifact-1',
            name: 'deck.pptx',
            path: 'workspace/deck.pptx',
            previewKind: 'office',
          },
          url: 'blob:hive-office-preview',
          usingSnapshot: true,
        }}
        onClose={vi.fn()}
        onDownload={vi.fn()}
        t={((_key: string, fallback?: string) => fallback || _key) as never}
      />,
    );

    expect(markup).toContain('<iframe');
    expect(markup).toContain('src="blob:hive-office-preview"');
    expect(markup).toContain('sandbox="allow-scripts"');
    expect(markup).toContain('referrerPolicy="no-referrer"');
    expect(markup).not.toContain('allow-same-origin');
    expect(markup).not.toContain('allow-forms');
    expect(markup).toContain('Previewing saved session snapshot');
    expect(markup).toContain('aria-label="Download deck.pptx"');
  });
});
