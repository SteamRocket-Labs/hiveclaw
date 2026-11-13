import { useCallback, useState } from 'react';
import { useTranslation } from 'react-i18next';
import { IconDownload, IconExternalLink, IconFileText, IconX } from '@tabler/icons-react';

import MarkdownRenderer from '../../components/MarkdownRenderer';
import { fileApi, type ResourceAuthorityOptions } from '../../api/domains/files';
import { officeApi } from '../../api/domains/office';
import { saveBlob } from '../../utils/authenticatedResource';
import { showAppToast } from '../../components/AppDialogs';
import type { ChatArtifactPart } from './chatRuntime';
import AgentWorkspaceSection from './AgentWorkspaceSection';
import ArtifactContinuation, { type ArtifactContinuationContext } from './ArtifactContinuation';
import OriginalUploadAction from './OriginalUploadAction';
import type { FileEditorState } from '../../components/FileBrowser';
import HtmlArtifactPreview from './HtmlArtifactPreview';
import './ArtifactContinuation.css';
import { formatArtifactSize, getEffectiveArtifactPreviewKind, isPendingEmptyArtifactPreview,
  isUserFacingDeliveryArtifact, artifactWorkspaceAgentId, type ArtifactDeliveryContext } from './artifactPresentation';
export { formatArtifactSize, getEffectiveArtifactPreviewKind, getArtifactOpenMode, isPendingEmptyArtifactPreview,
  isUserFacingDeliveryArtifact, artifactWorkspaceAgentId, shortArtifactAgentId, artifactContributorLabel } from './artifactPresentation';
export type { ArtifactOpenMode, ArtifactDeliveryContext } from './artifactPresentation';

type Translate = ReturnType<typeof useTranslation>['t'];

function artifactTypeLabel(artifact: ChatArtifactPart, t: Translate): string {
  if (artifact.source !== 'human_input' && getEffectiveArtifactPreviewKind(artifact) === 'html') {
    return t('agent.chat.artifacts.preview', 'Preview');
  }
  const extension = artifact.name.split('.').pop()?.toLowerCase();
  const raw = artifact.previewKind || (artifact.source === 'human_input' ? extension : undefined);
  if (!raw) return '';
  const kind = extension === 'csv' ? 'csv' : ['md', 'markdown'].includes(raw) ? 'markdown' : raw;
  const fallback = t('agent.chat.artifacts.fileTypes.other', 'File');
  return t(`agent.chat.artifacts.fileTypes.${kind}`, fallback);
}

export type ArtifactPreviewState = {
  artifact: ChatArtifactPart;
  content?: string;
  url?: string;
  loading?: boolean;
  error?: string;
  usingSnapshot?: boolean;
  workspaceChanged?: boolean;
  legacyCurrentFileFallback?: boolean;
};

export async function loadOfficeArtifactPreview(
  artifact: ChatArtifactPart,
  artifactAgentId: string,
  authority?: ResourceAuthorityOptions,
): Promise<ArtifactPreviewState> {
  const blob = artifact.id
    ? await officeApi.getArtifactPreview(artifactAgentId, artifact.id, authority)
    : await officeApi.getWorkspacePreview(artifactAgentId, artifact.path, authority);
  const usingSnapshot = Boolean(artifact.id && (artifact.snapshotStoragePath || artifact.snapshotHash));
  return {
    artifact,
    url: URL.createObjectURL(blob),
    usingSnapshot,
    legacyCurrentFileFallback: Boolean(artifact.id && !usingSnapshot),
  };
}

export async function downloadChatArtifact(
  artifact: ChatArtifactPart,
  artifactAgentId: string,
  authority: ResourceAuthorityOptions | undefined,
  t: Translate,
): Promise<void> {
  try {
    const blob = artifact.id
      ? await fileApi.downloadArtifact(artifactAgentId, artifact.id, authority)
      : await fileApi.download(artifactAgentId, artifact.path, authority);
    saveBlob(blob, artifact.name);
  } catch (error) {
    showAppToast(t('agent.chat.artifacts.downloadFailed', 'Download failed: {{message}}', {
      message: error instanceof Error ? error.message : String(error),
    }), 'error');
  }
}

