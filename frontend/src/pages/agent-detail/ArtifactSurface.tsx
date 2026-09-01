import { useTranslation } from 'react-i18next';
import { IconDownload, IconExternalLink, IconFileText, IconX } from '@tabler/icons-react';

import MarkdownRenderer from '../../components/MarkdownRenderer';
import { fileApi, type ResourceAuthorityOptions } from '../../api/domains/files';
import { officeApi } from '../../api/domains/office';
import { saveBlob } from '../../utils/authenticatedResource';
import { showAppToast } from '../../components/AppDialogs';
import type { ChatArtifactPart } from './chatRuntime';

type Translate = ReturnType<typeof useTranslation>['t'];

function stringValue(value: unknown): string {
  return value == null ? '' : String(value);
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

export function formatArtifactSize(size: number | undefined): string | null {
  if (typeof size !== 'number' || !Number.isFinite(size) || size < 0) return null;
  if (size < 1024) return `${size} B`;
  if (size < 1024 * 1024) return `${Math.round(size / 1024)} KB`;
  return `${(size / (1024 * 1024)).toFixed(1)} MB`;
}

export type ArtifactOpenMode = 'inspector_preview' | 'download';

export function getEffectiveArtifactPreviewKind(artifact: Pick<ChatArtifactPart, 'name' | 'path' | 'previewKind'>): string {
  const explicit = artifact.previewKind?.toLowerCase();
  if (explicit) return explicit;
  const suffix = (artifact.name || artifact.path).split('.').pop()?.toLowerCase() || '';
  if (['md', 'markdown'].includes(suffix)) return 'markdown';
  if (['txt', 'csv', 'json', 'jsonl', 'log', 'xml', 'yaml', 'yml'].includes(suffix)) return 'text';
  if (['png', 'jpg', 'jpeg', 'gif', 'webp', 'svg'].includes(suffix)) return 'image';
  if (suffix === 'pdf') return 'pdf';
  if (['docx', 'xlsx', 'pptx'].includes(suffix)) return 'office';
  return 'download';
}

export function getArtifactOpenMode(artifact: Pick<ChatArtifactPart, 'name' | 'path' | 'previewKind'>): ArtifactOpenMode {
  const previewKind = getEffectiveArtifactPreviewKind(artifact);
  if (previewKind && ['markdown', 'text', 'image', 'pdf', 'office'].includes(previewKind)) {
    return 'inspector_preview';
  }
  return 'download';
}

export function isPendingEmptyArtifactPreview(
  artifact: Partial<ChatArtifactPart>,
  content?: string,
  loading?: boolean,
  error?: string,
): boolean {
  if (loading || error) return false;
  return artifact.size === 0 && !String(content ?? '').trim();
}

export type ArtifactDeliveryContext = 'assistant' | 'tool';

export function isUserFacingDeliveryArtifact(
  artifact: Partial<ChatArtifactPart>,
  context: ArtifactDeliveryContext = 'assistant',
): boolean {
  if (context === 'assistant') return true;
  const source = String(artifact.source || '').toLowerCase();
  return (
    source.includes('artifact_delivery')
    || source.includes('a2a_delivery_ref')
    || source.includes('terminal')
    || source.includes('final')
  );
}

export function artifactWorkspaceAgentId(
  artifact: Pick<ChatArtifactPart, 'downloadAgentId' | 'ownerAgentId' | 'sourceAgentId'>,
  fallbackAgentId?: string | null,
): string | null {
  return artifact.downloadAgentId || artifact.ownerAgentId || artifact.sourceAgentId || fallbackAgentId || null;
}

export async function loadOfficeArtifactPreview(
  artifact: ChatArtifactPart,
  artifactAgentId: string,
  authority?: ResourceAuthorityOptions,
): Promise<ArtifactPreviewState> {
  const blob = artifact.id
    ? await officeApi.getArtifactPreview(artifactAgentId, artifact.id, authority)
    : await officeApi.getWorkspacePreview(artifactAgentId, artifact.path, authority);
  return {
    artifact,
    url: URL.createObjectURL(blob),
    usingSnapshot: Boolean(artifact.id && artifact.snapshotHash),
    legacyCurrentFileFallback: Boolean(artifact.id && !artifact.snapshotHash),
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

export function shortArtifactAgentId(value: unknown): string {
  const text = stringValue(value).trim();
  if (!text) return '';
  return text.length > 12 ? text.slice(0, 8) : text;
}

export function artifactContributorLabel(artifact: ChatArtifactPart, fallbackAgent?: Record<string, unknown> | null): string {
  const explicitName = stringValue(
    artifact.sourceAgentName
    || artifact.ownerAgentName
    || artifact.downloadAgentName
    || artifact.deliveryAgentName,
  ).trim();
  if (explicitName) return explicitName;

  const contributorId = stringValue(
    artifact.sourceAgentId
    || artifact.ownerAgentId
    || artifact.downloadAgentId
    || artifact.deliveryAgentId,
  ).trim();
  if (!contributorId) return stringValue(fallbackAgent?.name).trim();
  if (contributorId === stringValue(fallbackAgent?.id).trim()) {
    return stringValue(fallbackAgent?.name).trim() || shortArtifactAgentId(contributorId);
  }
  return shortArtifactAgentId(contributorId);
}

export function ArtifactPreviewPanel({
  preview,
  onClose,
  onRetry,
  onDownload,
  t,
}: {
  preview: ArtifactPreviewState;
  onClose: () => void;
  onRetry?: () => void;
  onDownload?: () => void;
  t: Translate;
}) {
  const previewKind = getEffectiveArtifactPreviewKind(preview.artifact);
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
              {preview.usingSnapshot
                ? t('agent.chat.artifacts.snapshotPreview', 'Previewing saved session snapshot')
                : preview.legacyCurrentFileFallback
                  ? t('agent.chat.artifacts.legacyCurrentFilePreview', 'Previewing current workspace file; no delivery snapshot exists')
                : t('agent.chat.artifacts.preview', 'Preview')}
              {preview.workspaceChanged ? ` · ${t('agent.chat.artifacts.workspaceChanged', 'Workspace file changed after delivery')}` : ''}
            </div>
          </div>
          {preview.url && previewKind !== 'office' && (
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
          {onDownload && (
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
      </section>
    </div>
  );
}

export function ArtifactCards({
  agentId,
  artifacts,
  onOpenArtifact,
  context = 'assistant',
  operatorView = false,
  operatorReason,
}: {
  agentId?: string | null;
  artifacts?: ChatArtifactPart[];
  onOpenArtifact?: (artifact: ChatArtifactPart) => void;
  context?: ArtifactDeliveryContext;
  operatorView?: boolean;
  operatorReason?: string;
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
        const downloadArtifact = () => downloadAgentId
          ? downloadChatArtifact(artifact, downloadAgentId, authority, t)
          : Promise.resolve();
        return (
          <div
            key={`${artifact.id || artifact.path}`}
            className="chat-artifact-row"
          >
            <button
              type="button"
              data-testid="chat-artifact-row-open"
              aria-label={t('agent.chat.artifacts.openNamed', 'Open {{name}}', { name: artifact.name })}
              className="chat-artifact-main"
              onClick={openArtifact}
            >
              <IconFileText size={14} className="chat-artifact-icon" />
              <span className="chat-artifact-name">{artifact.name}</span>
              <span className="chat-artifact-meta">
                {[artifact.previewKind, size].filter(Boolean).join(' · ') || artifact.path}
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
            </span>
          </div>
        );
      })}
    </div>
  );
}
