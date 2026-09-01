import React from 'react';
import { useTranslation } from 'react-i18next';

import { fileApi, type ResourceAuthorityOptions } from '../../api/domains/files';
import { showAppToast } from '../../components/AppDialogs';
import { saveBlob } from '../../utils/authenticatedResource';
import {
  artifactWorkspaceAgentId,
  downloadChatArtifact,
  getArtifactOpenMode,
  getEffectiveArtifactPreviewKind,
  loadOfficeArtifactPreview,
  type ArtifactPreviewState,
} from './ArtifactSurface';
import type { ChatArtifactPart } from './chatRuntime';

type GuardOperatorRead = <T>(request: Promise<T>) => Promise<T>;

interface UseArtifactPreviewOptions {
  authorityIdentity: string;
  effectiveAgentId: string | null;
  guardOperatorRead: GuardOperatorRead;
  operatorView: boolean;
  resourceOperatorOptions?: ResourceAuthorityOptions;
}

interface AuthorityBoundPreview {
  authorityIdentity: string;
  preview: ArtifactPreviewState;
}

export function useArtifactPreview({
  authorityIdentity,
  effectiveAgentId,
  guardOperatorRead,
  operatorView,
  resourceOperatorOptions,
}: UseArtifactPreviewOptions) {
  const { t } = useTranslation();
  const [authorityBoundPreview, setAuthorityBoundPreview] = React.useState<AuthorityBoundPreview | null>(null);
  const authorityIdentityRef = React.useRef(authorityIdentity);
  const requestGenerationRef = React.useRef(0);
  const mountedRef = React.useRef(false);

  if (authorityIdentityRef.current !== authorityIdentity) {
    authorityIdentityRef.current = authorityIdentity;
    requestGenerationRef.current += 1;
  }
  const artifactPreview = authorityBoundPreview?.authorityIdentity === authorityIdentity
    ? authorityBoundPreview.preview
    : null;
  const setArtifactPreview = React.useCallback((preview: ArtifactPreviewState | null) => {
    setAuthorityBoundPreview(preview ? {
      authorityIdentity: authorityIdentityRef.current,
      preview,
    } : null);
  }, []);

  React.useEffect(() => {
    mountedRef.current = true;
    return () => {
      mountedRef.current = false;
      requestGenerationRef.current += 1;
    };
  }, []);

  React.useEffect(() => {
    setArtifactPreview(null);
  }, [authorityIdentity]);

  React.useEffect(() => {
    const url = authorityBoundPreview?.preview.url;
    return () => {
      if (url?.startsWith('blob:')) URL.revokeObjectURL(url);
    };
  }, [authorityBoundPreview?.preview.url]);

  const closeArtifactPreview = React.useCallback(() => {
    requestGenerationRef.current += 1;
    setArtifactPreview(null);
  }, []);

  const downloadArtifactFile = React.useCallback(async (artifact: ChatArtifactPart) => {
    const artifactAgentId = artifactWorkspaceAgentId(artifact, effectiveAgentId);
    if (!artifactAgentId) return;
    if (!operatorView) {
      await downloadChatArtifact(artifact, artifactAgentId, undefined, t);
      return;
    }
    const authorityIdentityAtRequest = authorityIdentityRef.current;
    const requestGeneration = requestGenerationRef.current;
    try {
      const blob = await guardOperatorRead(
        artifact.id
          ? fileApi.downloadArtifact(artifactAgentId, artifact.id, resourceOperatorOptions)
          : fileApi.download(artifactAgentId, artifact.path, resourceOperatorOptions),
      );
      if (
        !mountedRef.current
        || authorityIdentityRef.current !== authorityIdentityAtRequest
        || requestGenerationRef.current !== requestGeneration
      ) return;
      saveBlob(blob, artifact.name);
    } catch (error) {
      if (isForbiddenOperatorResponse(error)) return;
      showAppToast(t('agent.chat.artifacts.downloadFailed', 'Download failed: {{message}}', {
        message: error instanceof Error ? error.message : String(error),
      }), 'error');
    }
  }, [effectiveAgentId, guardOperatorRead, operatorView, resourceOperatorOptions, t]);

  const openArtifact = React.useCallback(async (artifact: ChatArtifactPart) => {
    const artifactAgentId = artifactWorkspaceAgentId(artifact, effectiveAgentId);
    if (!artifactAgentId) return;
    const authorityIdentityAtRequest = authorityIdentityRef.current;
    const requestGeneration = ++requestGenerationRef.current;
    const requestIsCurrent = () => Boolean(
      mountedRef.current
      && requestGenerationRef.current === requestGeneration
      && authorityIdentityRef.current === authorityIdentityAtRequest,
    );
    const fetchArtifactBlob = () => artifact.id
      ? fileApi.downloadArtifact(artifactAgentId, artifact.id, resourceOperatorOptions)
      : fileApi.download(artifactAgentId, artifact.path, resourceOperatorOptions);
    if (getArtifactOpenMode(artifact) === 'download') {
      await downloadArtifactFile(artifact);
      return;
    }

    const previewKind = getEffectiveArtifactPreviewKind(artifact);
    if (previewKind === 'office') {
      if (requestIsCurrent()) setArtifactPreview({ artifact, loading: true });
      try {
        const preview = await guardOperatorRead(
          loadOfficeArtifactPreview(artifact, artifactAgentId, resourceOperatorOptions),
        );
        if (!requestIsCurrent()) {
          if (preview.url?.startsWith('blob:')) URL.revokeObjectURL(preview.url);
          return;
        }
        setArtifactPreview(preview);
      } catch (error) {
        if (!requestIsCurrent()) return;
        if (operatorView && isForbiddenOperatorResponse(error)) return;
        setArtifactPreview({
          artifact,
          error: error instanceof Error ? error.message : String(error),
        });
      }
      return;
    }
    if (previewKind === 'markdown' || previewKind === 'text' || !previewKind) {
      if (requestIsCurrent()) setArtifactPreview({ artifact, loading: true });
      try {
        const response = artifact.id
          ? await guardOperatorRead(fileApi.readArtifact(artifactAgentId, artifact.id, resourceOperatorOptions))
          : await guardOperatorRead(fileApi.read(artifactAgentId, artifact.path, resourceOperatorOptions));
        if (!requestIsCurrent()) return;
        setArtifactPreview({
          artifact,
          content: response.content || '',
          usingSnapshot: Boolean(response.uses_snapshot || artifact.snapshotHash),
          workspaceChanged: Boolean(response.workspace_changed),
          legacyCurrentFileFallback: Boolean(response.legacy_current_file_fallback),
        });
      } catch (error) {
        if (!requestIsCurrent()) return;
        if (operatorView && isForbiddenOperatorResponse(error)) return;
        if (typeof artifact.previewSnapshotContent === 'string') {
          setArtifactPreview({ artifact, content: artifact.previewSnapshotContent, usingSnapshot: true });
          return;
        }
        setArtifactPreview({
          artifact,
          error: error instanceof Error && !String(error.message || '').includes('File not found')
            ? error.message
            : t('agent.chat.artifacts.missingNoSnapshot', 'This file is no longer available in the workspace.'),
        });
      }
      return;
    }

    try {
      const blob = await guardOperatorRead(fetchArtifactBlob());
      const url = URL.createObjectURL(blob);
      if (!requestIsCurrent()) {
        URL.revokeObjectURL(url);
        return;
      }
      setArtifactPreview({ artifact, url });
    } catch (error) {
      if (!requestIsCurrent()) return;
      if (operatorView && isForbiddenOperatorResponse(error)) return;
      setArtifactPreview({
        artifact,
        error: error instanceof Error ? error.message : String(error),
      });
    }
  }, [downloadArtifactFile, effectiveAgentId, guardOperatorRead, operatorView, resourceOperatorOptions, t]);

  return { artifactPreview, closeArtifactPreview, downloadArtifactFile, openArtifact };
}

function isForbiddenOperatorResponse(error: unknown): boolean {
  if (!error || typeof error !== 'object') return false;
  return Number((error as { status?: unknown }).status) === 403;
}