export function ArtifactPreviewPanel({
  preview,
  agentId,
  workspaceAuthority,
  onWorkspaceAuthorityDenied,
  onClose,
  onRetry,
  onDownload,
  continuation,
  t,
}: {
  preview: ArtifactPreviewState;
  agentId?: string;
  workspaceAuthority?: ResourceAuthorityOptions;
  onWorkspaceAuthorityDenied?: (error: unknown) => void;
  onClose: () => void;
  onRetry?: () => void;
  onDownload?: () => void;
  continuation?: ArtifactContinuationContext;
  t: Translate;
}) {
  const previewKind = getEffectiveArtifactPreviewKind(preview.artifact);
  const isSvg = preview.artifact.mimeType?.split(';')[0].trim().toLowerCase() === 'image/svg+xml'
    || [preview.artifact.name, preview.artifact.path].some(value => value.toLowerCase().endsWith('.svg'));
  const workspaceAgentId = artifactWorkspaceAgentId(preview.artifact, agentId);
  const fileIdentity = `${workspaceAgentId}:${preview.artifact.path}:${workspaceAuthority?.operatorView ? workspaceAuthority.reason : 'owner'}`;
  const [openedFile, setOpenedFile] = useState<string | null>(null);
  const [showCurrent, setShowCurrent] = useState(false);
  const currentFileOpen = openedFile === fileIdentity && showCurrent;
  const canOpenCurrent = workspaceAgentId && preview.artifact.path.startsWith('workspace/') && ['markdown', 'text', 'office'].includes(previewKind);
  const [editor, setEditor] = useState<{ identity: string; version: string; state: FileEditorState } | null>(null);
  const keepDraft = editor?.identity === fileIdentity && (editor.state.dirty || editor.state.busy || editor.state.unavailable);
  const editorIdentity = keepDraft ? editor.version : `${fileIdentity}:${preview.artifact.id}:${preview.artifact.snapshotHash}`;
  const editorChanged = useCallback((state: FileEditorState) => setEditor({ identity: fileIdentity, version: editorIdentity, state }), [fileIdentity, editorIdentity]);
  const currentEditor = editor?.identity === fileIdentity && editor.version === editorIdentity ? editor.state : null;
  const continuationBlocked = Boolean(currentEditor && (currentEditor.dirty || currentEditor.busy || currentEditor.unavailable));
  const showPendingEmptyPreview = isPendingEmptyArtifactPreview(
    preview.artifact,
    preview.content,
    preview.loading,
    preview.error,
  );
  const pendingEmptyPreview = (
    <div
      style={{
        border: '1px dashed var(--border-subtle)',
        borderRadius: '7px',
        padding: '14px',
        color: 'var(--text-tertiary)',
        fontSize: '12px',
        lineHeight: 1.6,
        background: 'var(--bg-primary)',
      }}
    >
      {t(
        'agent.chat.artifacts.emptyPending',
        'This file is empty for now. Its content will appear here after the session permission is approved or the write finishes.',
      )}
    </div>
  );
  return (
    <div
      data-testid="session-artifact-preview-inspector"
      role="complementary"
      aria-label={preview.artifact.name}
      style={{
        position: 'fixed',
        zIndex: 80,
        top: '76px',
        right: '16px',
        bottom: '112px',
        width: 'min(680px, calc(100vw - 32px))',
        display: 'flex',
        pointerEvents: 'none',
      }}
    >
      <section
        data-testid="session-artifact-inspector"
        style={{
          width: '100%',
          minHeight: 0,
          border: '1px solid var(--border-subtle)',
          borderRadius: '10px',
          background: 'var(--bg-secondary)',
          boxShadow: '0 18px 64px rgba(15, 23, 42, 0.24)',
          overflow: 'hidden',
          display: 'flex',
          flexDirection: 'column',
          pointerEvents: 'auto',
        }}
      >
        <div style={{ display: 'flex', alignItems: 'center', gap: '8px', padding: '9px 10px', borderBottom: '1px solid var(--border-subtle)' }}>
          <IconFileText size={15} color="var(--text-tertiary)" />
          <div style={{ minWidth: 0, flex: 1 }}>
            <div style={{ fontSize: '12px', fontWeight: 700, color: 'var(--text-primary)', overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>
              {preview.artifact.name}
            </div>
            <div style={{ fontSize: '10px', color: 'var(--text-tertiary)' }}>
              {currentFileOpen
                ? t('agent.chat.artifacts.currentFile', 'Current workspace file')
                : preview.usingSnapshot
                ? t('agent.chat.artifacts.snapshotPreview', 'Previewing saved session snapshot')
                : preview.legacyCurrentFileFallback
                  ? t('agent.chat.artifacts.legacyCurrentFilePreview', 'Previewing current workspace file; no delivery snapshot exists')
                : t('agent.chat.artifacts.preview', 'Preview')}
              {preview.workspaceChanged ? ` · ${t('agent.chat.artifacts.workspaceChanged', 'Workspace file changed after delivery')}` : ''}
            </div>
          </div>
          {preview.url && previewKind !== 'office' && !isSvg && (
            <a
              href={preview.url}
              target="_blank"
              rel="noreferrer"
              style={{ color: 'var(--text-tertiary)', display: 'inline-flex', alignItems: 'center' }}
              title={t('agent.chat.artifacts.openInNewTab', 'Open in new tab')}
            >
              <IconExternalLink size={14} />
            </a>
          )}
          {canOpenCurrent && (
            <button type="button" className="btn btn-secondary btn-sm" aria-pressed={currentFileOpen}
              onClick={() => { setOpenedFile(fileIdentity); setShowCurrent(!currentFileOpen); }}>
              {currentFileOpen
                ? t('agent.chat.artifacts.backToDelivery', 'Back to delivery')
                : t('agent.chat.artifacts.openCurrentFile', 'Open current file')}
            </button>
          )}
          {onDownload && !currentFileOpen && (
            <button
              type="button"
              onClick={onDownload}
              aria-label={`${t('agent.chat.artifacts.download', 'Download')} ${preview.artifact.name}`}
              title={t('agent.chat.artifacts.download', 'Download')}
              style={{
                border: '1px solid var(--border-subtle)',
                background: 'transparent',
                borderRadius: '6px',
                padding: '3px',
                color: 'var(--text-secondary)',
                cursor: 'pointer',
                display: 'inline-flex',
                alignItems: 'center',
                justifyContent: 'center',
              }}
            >
              <IconDownload size={13} />
            </button>
          )}
          <button
            type="button"
            onClick={onClose}
            aria-label={t('common.close', 'Close')}
            title={t('common.close', 'Close')}
            style={{
              border: '1px solid var(--border-subtle)',
              background: 'transparent',
              borderRadius: '6px',
              padding: '3px',
              color: 'var(--text-secondary)',
              cursor: 'pointer',
              display: 'inline-flex',
              alignItems: 'center',
              justifyContent: 'center',
            }}
          >
            <IconX size={13} />
          </button>
        </div>
        <div style={{ padding: '12px', overflow: 'auto', flex: 1, minHeight: 0 }}>
          {openedFile === fileIdentity && workspaceAgentId && (
            <div hidden={!currentFileOpen}>
              <AgentWorkspaceSection key={editorIdentity} agentId={workspaceAgentId} singleFile={preview.artifact.path}
                canUseOperatorView={Boolean(workspaceAuthority?.operatorView)} operatorOnly={Boolean(workspaceAuthority?.operatorView)}
                operatorReason={workspaceAuthority?.reason} onOperatorAuthorityDenied={onWorkspaceAuthorityDenied}
                onEditorStateChange={editorChanged} />
            </div>
          )}
          <div hidden={currentFileOpen}>
          {preview.loading ? (
            <div style={{ fontSize: '12px', color: 'var(--text-tertiary)' }}>
              {t('common.loading', 'Loading')}
            </div>
          ) : preview.error ? (
            <div style={{ display: 'grid', gap: '10px', fontSize: '12px', color: 'var(--danger-text)' }}>
              <span>{preview.error}</span>
              {onRetry && (
                <button type="button" className="btn btn-secondary" onClick={onRetry} style={{ justifySelf: 'start' }}>
                  {t('common.retry', 'Retry')}
                </button>
              )}
            </div>
          ) : previewKind === 'image' && preview.url ? (
            <img
              src={preview.url}
              alt={preview.artifact.name}
              style={{ maxWidth: '100%', maxHeight: '62vh', borderRadius: '6px', border: '1px solid var(--border-subtle)' }}
            />
          ) : previewKind === 'pdf' && preview.url ? (
            <iframe
              title={preview.artifact.name}
              src={preview.url}
              style={{ width: '100%', height: '62vh', border: '1px solid var(--border-subtle)', borderRadius: '6px' }}
            />
          ) : previewKind === 'office' && preview.url ? (
            <iframe
              title={preview.artifact.name}
              src={preview.url}
              sandbox="allow-scripts"
              referrerPolicy="no-referrer"
              style={{ width: '100%', height: '62vh', border: '1px solid var(--border-subtle)', borderRadius: '6px', background: '#fff' }}
            />
          ) : previewKind === 'html' ? (
            showPendingEmptyPreview ? pendingEmptyPreview : <HtmlArtifactPreview key={`${fileIdentity}:${preview.artifact.id}:${preview.artifact.snapshotHash}`}
              content={preview.content || ''} name={preview.artifact.name} t={t} />
          ) : previewKind === 'markdown' ? (
            <div style={{ fontSize: '12px', lineHeight: 1.6 }}>
              {showPendingEmptyPreview ? pendingEmptyPreview : <MarkdownRenderer content={preview.content || ''} />}
            </div>
          ) : (
            <pre
              style={{
                margin: 0,
                fontSize: '11px',
                lineHeight: 1.55,
                whiteSpace: 'pre-wrap',
                wordBreak: 'break-word',
                color: 'var(--text-secondary)',
              }}
            >
              {showPendingEmptyPreview ? t(
                'agent.chat.artifacts.emptyPending',
                'This file is empty for now. Its content will appear here after the session permission is approved or the write finishes.',
              ) : preview.content || ''}
            </pre>
          )}
          </div>
        </div>
        {continuation && workspaceAgentId && preview.artifact.path.startsWith('workspace/') && !workspaceAuthority?.operatorView && (
          <ArtifactContinuation key={`${fileIdentity}:${continuation.sessionId}`} artifact={preview.artifact}
            workspaceAgentId={workspaceAgentId} context={continuation} blocked={continuationBlocked}
            lastSavedHash={currentEditor?.lastSavedHash} />
        )}
      </section>
    </div>
  );
}

export function ArtifactCards({
  agentId,
  artifacts,
  onOpenArtifact,
  onDownloadArtifact,
  context = 'assistant',
  operatorView = false,
  operatorReason,
  viewerUserId,
}: {
  agentId?: string | null;
  artifacts?: ChatArtifactPart[];
  onOpenArtifact?: (artifact: ChatArtifactPart) => void;
  onDownloadArtifact?: (artifact: ChatArtifactPart) => void | Promise<unknown>;
  context?: ArtifactDeliveryContext;
  operatorView?: boolean;
  operatorReason?: string;
  viewerUserId?: string;
}) {
  const { t } = useTranslation();
  const visibleArtifacts = (artifacts || []).filter((artifact) => (
    artifact.path && isUserFacingDeliveryArtifact(artifact, context)
  ));
  if (!agentId || visibleArtifacts.length === 0) return null;

  // Codex 式聚合卡：一个容器承载全部交付物，每文件一行，动作 hover 浮现。
  return (
    <div className="chat-artifacts" data-testid="chat-artifacts-card">
      <div className="chat-artifacts-title">
        <IconFileText size={13} />
        {t('agent.chat.artifacts.count', '{{count}} files', { count: visibleArtifacts.length })}
      </div>
      {visibleArtifacts.map((artifact) => {
        const downloadAgentId = artifactWorkspaceAgentId(artifact, agentId);
        const authority = operatorView
          ? { operatorView: true, reason: operatorReason }
          : undefined;
        const size = formatArtifactSize(artifact.size);
        const openArtifact = () => onOpenArtifact?.(artifact);
        const downloadArtifact = () => {
          if (onDownloadArtifact) return onDownloadArtifact(artifact);
          return downloadAgentId
            ? downloadChatArtifact(artifact, downloadAgentId, authority, t)
            : Promise.resolve();
        };
        return (
          <div
            key={`${artifact.id || artifact.path}`}
            className={artifact.source === 'human_input' ? 'chat-artifact-row is-upload' : 'chat-artifact-row'}
          >
            <button
              type="button"
              data-testid="chat-artifact-row-open"
              aria-label={t('agent.chat.artifacts.openNamed', 'Open {{name}}', { name: artifact.name })}
              className="chat-artifact-main"
              onClick={openArtifact}
            >
              <IconFileText size={14} className="chat-artifact-icon" />
              <span className="chat-artifact-name" title={artifact.name}>{artifact.source === 'human_input' && artifact.name.lastIndexOf('.') > 0
                ? <><span className="chat-artifact-stem">{artifact.name.slice(0, artifact.name.lastIndexOf('.'))}</span><span className="chat-artifact-extension">{artifact.name.slice(artifact.name.lastIndexOf('.'))}</span></>
                : artifact.name}</span>
              <span className="chat-artifact-meta">
                {[artifactTypeLabel(artifact, t), size].filter(Boolean).join(' · ')}
              </span>
            </button>
            <span className="chat-artifact-actions">
              <button
                type="button"
                data-testid="chat-artifact-open"
                className="chat-artifact-action"
                onClick={(event) => {
                  event.stopPropagation();
                  openArtifact();
                }}
              >
                <IconExternalLink size={12} />
                {t('agent.chat.artifacts.open', 'Open')}
              </button>
              <button
                type="button"
                className="chat-artifact-action"
                onClick={(event) => {
                  event.stopPropagation();
                  void downloadArtifact();
                }}
              >
                <IconDownload size={12} />
                {t('agent.chat.artifacts.download', 'Download')}
              </button>
              <OriginalUploadAction agentId={downloadAgentId || agentId} path={artifact.path} name={artifact.name}
                original={artifact.originalUpload} viewerUserId={viewerUserId} discover={artifact.source === 'human_input'} />
            </span>
          </div>
        );
      })}
    </div>
  );
}
