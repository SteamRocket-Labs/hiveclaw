import type { ChatArtifactPart } from './chatRuntime';

function stringValue(value: unknown): string {
  return value == null ? '' : String(value);
}

export function formatArtifactSize(size: number | undefined): string | null {
  if (typeof size !== 'number' || !Number.isFinite(size) || size < 0) return null;
  if (size < 1024) return `${size} B`;
  if (size < 1024 * 1024) return `${Math.round(size / 1024)} KB`;
  return `${(size / (1024 * 1024)).toFixed(1)} MB`;
}

export type ArtifactOpenMode = 'inspector_preview' | 'download';

export function getEffectiveArtifactPreviewKind(artifact: Pick<ChatArtifactPart, 'name' | 'path' | 'previewKind'>): string {
  const suffix = (artifact.name || artifact.path).split('.').pop()?.toLowerCase() || '';
  if (['html', 'htm'].includes(suffix)) return 'html';
  const explicit = artifact.previewKind?.toLowerCase();
  if (explicit) return explicit;
  if (['md', 'markdown'].includes(suffix)) return 'markdown';
  if (['txt', 'csv', 'json', 'jsonl', 'log', 'xml', 'yaml', 'yml'].includes(suffix)) return 'text';
  if (['png', 'jpg', 'jpeg', 'gif', 'webp', 'svg'].includes(suffix)) return 'image';
  if (suffix === 'pdf') return 'pdf';
  if (['docx', 'xlsx', 'pptx'].includes(suffix)) return 'office';
  return 'download';
}

export function getArtifactOpenMode(artifact: Pick<ChatArtifactPart, 'name' | 'path' | 'previewKind'>): ArtifactOpenMode {
  const previewKind = getEffectiveArtifactPreviewKind(artifact);
  if (previewKind && ['markdown', 'text', 'html', 'image', 'pdf', 'office'].includes(previewKind)) {
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
