// @vitest-environment jsdom

import React from 'react';
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';

import { ApiError } from '../../api/core';
import { fileApi } from '../../api/domains/files';
import { ArtifactPreviewPanel } from './ArtifactSurface';
import type { ChatArtifactPart } from './chatRuntime';
import { useArtifactPreview } from './useArtifactPreview';

vi.mock('react-i18next', () => ({
  useTranslation: () => ({ t: (_key: string, fallback?: string) => fallback || _key }),
}));

vi.mock('./AgentWorkspaceSection', () => ({ default: () => null }));

afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
});

function Preview({ artifact }: { artifact: ChatArtifactPart }) {
  const { artifactPreview, closeArtifactPreview, openArtifact } = useArtifactPreview({
    authorityIdentity: 'same-reader-and-session',
    effectiveAgentId: 'agent-1',
    guardOperatorRead: async (request) => request,
    operatorView: false,
  });
  return <>
    <button onClick={() => void openArtifact(artifact)}>Open saved file</button>
    <button onClick={closeArtifactPreview}>Close saved file</button>
    {artifactPreview && <ArtifactPreviewPanel
      preview={artifactPreview}
      onClose={closeArtifactPreview}
      onRetry={() => void openArtifact(artifact)}
      t={((_key: string, fallback?: string) => fallback || _key) as never}
    />}
  </>;
}

describe('current source authority for saved text previews', () => {
  it.each([
    ['artifact', 403], ['artifact', 503],
    ['workspace', 403], ['workspace', 503],
  ] as const)('does not display cached bytes after %s read returns %s, and remains recoverable', async (surface, status) => {
    const artifact: ChatArtifactPart = {
      id: surface === 'artifact' ? 'artifact-1' : undefined,
      name: 'release.md', path: 'workspace/release.md', previewKind: 'markdown',
      previewSnapshotContent: 'CACHED_RELEASE_MARKER',
    };
    const request = vi.spyOn(fileApi, surface === 'artifact' ? 'readArtifact' : 'read')
      .mockResolvedValueOnce({ path: artifact.path, content: 'AUTHORIZED_SERVER_MARKER', uses_snapshot: true })
      .mockRejectedValueOnce(new ApiError(status, 'The current source cannot be read'))
      .mockResolvedValueOnce({ path: artifact.path, content: 'RESTORED_SERVER_MARKER', uses_snapshot: true });
    render(<Preview artifact={artifact} />);

    fireEvent.click(screen.getByRole('button', { name: 'Open saved file' }));
    await screen.findByText('AUTHORIZED_SERVER_MARKER');
    expect(screen.queryByText('CACHED_RELEASE_MARKER')).toBeNull();
    fireEvent.click(screen.getByRole('button', { name: 'Close saved file' }));
    expect(screen.queryByText('AUTHORIZED_SERVER_MARKER')).toBeNull();

    fireEvent.click(screen.getByRole('button', { name: 'Open saved file' }));
    await screen.findByText('The current source cannot be read');
    expect(screen.queryByText('CACHED_RELEASE_MARKER')).toBeNull();
    expect(screen.queryByText('AUTHORIZED_SERVER_MARKER')).toBeNull();
    expect(request).toHaveBeenCalledTimes(2);

    fireEvent.click(screen.getByRole('button', { name: 'Retry' }));
    await screen.findByText('RESTORED_SERVER_MARKER');
    await waitFor(() => expect(request).toHaveBeenCalledTimes(3));
    expect(screen.queryByText('The current source cannot be read')).toBeNull();
    expect(screen.queryByText('CACHED_RELEASE_MARKER')).toBeNull();
  });
});
