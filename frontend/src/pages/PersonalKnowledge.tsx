import { type FormEvent, type ReactNode, useEffect, useMemo, useRef, useState } from 'react';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { Link } from 'react-router-dom';
import { useTranslation } from 'react-i18next';
import type { TFunction } from 'i18next';
import {
  IconArchive,
  IconBrain,
  IconDatabase,
  IconFileText,
  IconRefresh,
  IconSearch,
  IconShieldCheck,
  IconSitemap,
  IconTrash,
  IconUpload,
  IconUser,
  IconWorld,
} from '@tabler/icons-react';
import {
  knowledgeApi,
  type PersonalKnowledgeDocumentDetail,
  type PersonalKnowledgeDocumentSummary,
  type PersonalKnowledgeGrantRequest,
  type PersonalKnowledgeGrantSummary,
  type PersonalKnowledgeGraphSummary,
  type PersonalKnowledgeJobSummary,
  type PersonalKnowledgeProposalSummary,
  type PersonalKnowledgeRevision,
  type PersonalKnowledgeSearchResult,
} from '../api/domains/knowledge';
import PersonalKnowledgeQueryState from '../components/PersonalKnowledgeQueryState';
import PersonalKnowledgePromotionCard from './personal-knowledge/PersonalKnowledgePromotionCard';
import './PersonalKnowledge.css';

type PersonalKnowledgeLane = 'inbox' | 'proposals' | 'library' | 'graph' | 'profile' | 'grants';

const laneIcons: Record<PersonalKnowledgeLane, ReactNode> = {
  inbox: <IconArchive size={15} stroke={1.7} />,
  proposals: <IconShieldCheck size={15} stroke={1.7} />,
  library: <IconFileText size={15} stroke={1.7} />,
  graph: <IconSitemap size={15} stroke={1.7} />,
  profile: <IconUser size={15} stroke={1.7} />,
  grants: <IconShieldCheck size={15} stroke={1.7} />,
};

// Advertised formats are exactly the ones proven by the RC-01 vertical
// evidence (real conversion → segments → search in real PostgreSQL):
// PDF, DOCX, Markdown, and plain text. Nothing else is advertised.
const importFormats = [
  { labelKey: 'personalKnowledge.formatPdf', fallback: 'PDF', helper: 'pdf' },
  { labelKey: 'personalKnowledge.formatDocx', fallback: 'Word / DOCX', helper: 'docx' },
  { labelKey: 'personalKnowledge.formatMarkdown', fallback: 'Markdown', helper: 'md · txt' },
] as const;

// Bounded machine-code vocabularies with exact static i18n keys; any unknown
// value renders one neutral localized label — raw codes never enter the DOM.
function sourceLabel(sourceKind: string, t: TFunction): string {
  switch (sourceKind) {
    case 'paste':
      return t('personalKnowledge.sourcePaste', 'Paste');
    case 'link':
    case 'url':
      return t('personalKnowledge.sourceLink', 'Link');
    case 'upload':
      return t('personalKnowledge.sourceUpload', 'Upload');
    case 'chat_attachment':
      return t('personalKnowledge.sourceChatAttachment', 'Chat attachment');
    case 'agent':
      return t('personalKnowledge.sourceAgent', 'From agent');
    default:
      return t('personalKnowledge.sourceOther', 'Other');
  }
}

function jobLifecycleLabel(lifecycleStatus: string, t: TFunction): string {
  switch (lifecycleStatus) {
    case 'queued':
      return t('personalKnowledge.jobStatus.queued', 'Queued');
    case 'running':
      return t('personalKnowledge.jobStatus.running', 'Running');
    case 'completed':
      return t('personalKnowledge.jobStatus.completed', 'Completed');
    case 'failed':
      return t('personalKnowledge.jobStatus.failed', 'Failed');
    case 'cancelled':
      return t('personalKnowledge.jobStatus.cancelled', 'Cancelled');
    default:
      return t('personalKnowledge.jobStatus.unknown', 'Status unavailable');
  }
}

function jobResultLabel(resultStatus: string | null, t: TFunction): string | null {
  switch (resultStatus) {
    case null:
    case '':
      return null;
    case 'ready':
      return t('personalKnowledge.jobResult.ready', 'Ready');
    case 'degraded':
      return t('personalKnowledge.jobResult.degraded', 'Partially indexed');
    case 'failed':
      return t('personalKnowledge.jobResult.failed', 'Failed');
    case 'cancelled':
      return t('personalKnowledge.jobResult.cancelled', 'Cancelled');
    default:
      return t('personalKnowledge.jobResult.unknown', 'Status unavailable');
  }
}

function jobErrorLabel(errorCode: string | null, t: TFunction): string | null {
  if (!errorCode) return null;
  switch (errorCode) {
    case 'conversion_failed':
      return t('personalKnowledge.jobError.conversionFailed', 'The file could not be converted.');
    case 'conversion_timeout':
      return t('personalKnowledge.jobError.conversionTimeout', 'Conversion timed out; you can retry.');
    case 'source_missing':
      return t('personalKnowledge.jobError.sourceMissing', 'The uploaded source is no longer available.');
    case 'unsupported_file_type':
      return t('personalKnowledge.jobError.unsupportedFileType', 'This file type is not supported.');
    case 'unsupported_or_unconfigured':
      return t('personalKnowledge.jobError.unsupportedOrUnconfigured', 'This media type is not supported here.');
    case 'media_transcription_empty':
      return t('personalKnowledge.jobError.mediaTranscriptionEmpty', 'No readable content was produced.');
    case 'document_missing':
      return t('personalKnowledge.jobError.documentMissing', 'The document is no longer available.');
    case 'canonical_markdown_missing':
      return t('personalKnowledge.jobError.canonicalMissing', 'The stored content is no longer available.');
    case 'import_payload_invalid':
      return t('personalKnowledge.jobError.importPayloadInvalid', 'The import request was invalid.');
    case 'worker_error':
    case 'import_failed':
      return t('personalKnowledge.jobError.importFailed', 'Import failed with an unspecified error.');
    case 'personal_kb_import_attempt_limit_exceeded':
      return t('personalKnowledge.jobError.attemptLimit', 'Retry limit reached.');
    default:
      return t('personalKnowledge.jobError.unknown', 'Import failed with an unspecified error.');
  }
}

function documentStatusLabel(status: string, t: TFunction): string {
  switch (status) {
    case 'queued':
      return t('personalKnowledge.documentStatus.queued', 'Queued');
    case 'ready':
      return t('personalKnowledge.documentStatus.ready', 'Ready');
    case 'degraded':
      return t('personalKnowledge.documentStatus.degraded', 'Partially indexed');
    case 'failed':
      return t('personalKnowledge.documentStatus.failed', 'Failed');
    case 'archived':
      return t('personalKnowledge.documentStatus.archived', 'Archived');
    default:
      return t('personalKnowledge.documentStatus.unknown', 'Status unavailable');
  }
}

// Exact conflict codes returned by cancel/retry/restore endpoints (409) and
// the bounded upload rejection (413); unknown failures use the generic code.
function actionErrorLabel(code: string | null, t: TFunction): string {
  switch (code) {
    case 'upload_too_large':
      return t('personalKnowledge.actionError.uploadTooLarge', 'The file is too large to import.');
    case 'not_retryable':
      return t('personalKnowledge.actionError.notRetryable', 'This import cannot be retried now.');
    case 'retry_attempt_limit':
      return t('personalKnowledge.actionError.retryAttemptLimit', 'Retry limit reached.');
    case 'not_cancellable_while_running':
      return t('personalKnowledge.actionError.notCancellableRunning', 'A running import cannot be cancelled.');
    case 'not_cancellable_terminal':
      return t('personalKnowledge.actionError.notCancellableTerminal', 'This import has already finished.');
    case 'restore_requires_archived':
      return t('personalKnowledge.actionError.restoreRequiresArchived', 'Only archived documents can be restored.');
    case 'restore_no_consumable_state':
      return t('personalKnowledge.actionError.restoreNoConsumableState', 'There is no consumable version to restore.');
    default:
      return t('personalKnowledge.actionError.unknown', 'The action could not be completed.');
  }
}

/** Extract the exact machine code from a typed API conflict; any other
 * failure (404/500/network) maps to the one bounded generic code so the
 * action error is always visible, never silently swallowed. */
export function actionErrorCode(error: unknown): string {
  const data = (error as { data?: unknown } | null)?.data;
  if (data && typeof data === 'object' && 'code' in data) {
    const code = (data as { code?: unknown }).code;
    if (typeof code === 'string' && code) return code;
  }
  return 'unknown';
}

function formatDate(value: string | null): string {
  if (!value) return '';
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return '';
  return new Intl.DateTimeFormat(undefined, { month: '2-digit', day: '2-digit' }).format(date);
}

function documentTags(document: PersonalKnowledgeDocumentSummary): string[] {
  const raw = document.metadata?.tags;
  return Array.isArray(raw) ? raw.map((tag) => String(tag)).filter(Boolean).slice(0, 4) : [];
}

function sourceImagePreview(document?: PersonalKnowledgeDocumentDetail): { filename: string; mimeType: string } | null {
  const metadata = document?.metadata ?? {};
  const mimeType = typeof metadata.source_mime_type === 'string' ? metadata.source_mime_type.trim().toLowerCase() : '';
  const mediaKind = typeof metadata.media_kind === 'string' ? metadata.media_kind.trim().toLowerCase() : '';
  if (!mimeType.startsWith('image/') && mediaKind !== 'image') return null;
  const filename = typeof metadata.source_filename === 'string' && metadata.source_filename.trim()
    ? metadata.source_filename.trim()
    : document?.title || 'source image';
  return { filename, mimeType: mimeType || 'image/*' };
}

function EmptyBlock({ children }: { children: string }) {
  return <div className="personal-kb-empty">{children}</div>;
}

function SearchResults({ results, query }: { results: PersonalKnowledgeSearchResult[]; query: string }) {
  const { t } = useTranslation();
  return (
    <section className="personal-kb-panel personal-kb-search-results">
      <div className="personal-kb-panel-heading">
        <h2>{t('personalKnowledge.searchResults')}</h2>
      </div>
      {results.length === 0 ? (
        // A completed zero-hit search is an explicit empty conclusion,
        // never the unavailable/error surface and never silent nothing.
        <EmptyBlock>{t('personalKnowledge.searchEmpty', { query })}</EmptyBlock>
      ) : (
        results.map((result) => (
          <div key={result.segment_id} className="personal-kb-result">
            <strong>{result.title}</strong>
            <span>{result.heading_path.join(' / ')}</span>
            <p>{result.snippet}</p>
            <code>{result.source_ref}</code>
          </div>
        ))
      )}
    </section>
  );
}

export function ImportJobs({
  jobs,
  onRetry,
  onCancel,
  busyJobId,
  actionError,
}: {
  jobs: PersonalKnowledgeJobSummary[];
  onRetry: (jobId: string) => void;
  onCancel: (jobId: string) => void;
  busyJobId?: string | null;
  actionError?: string | null;
}) {
  const { t } = useTranslation();
  return (
    <div className="personal-kb-jobs">
      {actionError && (
        <div className="personal-kb-action-error" role="alert">
          {actionErrorLabel(actionError, t)}
        </div>
      )}
      {jobs.length === 0 ? (
        <EmptyBlock>{t('personalKnowledge.noJobs')}</EmptyBlock>
      ) : (
        jobs.map((job) => {
          const filename = String(job.metadata?.source_filename || t('personalKnowledge.untitledJob', 'Knowledge import'));
          const errorLabel = jobErrorLabel(job.error_code, t);
          // The result label is shown only when it adds information beyond
          // the lifecycle (completed → ready/degraded); failed/cancelled
          // would only duplicate the lifecycle label.
          const resultLabel = job.lifecycle_status === 'completed' ? jobResultLabel(job.result_status, t) : null;
          return (
            <div key={job.job_id} className="personal-kb-job-row">
              <div>
                <strong>{filename}</strong>
                <span>
                  {jobLifecycleLabel(job.lifecycle_status, t)}
                  {resultLabel ? ` · ${resultLabel}` : ''} · {t('personalKnowledge.attempts')} {job.attempt_count}/{job.max_attempts}
                </span>
                {errorLabel && <small>{errorLabel}</small>}
              </div>
              <span className="personal-kb-job-actions">
                {job.cancellable && (
                  <button
                    type="button"
                    className="btn btn-secondary btn-sm"
                    disabled={busyJobId === job.job_id}
                    onClick={() => onCancel(job.job_id)}
                  >
                    {t('personalKnowledge.cancel', 'Cancel')}
                  </button>
                )}
                {job.retryable && (
                  <button
                    type="button"
                    className="btn btn-secondary btn-sm"
                    disabled={busyJobId === job.job_id}
                    onClick={() => onRetry(job.job_id)}
                  >
                    <IconRefresh size={14} stroke={1.7} />
                    {t('personalKnowledge.retry')}
                  </button>
                )}
              </span>
            </div>
          );
        })
      )}
    </div>
  );
}

export function InboxPanel({
  title,
  markdown,
  url,
  selectedFile,
  jobs,
  jobsLoading,
  jobsError,
  busyJobId,
  actionError,
  intakeError,
  onTitleChange,
  onMarkdownChange,
  onUrlChange,
  onFileChange,
  onPasteSubmit,
  onFileSubmit,
  onUrlSubmit,
  onRetryJob,
  onCancelJob,
  onRetryJobsQuery,
  pastePending,
  filePending,
  urlPending,
}: {
  title: string;
  markdown: string;
  url: string;
  selectedFile: File | null;
  jobs: PersonalKnowledgeJobSummary[];
  jobsLoading: boolean;
  jobsError?: unknown;
  busyJobId?: string | null;
  actionError?: string | null;
  intakeError?: string | null;
  onTitleChange: (value: string) => void;
  onMarkdownChange: (value: string) => void;
  onUrlChange: (value: string) => void;
  onFileChange: (file: File | null) => void;
  onPasteSubmit: (event: FormEvent) => void;
  onFileSubmit: (event: FormEvent) => void;
  onUrlSubmit: (event: FormEvent) => void;
  onRetryJob: (jobId: string) => void;
  onCancelJob: (jobId: string) => void;
  onRetryJobsQuery: () => void;
  pastePending: boolean;
  filePending: boolean;
  urlPending: boolean;
}) {
  const { t } = useTranslation();
  const formatLabels: Record<(typeof importFormats)[number]['labelKey'], string> = {
    'personalKnowledge.formatPdf': t('personalKnowledge.formatPdf', 'PDF'),
    'personalKnowledge.formatDocx': t('personalKnowledge.formatDocx', 'Word / DOCX'),
    'personalKnowledge.formatMarkdown': t('personalKnowledge.formatMarkdown', 'Markdown'),
  };
  return (
    <section className="personal-kb-panel personal-kb-intake">
      <div className="personal-kb-panel-heading">
        <div>
          <h2>{t('personalKnowledge.inboxTitle')}</h2>
          <p>{t('personalKnowledge.inboxDesc')}</p>
        </div>
        <IconDatabase size={18} stroke={1.7} />
      </div>

      {intakeError && (
        <div className="personal-kb-action-error" role="alert">
          {actionErrorLabel(intakeError, t)}
        </div>
      )}

      <form className="personal-kb-upload-zone" onSubmit={onFileSubmit}>
        <label
          className="personal-kb-drop-target"
          onDrop={(event) => {
            event.preventDefault();
            onFileChange(event.dataTransfer.files?.[0] ?? null);
          }}
          onDragOver={(event) => event.preventDefault()}
        >
          <IconUpload size={18} stroke={1.7} />
          <strong>{t('personalKnowledge.dropOrChoose')}</strong>
          <span>{selectedFile ? selectedFile.name : t('personalKnowledge.dropHelper')}</span>
          <input
            type="file"
            onChange={(event) => onFileChange(event.target.files?.[0] ?? null)}
          />
        </label>
        <div className="personal-kb-format-grid">
          {importFormats.map((format) => (
            <span key={format.labelKey}>
              <strong>{formatLabels[format.labelKey]}</strong>
              <small>{format.helper}</small>
            </span>
          ))}
        </div>
        <button type="submit" className="btn btn-primary" disabled={!selectedFile || filePending}>
          {filePending ? t('personalKnowledge.importing') : t('personalKnowledge.importFile')}
        </button>
      </form>

      <form className="personal-kb-url-form" onSubmit={onUrlSubmit}>
        <div>
          <strong>{t('personalKnowledge.urlImport')}</strong>
          <span>{t('personalKnowledge.urlImportDesc')}</span>
        </div>
        <input
          value={url}
          onChange={(event) => onUrlChange(event.target.value)}
          placeholder="https://example.com/research"
        />
        <button type="submit" className="btn btn-secondary" disabled={!url.trim() || urlPending}>
          <IconWorld size={14} stroke={1.7} />
          {t('personalKnowledge.importUrl')}
        </button>
      </form>

      <form id="personal-kb-intake-form" onSubmit={onPasteSubmit} className="personal-kb-intake-form">
        <input
          value={title}
          onChange={(event) => onTitleChange(event.target.value)}
          placeholder={t('personalKnowledge.titlePlaceholder')}
        />
        <textarea
          value={markdown}
          onChange={(event) => onMarkdownChange(event.target.value)}
          placeholder={t('personalKnowledge.markdownPlaceholder')}
        />
        <div className="personal-kb-panel-actions">
          <button type="submit" className="btn btn-primary" disabled={!markdown.trim() || pastePending}>
            {pastePending ? t('common.saving', 'Saving...') : t('personalKnowledge.feed')}
          </button>
        </div>
      </form>

      <div className="personal-kb-subsection">
        <h3>{t('personalKnowledge.importJobs')}</h3>
        {jobsError ? (
          <PersonalKnowledgeQueryState error={jobsError} onRetry={onRetryJobsQuery} />
        ) : jobsLoading ? (
          <EmptyBlock>{t('common.loading', 'Loading...')}</EmptyBlock>
        ) : (
          <ImportJobs jobs={jobs} onRetry={onRetryJob} onCancel={onCancelJob} busyJobId={busyJobId} actionError={actionError} />
        )}
      </div>
    </section>
  );
}

function LibraryPanel({
  documents,
  activeDocumentId,
  isLoading,
  onSelect,
}: {
  documents: PersonalKnowledgeDocumentSummary[];
  activeDocumentId: string | null;
  isLoading: boolean;
  onSelect: (documentId: string) => void;
}) {
  const { t } = useTranslation();
  return (
    <section className="personal-kb-panel">
      <div className="personal-kb-panel-heading">
        <div>
          <h2>{t('personalKnowledge.libraryTitle')}</h2>
          <p>{t('personalKnowledge.libraryDesc')}</p>
        </div>
        <IconFileText size={18} stroke={1.7} />
      </div>
      {isLoading && <EmptyBlock>{t('common.loading', 'Loading...')}</EmptyBlock>}
      {!isLoading && documents.length === 0 && (
        <EmptyBlock>{t('personalKnowledge.empty')}</EmptyBlock>
      )}
      <div className="personal-kb-document-list">
        {documents.map((document) => (
          <button
            key={document.document_id}
            type="button"
            className={`personal-kb-doc ${activeDocumentId === document.document_id ? 'active' : ''}`}
            onClick={() => onSelect(document.document_id)}
          >
            <span className="personal-kb-doc-head">
              <strong>{document.title}</strong>
              <small>{sourceLabel(document.source_kind, t)} · {documentStatusLabel(document.status, t)}</small>
            </span>
            <span className="personal-kb-doc-tags">
              {documentTags(document).map((tag) => <em key={tag}>{tag}</em>)}
            </span>
            <span className="personal-kb-doc-meta">
              {formatDate(document.created_at)}
              {formatDate(document.created_at) ? ' · ' : ''}
              {document.segment_count} {t('personalKnowledge.segmentUnit')} · {document.sensitivity}
            </span>
            <code>{document.source_ref}</code>
          </button>
        ))}
      </div>
    </section>
  );
}

function GraphPanel({ graph }: { graph?: PersonalKnowledgeGraphSummary }) {
  const { t } = useTranslation();
  const entities = graph?.entities ?? [];
  const links = graph?.links ?? [];
  const assertions = graph?.assertions ?? [];
  return (
    <section className="personal-kb-panel">
      <div className="personal-kb-panel-heading">
        <div>
          <h2>{t('personalKnowledge.graph')}</h2>
          <p>{t('personalKnowledge.graphDesc')}</p>
        </div>
        <IconSitemap size={18} stroke={1.7} />
      </div>
      <div className="personal-kb-mini-grid">
        <span>{entities.length} entities</span>
        <span>{links.length} links</span>
        <span>{assertions.length} assertions</span>
      </div>
      {entities.length === 0 ? (
        <EmptyBlock>{t('personalKnowledge.graphEmpty')}</EmptyBlock>
      ) : (
        <div className="personal-kb-graph-list">
          {entities.map((entity) => (
            <div key={entity.entity_id} className="personal-kb-graph-entity">
              <strong>{entity.canonical_name}</strong>
              <span>{entity.entity_type} · confidence {entity.confidence.toFixed(2)}</span>
              {entity.description && <p>{entity.description}</p>}
              {entity.aliases.length > 0 && <code>{entity.aliases.join(', ')}</code>}
            </div>
          ))}
        </div>
      )}
    </section>
  );
}

type PersonalKnowledgeGrantPurpose = 'interactive_session' | 'autonomous_agent' | 'a2a_delegation' | 'subagent_delegation';
type PersonalKnowledgeSensitivityCeiling = 'PL1_public' | 'PL2_pii' | 'PL3_sensitive' | 'PL4_credential';

function defaultGrantExpiryLocal(): string {
  const expires = new Date(Date.now() + 60 * 60 * 1000);
  const local = new Date(expires.getTime() - expires.getTimezoneOffset() * 60_000);
  return local.toISOString().slice(0, 16);
}

export function GrantsPanel({
  grants,
  granteeType,
  granteeId,
  permission,
  requesterUserId,
  sessionId,
  purpose,
  delegationId,
  sensitivityCeiling,
  expiresAt,
  onGranteeTypeChange,
  onGranteeIdChange,
  onPermissionChange,
  onRequesterUserIdChange,
  onSessionIdChange,
  onPurposeChange,
  onDelegationIdChange,
  onSensitivityCeilingChange,
  onExpiresAtChange,
  onCreate,
  onDelete,
  createPending,
  deletingGrantId,
}: {
  grants: PersonalKnowledgeGrantSummary[];
  granteeType: 'user' | 'agent';
  granteeId: string;
  permission: 'read' | 'search' | 'manage';
  requesterUserId: string;
  sessionId: string;
  purpose: PersonalKnowledgeGrantPurpose;
  delegationId: string;
  sensitivityCeiling: PersonalKnowledgeSensitivityCeiling;
  expiresAt: string;
  onGranteeTypeChange: (value: 'user' | 'agent') => void;
  onGranteeIdChange: (value: string) => void;
  onPermissionChange: (value: 'read' | 'search' | 'manage') => void;
  onRequesterUserIdChange: (value: string) => void;
  onSessionIdChange: (value: string) => void;
  onPurposeChange: (value: PersonalKnowledgeGrantPurpose) => void;
  onDelegationIdChange: (value: string) => void;
  onSensitivityCeilingChange: (value: PersonalKnowledgeSensitivityCeiling) => void;
  onExpiresAtChange: (value: string) => void;
  onCreate: (event: FormEvent) => void;
  onDelete: (grantId: string) => void;
  createPending: boolean;
  deletingGrantId?: string | null;
}) {
  const { t } = useTranslation();
  const delegatedPurpose = purpose === 'a2a_delegation' || purpose === 'subagent_delegation';
  const requiresSession = granteeType === 'agent' && purpose !== 'autonomous_agent';
  const createDisabled = !granteeId.trim()
    || createPending
    || (granteeType === 'agent' && !expiresAt)
    || (requiresSession && (!requesterUserId.trim() || !sessionId.trim()))
    || (delegatedPurpose && !delegationId.trim());
  return (
    <section className="personal-kb-panel">
      <div className="personal-kb-panel-heading">
        <div>
          <h2>{t('personalKnowledge.grants')}</h2>
          <p>{t('personalKnowledge.grantsDesc')}</p>
        </div>
        <IconShieldCheck size={18} stroke={1.7} />
      </div>
      <form className="personal-kb-grant-form" onSubmit={onCreate}>
        <select value={granteeType} onChange={(event) => onGranteeTypeChange(event.target.value as 'user' | 'agent')}>
          <option value="agent">agent</option>
          <option value="user">user</option>
        </select>
        <input value={granteeId} onChange={(event) => onGranteeIdChange(event.target.value)} placeholder="grantee UUID" />
        <select value={permission} onChange={(event) => onPermissionChange(event.target.value as 'read' | 'search' | 'manage')}>
          <option value="search">search</option>
          <option value="read">read</option>
          <option value="manage">manage</option>
        </select>
        <select
          value={sensitivityCeiling}
          aria-label={t('personalKnowledge.sensitivityCeiling')}
          onChange={(event) => onSensitivityCeilingChange(event.target.value as PersonalKnowledgeSensitivityCeiling)}
        >
          <option value="PL1_public">PL1_public</option>
          <option value="PL2_pii">PL2_pii</option>
          <option value="PL3_sensitive">PL3_sensitive</option>
          <option value="PL4_credential">PL4_credential · reference only</option>
        </select>
        {granteeType === 'agent' && (
          <>
            <select
              value={purpose}
              aria-label={t('personalKnowledge.grantPurpose')}
              onChange={(event) => onPurposeChange(event.target.value as PersonalKnowledgeGrantPurpose)}
            >
              <option value="autonomous_agent">autonomous_agent</option>
              <option value="interactive_session">interactive_session</option>
              <option value="a2a_delegation">a2a_delegation</option>
              <option value="subagent_delegation">subagent_delegation</option>
            </select>
            {requiresSession && (
              <>
                <input
                  value={requesterUserId}
                  onChange={(event) => onRequesterUserIdChange(event.target.value)}
                  placeholder="requester user UUID"
                />
                <input
                  value={sessionId}
                  onChange={(event) => onSessionIdChange(event.target.value)}
                  placeholder="bound session ID"
                />
              </>
            )}
            {delegatedPurpose && (
              <input
                value={delegationId}
                onChange={(event) => onDelegationIdChange(event.target.value)}
                placeholder="delegation ID"
              />
            )}
            <label>
              <span>{t('personalKnowledge.grantExpiresAt')}</span>
              <input
                type="datetime-local"
                value={expiresAt}
                onChange={(event) => onExpiresAtChange(event.target.value)}
                required
              />
            </label>
          </>
        )}
        <button type="submit" className="btn btn-primary" disabled={createDisabled}>
          {t('personalKnowledge.createGrant')}
        </button>
      </form>
      <div className="personal-kb-grant-list">
        {grants.map((grant) => (
          <div key={grant.grant_id} className="personal-kb-grant-row">
            <div>
              <strong>{grant.grantee_type}:{grant.grantee_id}</strong>
              <span>
                {grant.permission} · {grant.resource_type} · {grant.sensitivity_ceiling}
                {grant.purpose ? ` · ${grant.purpose}` : ''}
                {grant.active ? '' : ` · ${t('personalKnowledge.revokedOrExpired')}`}
              </span>
              {grant.requester_user_id && <code>requester:{grant.requester_user_id}</code>}
              {grant.session_id && <code>session:{grant.session_id}</code>}
              {grant.delegation_id && <code>delegation:{grant.delegation_id}</code>}
              {grant.expires_at && <code>{grant.expires_at}</code>}
            </div>
            <button
              type="button"
              className="btn btn-secondary btn-sm"
              disabled={deletingGrantId === grant.grant_id}
              onClick={() => onDelete(grant.grant_id)}
            >
              <IconTrash size={14} stroke={1.7} />
              {t('personalKnowledge.revoke')}
            </button>
          </div>
        ))}
        {grants.length === 0 && <EmptyBlock>{t('personalKnowledge.noGrants')}</EmptyBlock>}
      </div>
    </section>
  );
}

export function ProposalReviewPanel({
  proposals,
  busyProposalId,
  onDecision,
}: {
  proposals: PersonalKnowledgeProposalSummary[];
  busyProposalId: string | null;
  onDecision: (proposalId: string, decision: 'approve' | 'reject') => void;
}) {
  const { t } = useTranslation();
  return (
    <section className="personal-kb-panel">
      <div className="personal-kb-panel-heading">
        <div>
          <h2>{t('personalKnowledge.proposals')}</h2>
          <p>{t('personalKnowledge.proposalsDesc')}</p>
        </div>
        <IconShieldCheck size={18} stroke={1.7} />
      </div>
      <div className="personal-kb-proposal-list">
        {proposals.map((proposal) => (
          <article key={proposal.proposal_id} className="personal-kb-proposal">
            <div className="personal-kb-proposal-head">
              <div>
                <strong>{proposal.title}</strong>
                <span>{proposal.target_collection} · {proposal.sensitivity} · {proposal.status}</span>
              </div>
              <span className="ui-chip">{proposal.policy_outcome}</span>
            </div>
            <p>{proposal.purpose}</p>
            <div className="personal-kb-preview-title">{t('personalKnowledge.proposalDiff')}</div>
            <pre className="personal-kb-diff" aria-label={t('personalKnowledge.proposalDiff')}>
              {proposal.diff_unified || proposal.content}
            </pre>
            <div className="personal-kb-proposal-evidence">
              <small>Agent · {proposal.proposed_by_agent_id}</small>
              <small>SHA-256 · {proposal.content_hash}</small>
              {proposal.baseline_revision_id && <small>baseline · {proposal.baseline_revision_id}</small>}
              {proposal.source_refs.map((ref) => <code key={ref}>{ref}</code>)}
            </div>
            {proposal.policy_reason_codes.length > 0 && (
              <small>{proposal.policy_reason_codes.join(' · ')}</small>
            )}
            {proposal.status === 'pending' && (
              <div className="personal-kb-detail-actions">
                <button
                  type="button"
                  className="btn btn-primary btn-sm"
                  disabled={busyProposalId === proposal.proposal_id}
                  onClick={() => onDecision(proposal.proposal_id, 'approve')}
                >
                  {t('personalKnowledge.approveProposal')}
                </button>
                <button
                  type="button"
                  className="btn btn-secondary btn-sm"
                  disabled={busyProposalId === proposal.proposal_id}
                  onClick={() => onDecision(proposal.proposal_id, 'reject')}
                >
                  {t('personalKnowledge.rejectProposal')}
                </button>
              </div>
            )}
          </article>
        ))}
        {proposals.length === 0 && (
          <EmptyBlock>{t('personalKnowledge.noProposals')}</EmptyBlock>
        )}
      </div>
    </section>
  );
}

export function RevisionHistory({
  revisions,
  busyVersion,
  onRollback,
}: {
  revisions: PersonalKnowledgeRevision[];
  busyVersion: number | null;
  onRollback: (version: number) => void;
}) {
  const { t } = useTranslation();
  return (
    <div className="personal-kb-revisions">
      <div className="personal-kb-preview-title">{t('personalKnowledge.revisionHistory')}</div>
      {revisions.map((revision, index) => (
        <div key={revision.id} className="personal-kb-revision-row">
          <div>
            <strong>{t('personalKnowledge.version')} {revision.version}</strong>
            <span>{revision.change_source}</span>
            {revision.change_message && <small>{revision.change_message}</small>}
          </div>
          {index > 0 && (
            <button
              type="button"
              className="btn btn-secondary btn-sm"
              disabled={busyVersion === revision.version}
              onClick={() => onRollback(revision.version)}
            >
              {t('personalKnowledge.rollbackVersion')}
            </button>
          )}
        </div>
      ))}
      {revisions.length === 0 && <small>{t('personalKnowledge.noRevisions')}</small>}
    </div>
  );
}

export function DocumentDetail({
  document,
  onRebuild,
  onToggleAgentSearchable,
  onArchive,
  onRestore,
  rebuildPending,
  patchPending,
  restorePending,
  actionError,
  revisions,
  revisionsLoading,
  revisionsError,
  rollbackPendingVersion,
  onRollback,
  onRetryRevisions,
}: {
  document?: PersonalKnowledgeDocumentDetail;
  onRebuild: (documentId: string) => void;
  onToggleAgentSearchable: (document: PersonalKnowledgeDocumentDetail) => void;
  onArchive: (documentId: string) => void;
  onRestore: (documentId: string) => void;
  rebuildPending: boolean;
  patchPending: boolean;
  restorePending: boolean;
  actionError?: string | null;
  revisions: PersonalKnowledgeRevision[];
  revisionsLoading: boolean;
  revisionsError?: unknown;
  rollbackPendingVersion: number | null;
  onRollback: (version: number) => void;
  onRetryRevisions: () => void;
}) {
  const { t } = useTranslation();
  const imagePreview = sourceImagePreview(document);
  const imagePreviewQuery = useQuery({
    queryKey: ['personal-knowledge-source-preview', document?.document_id],
    queryFn: () => knowledgeApi.myPersonalDocumentSourcePreview(document?.document_id ?? ''),
    enabled: !!document && !!imagePreview,
    staleTime: 5 * 60 * 1000,
    retry: 1,
  });
  const [imagePreviewUrl, setImagePreviewUrl] = useState<string | null>(null);

  useEffect(() => {
    if (!imagePreviewQuery.data || typeof URL === 'undefined') {
      setImagePreviewUrl(null);
      return undefined;
    }
    const objectUrl = URL.createObjectURL(imagePreviewQuery.data);
    setImagePreviewUrl(objectUrl);
    return () => URL.revokeObjectURL(objectUrl);
  }, [imagePreviewQuery.data]);

  if (!document) {
    return (
      <aside className="personal-kb-detail">
        <EmptyBlock>{t('personalKnowledge.selectDocument')}</EmptyBlock>
      </aside>
    );
  }

  return (
    <aside className="personal-kb-detail">
      <div className="personal-kb-detail-head">
        <div>
          <span className="personal-kb-eyebrow">{t('personalKnowledge.detailEyebrow')}</span>
          <h2>{document.title}</h2>
        </div>
        <span className="ui-chip">{documentStatusLabel(document.status, t)}</span>
      </div>
      {imagePreview && (
        <div className="personal-kb-source-preview">
          <div className="personal-kb-preview-title">{t('personalKnowledge.sourceImagePreview')}</div>
          {imagePreviewUrl ? (
            <img src={imagePreviewUrl} alt={imagePreview.filename} />
          ) : imagePreviewQuery.isError ? (
            <PersonalKnowledgeQueryState
              error={imagePreviewQuery.error}
              onRetry={() => void imagePreviewQuery.refetch()}
            />
          ) : (
            <div className="personal-kb-source-preview-placeholder">
              {t('personalKnowledge.sourceImagePreviewLoading')}
            </div>
          )}
          <small>{imagePreview.filename}</small>
        </div>
      )}
      <div className="personal-kb-preview">
        <div className="personal-kb-preview-title">{t('personalKnowledge.mdPreview')}</div>
        {document.segments.slice(0, 4).map((segment) => (
          <div key={segment.segment_id} className="personal-kb-segment">
            <span>
              #{segment.position + 1} {segment.heading_path.join(' / ')} · {segment.token_count} tok
            </span>
            <p>{segment.content}</p>
          </div>
        ))}
      </div>
      <div className="personal-kb-evidence">
        <h3>{t('personalKnowledge.evidenceChain')}</h3>
        <code>{document.source_ref}</code>
        <small>{document.canonical_md_path}</small>
      </div>
      {revisionsError ? (
        <PersonalKnowledgeQueryState error={revisionsError} onRetry={onRetryRevisions} />
      ) : revisionsLoading ? (
        <EmptyBlock>{t('common.loading', 'Loading...')}</EmptyBlock>
      ) : (
        <RevisionHistory
          revisions={revisions}
          busyVersion={rollbackPendingVersion}
          onRollback={onRollback}
        />
      )}
      <div className="personal-kb-detail-actions">
        {actionError && (
          <div className="personal-kb-action-error" role="alert">
            {actionErrorLabel(actionError, t)}
          </div>
        )}
        <button
          type="button"
          className="btn btn-secondary btn-sm"
          disabled={rebuildPending}
          onClick={() => onRebuild(document.document_id)}
        >
          <IconRefresh size={14} stroke={1.7} />
          {t('personalKnowledge.rebuildIndex')}
        </button>
        <button
          type="button"
          className="btn btn-secondary btn-sm"
          disabled={patchPending}
          onClick={() => onToggleAgentSearchable(document)}
        >
          {document.agent_searchable
            ? t('personalKnowledge.blockAgentSearch')
            : t('personalKnowledge.allowAgentSearch')}
        </button>
        {document.status === 'archived' ? (
          <button
            type="button"
            className="btn btn-secondary btn-sm"
            disabled={restorePending}
            onClick={() => onRestore(document.document_id)}
          >
            {t('personalKnowledge.restore', 'Restore')}
          </button>
        ) : (
          <button
            type="button"
            className="btn btn-secondary btn-sm"
            disabled={patchPending}
            onClick={() => onArchive(document.document_id)}
          >
            {t('personalKnowledge.archive')}
          </button>
        )}
        {document.status === 'ready' && (
          <PersonalKnowledgePromotionCard
            documentKey={document.document_id}
            documentTitle={document.title}
          />
        )}
      </div>
    </aside>
  );
}

export default function PersonalKnowledge() {
  const { t } = useTranslation();
  const queryClient = useQueryClient();
  const [activeLane, setActiveLane] = useState<PersonalKnowledgeLane>('inbox');
  const [selectedDocumentId, setSelectedDocumentId] = useState<string | null>(null);
  const [searchInput, setSearchInput] = useState('');
  const [activeSearch, setActiveSearch] = useState('');
  const [title, setTitle] = useState('');
  const [markdown, setMarkdown] = useState('');
  const [url, setUrl] = useState('');
  const [selectedFile, setSelectedFile] = useState<File | null>(null);
  const [busyJobId, setBusyJobId] = useState<string | null>(null);
  const [granteeType, setGranteeType] = useState<'user' | 'agent'>('agent');
  const [granteeId, setGranteeId] = useState('');
  const [grantPermission, setGrantPermission] = useState<'read' | 'search' | 'manage'>('search');
  const [grantRequesterUserId, setGrantRequesterUserId] = useState('');
  const [grantSessionId, setGrantSessionId] = useState('');
  const [grantPurpose, setGrantPurpose] = useState<PersonalKnowledgeGrantPurpose>('autonomous_agent');
  const [grantDelegationId, setGrantDelegationId] = useState('');
  const [grantSensitivityCeiling, setGrantSensitivityCeiling] = useState<PersonalKnowledgeSensitivityCeiling>('PL3_sensitive');
  const [grantExpiresAt, setGrantExpiresAt] = useState(defaultGrantExpiryLocal);
  const [deletingGrantId, setDeletingGrantId] = useState<string | null>(null);
  const [busyProposalId, setBusyProposalId] = useState<string | null>(null);
  const [rollbackPendingVersion, setRollbackPendingVersion] = useState<number | null>(null);
  const [jobActionError, setJobActionError] = useState<string | null>(null);
  const [documentActionError, setDocumentActionError] = useState<string | null>(null);
  const [intakeActionError, setIntakeActionError] = useState<string | null>(null);

  const documentsQuery = useQuery({
    queryKey: ['personal-knowledge-documents'],
    queryFn: () => knowledgeApi.myPersonalDocuments(),
  });
  const jobsQuery = useQuery({
    queryKey: ['personal-knowledge-import-jobs'],
    queryFn: () => knowledgeApi.myPersonalImportJobs(),
    enabled: activeLane === 'inbox',
    // Lifecycle-aware polling: refresh only while any job is queued/running;
    // stop once everything is terminal.
    refetchInterval: (query) => {
      const jobs = (query.state.data as { jobs?: PersonalKnowledgeJobSummary[] } | undefined)?.jobs ?? [];
      return jobs.some((job) => job.lifecycle_status === 'queued' || job.lifecycle_status === 'running')
        ? 3000
        : false;
    },
  });
  const jobs = jobsQuery.data?.jobs ?? [];
  const graphQuery = useQuery({
    queryKey: ['personal-knowledge-graph'],
    queryFn: () => knowledgeApi.myPersonalGraph(),
    enabled: activeLane === 'graph',
  });
  const grantsQuery = useQuery({
    queryKey: ['personal-knowledge-grants'],
    queryFn: () => knowledgeApi.myPersonalGrants(),
    enabled: activeLane === 'grants',
  });
  const proposalsQuery = useQuery({
    queryKey: ['personal-knowledge-proposals'],
    queryFn: () => knowledgeApi.myPersonalProposals(),
    enabled: activeLane === 'proposals',
  });
  const documents = documentsQuery.data?.documents ?? [];
  const activeDocumentId = selectedDocumentId || documents[0]?.document_id || null;
  const detailQuery = useQuery({
    queryKey: ['personal-knowledge-document', activeDocumentId],
    queryFn: () => knowledgeApi.myPersonalDocument(activeDocumentId as string),
    enabled: !!activeDocumentId,
  });
  const searchQuery = useQuery({
    queryKey: ['personal-knowledge-search', activeSearch],
    queryFn: () => knowledgeApi.myPersonalSearch(activeSearch, 8),
    enabled: activeSearch.trim().length > 0,
  });
  const revisionsQuery = useQuery({
    queryKey: ['personal-knowledge-revisions', activeDocumentId],
    queryFn: () => knowledgeApi.myPersonalDocumentRevisions(activeDocumentId as string),
    enabled: !!activeDocumentId,
  });

  const invalidatePersonalKb = () => {
    void queryClient.invalidateQueries({ queryKey: ['personal-knowledge-documents'] });
    void queryClient.invalidateQueries({ queryKey: ['personal-knowledge-import-jobs'] });
    void queryClient.invalidateQueries({ queryKey: ['personal-knowledge-graph'] });
    void queryClient.invalidateQueries({ queryKey: ['personal-knowledge-proposals'] });
    void queryClient.invalidateQueries({ queryKey: ['personal-knowledge-search'] });
    if (activeDocumentId) void queryClient.invalidateQueries({ queryKey: ['personal-knowledge-document', activeDocumentId] });
    if (activeDocumentId) void queryClient.invalidateQueries({ queryKey: ['personal-knowledge-revisions', activeDocumentId] });
  };

  // When a job transitions to a terminal lifecycle state, the documents/
  // detail/graph/revisions/search read models refresh without a reload.
  const terminalSignature = jobs
    .filter((job) => job.lifecycle_status !== 'queued' && job.lifecycle_status !== 'running')
    .map((job) => `${job.job_id}:${job.lifecycle_status}:${job.result_status ?? ''}`)
    .sort()
    .join('|');
  const previousTerminalSignatureRef = useRef<string | null>(null);
  useEffect(() => {
    const previous = previousTerminalSignatureRef.current;
    previousTerminalSignatureRef.current = terminalSignature;
    if (previous === null || previous === terminalSignature) return;
    invalidatePersonalKb();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [terminalSignature]);

  const ingestMutation = useMutation({
    mutationFn: () =>
      knowledgeApi.myPersonalIngest({
        title: title.trim() || t('personalKnowledge.untitled'),
        markdown,
        source_kind: 'paste',
        source_uri: 'browser://knowledge/personal',
        agent_searchable: true,
        sensitivity: 'internal',
      }),
    onMutate: () => setIntakeActionError(null),
    onSuccess: (result) => {
      setTitle('');
      setMarkdown('');
      setIntakeActionError(null);
      setSelectedDocumentId(result.document_id);
      invalidatePersonalKb();
    },
    onError: (error) => setIntakeActionError(actionErrorCode(error)),
  });
  const importFileMutation = useMutation({
    mutationFn: (file: File) => knowledgeApi.myPersonalImportFile(file, { title: title.trim() || file.name, sensitivity: 'internal' }),
    onMutate: () => setIntakeActionError(null),
    onSuccess: (result) => {
      setSelectedFile(null);
      setIntakeActionError(null);
      setSelectedDocumentId(result.document_id);
      invalidatePersonalKb();
    },
    onError: (error) => setIntakeActionError(actionErrorCode(error)),
  });
  const importUrlMutation = useMutation({
    mutationFn: () => knowledgeApi.myPersonalImportUrl({ url: url.trim(), title: title.trim() || undefined, sensitivity: 'internal' }),
    onMutate: () => setIntakeActionError(null),
    onSuccess: (result) => {
      setUrl('');
      setIntakeActionError(null);
      setSelectedDocumentId(result.document_id);
      invalidatePersonalKb();
    },
    onError: (error) => setIntakeActionError(actionErrorCode(error)),
  });
  const retryMutation = useMutation({
    mutationFn: (jobId: string) => knowledgeApi.myPersonalRetryImportJob(jobId),
    onMutate: () => setJobActionError(null),
    onSuccess: (result) => {
      setBusyJobId(null);
      setJobActionError(null);
      setSelectedDocumentId(result.document_id);
      invalidatePersonalKb();
    },
    onError: (error) => {
      setBusyJobId(null);
      setJobActionError(actionErrorCode(error));
    },
  });
  const cancelJobMutation = useMutation({
    mutationFn: (jobId: string) => knowledgeApi.myPersonalCancelImportJob(jobId),
    onMutate: () => setJobActionError(null),
    onSuccess: () => {
      setBusyJobId(null);
      setJobActionError(null);
      invalidatePersonalKb();
    },
    onError: (error) => {
      setBusyJobId(null);
      setJobActionError(actionErrorCode(error));
    },
  });
  const patchMutation = useMutation({
    mutationFn: ({ documentId, body }: { documentId: string; body: { agent_searchable?: boolean; status?: string } }) =>
      knowledgeApi.myPersonalPatchDocument(documentId, body),
    onMutate: () => setDocumentActionError(null),
    onSuccess: (document) => {
      setDocumentActionError(null);
      setSelectedDocumentId(document.document_id);
      invalidatePersonalKb();
    },
    onError: (error) => setDocumentActionError(actionErrorCode(error)),
  });
  const restoreMutation = useMutation({
    mutationFn: (documentId: string) => knowledgeApi.myPersonalRestoreDocument(documentId),
    onMutate: () => setDocumentActionError(null),
    onSuccess: (document) => {
      setDocumentActionError(null);
      setSelectedDocumentId(document.document_id);
      invalidatePersonalKb();
    },
    onError: (error) => setDocumentActionError(actionErrorCode(error)),
  });
  const rebuildMutation = useMutation({
    mutationFn: (documentId: string) => knowledgeApi.myPersonalRebuildDocument(documentId),
    onMutate: () => setDocumentActionError(null),
    onSuccess: (result) => {
      setDocumentActionError(null);
      setSelectedDocumentId(result.document_id);
      invalidatePersonalKb();
    },
    onError: (error) => setDocumentActionError(actionErrorCode(error)),
  });
  const createGrantMutation = useMutation({
    mutationFn: () => {
      const request: PersonalKnowledgeGrantRequest = {
        resource_type: 'scope',
        grantee_type: granteeType,
        grantee_id: granteeId.trim(),
        permission: grantPermission,
        sensitivity_ceiling: grantSensitivityCeiling,
      };
      if (granteeType === 'agent') {
        request.purpose = grantPurpose;
        request.expires_at = new Date(grantExpiresAt).toISOString();
        if (grantPurpose !== 'autonomous_agent') {
          request.requester_user_id = grantRequesterUserId.trim();
          request.session_id = grantSessionId.trim();
        }
        if (grantPurpose === 'a2a_delegation' || grantPurpose === 'subagent_delegation') {
          request.delegation_id = grantDelegationId.trim();
        }
      }
      return knowledgeApi.myPersonalCreateGrant(request);
    },
    onSuccess: () => {
      setGranteeId('');
      setGrantRequesterUserId('');
      setGrantSessionId('');
      setGrantDelegationId('');
      setGrantExpiresAt(defaultGrantExpiryLocal());
      void queryClient.invalidateQueries({ queryKey: ['personal-knowledge-grants'] });
    },
  });
  const deleteGrantMutation = useMutation({
    mutationFn: (grantId: string) => knowledgeApi.myPersonalDeleteGrant(grantId),
    onSuccess: () => {
      setDeletingGrantId(null);
      void queryClient.invalidateQueries({ queryKey: ['personal-knowledge-grants'] });
    },
    onError: () => setDeletingGrantId(null),
  });
  const decideProposalMutation = useMutation({
    mutationFn: ({ proposalId, decision }: { proposalId: string; decision: 'approve' | 'reject' }) =>
      knowledgeApi.myPersonalDecideProposal(proposalId, {
        decision,
        reason: decision === 'approve' ? 'Owner approved in Personal KB workbench.' : 'Owner rejected in Personal KB workbench.',
      }),
    onSuccess: (proposal) => {
      setBusyProposalId(null);
      if (proposal.document_id) setSelectedDocumentId(proposal.document_id);
      invalidatePersonalKb();
    },
    onError: () => setBusyProposalId(null),
  });
  const rollbackMutation = useMutation({
    mutationFn: ({ documentId, version }: { documentId: string; version: number }) =>
      knowledgeApi.myPersonalRollbackDocument(documentId, version),
    onSuccess: (result) => {
      setRollbackPendingVersion(null);
      setSelectedDocumentId(result.document_id);
      invalidatePersonalKb();
    },
    onError: () => setRollbackPendingVersion(null),
  });

  const stats = useMemo(() => ({
    documents: documents.length,
    segments: documents.reduce((sum, document) => sum + document.segment_count, 0),
    searchable: documents.filter((document) => document.agent_searchable).length,
  }), [documents]);

  const onSearch = (event: FormEvent) => {
    event.preventDefault();
    const normalized = searchInput.trim();
    if (!normalized) return;
    if (normalized === activeSearch) {
      // Retrying the same query is a real action: explicitly refetch.
      void searchQuery.refetch();
      return;
    }
    setActiveSearch(normalized);
  };

  const lanes: Array<{ key: PersonalKnowledgeLane; label: string; helper: string }> = [
    { key: 'inbox', label: t('personalKnowledge.inbox'), helper: t('personalKnowledge.inboxHelper') },
    { key: 'proposals', label: t('personalKnowledge.proposals'), helper: t('personalKnowledge.proposalsHelper') },
    { key: 'library', label: t('personalKnowledge.library'), helper: t('personalKnowledge.libraryHelper', 'canonical MD') },
    { key: 'graph', label: t('personalKnowledge.graph'), helper: t('personalKnowledge.graphHelper') },
    { key: 'profile', label: t('personalKnowledge.profile'), helper: t('personalKnowledge.profileHelper', 'taste / profile') },
    { key: 'grants', label: t('personalKnowledge.grants'), helper: t('personalKnowledge.grantsHelper') },
  ];

  const pageChrome = (
    <>
      <header className="personal-kb-header">
        <div>
          <span className="personal-kb-eyebrow">HIVE · Personal Knowledge</span>
          <h1>{t('personalKnowledge.title')}</h1>
          <p>{t('personalKnowledge.subtitle')}</p>
        </div>
        <Link to="/knowledge/company" className="btn btn-secondary">
          <IconDatabase size={15} stroke={1.7} />
          {t('personalKnowledge.openCompanyLibrary', 'Open Company Library')}
        </Link>
      </header>

      <nav className="personal-kb-tabs" role="tablist" aria-label={t('personalKnowledge.navLabel', 'Personal knowledge sections')}>
        {lanes.map((lane) => (
          <button
            key={lane.key}
            type="button"
            role="tab"
            aria-selected={activeLane === lane.key}
            className={`personal-kb-tab ${activeLane === lane.key ? 'active' : ''}`}
            onClick={() => setActiveLane(lane.key)}
          >
            {laneIcons[lane.key]}
            <span>
              <strong>{lane.label}</strong>
              <small>{lane.helper}</small>
            </span>
          </button>
        ))}
      </nav>
    </>
  );

  if (documentsQuery.isError) {
    return (
      <div className="personal-kb-page">
        {pageChrome}
        <PersonalKnowledgeQueryState
          error={documentsQuery.error}
          onRetry={() => void documentsQuery.refetch()}
        />
      </div>
    );
  }
  if (documentsQuery.isLoading) {
    return (
      <div className="personal-kb-page">
        {pageChrome}
        <div data-testid="personal-knowledge-loading">
          <EmptyBlock>{t('common.loading', 'Loading...')}</EmptyBlock>
        </div>
      </div>
    );
  }

  return (
    <div className="personal-kb-page">
      {pageChrome}
      <section className="personal-kb-command-row">
        <form
          className="personal-kb-search"
          role="search"
          aria-label={t('personalKnowledge.searchLabel', 'Search Personal Knowledge')}
          onSubmit={onSearch}
        >
          <IconSearch size={16} stroke={1.7} />
          <input
            value={searchInput}
            onChange={(event) => setSearchInput(event.target.value)}
            placeholder={t('personalKnowledge.searchPlaceholder')}
          />
          <button
            type="submit"
            className="btn btn-primary btn-sm personal-kb-search-submit"
            disabled={!searchInput.trim() || searchQuery.isFetching}
          >
            {searchQuery.isFetching
              ? t('personalKnowledge.searching', 'Searching...')
              : t('personalKnowledge.searchAction', 'Search')}
          </button>
        </form>
        <button type="button" className="btn btn-primary" onClick={() => setActiveLane('inbox')}>
          {t('personalKnowledge.feed')}
        </button>
      </section>

      <div className="personal-kb-stats" aria-label={t('personalKnowledge.stats', 'Personal knowledge stats')}>
        <span>{stats.documents} {t('personalKnowledge.docUnit')}</span>
        <span>{stats.segments} {t('personalKnowledge.segmentUnit')}</span>
        <span>{stats.searchable} {t('personalKnowledge.searchableUnit')}</span>
      </div>

      <div className="personal-kb-shell">
        <main className="personal-kb-main">
          {activeLane === 'inbox' && (
            <InboxPanel
              title={title}
              markdown={markdown}
              url={url}
              selectedFile={selectedFile}
              jobs={jobsQuery.data?.jobs ?? []}
              jobsLoading={jobsQuery.isLoading}
              jobsError={jobsQuery.isError ? jobsQuery.error : undefined}
              busyJobId={busyJobId}
              onTitleChange={setTitle}
              onMarkdownChange={setMarkdown}
              onUrlChange={setUrl}
              onFileChange={setSelectedFile}
              onPasteSubmit={(event) => {
                event.preventDefault();
                if (markdown.trim()) ingestMutation.mutate();
              }}
              onFileSubmit={(event) => {
                event.preventDefault();
                if (selectedFile) importFileMutation.mutate(selectedFile);
              }}
              onUrlSubmit={(event) => {
                event.preventDefault();
                if (url.trim()) importUrlMutation.mutate();
              }}
              onRetryJob={(jobId) => {
                setBusyJobId(jobId);
                retryMutation.mutate(jobId);
              }}
              onCancelJob={(jobId) => {
                setBusyJobId(jobId);
                cancelJobMutation.mutate(jobId);
              }}
              onRetryJobsQuery={() => void jobsQuery.refetch()}
              actionError={jobActionError}
              intakeError={intakeActionError}
              pastePending={ingestMutation.isPending}
              filePending={importFileMutation.isPending}
              urlPending={importUrlMutation.isPending}
            />
          )}

          {activeLane === 'library' && (
            <LibraryPanel
              documents={documents}
              activeDocumentId={activeDocumentId}
              isLoading={documentsQuery.isLoading}
              onSelect={setSelectedDocumentId}
            />
          )}

          {activeLane === 'proposals' && (
            proposalsQuery.isError ? (
              <PersonalKnowledgeQueryState
                error={proposalsQuery.error}
                onRetry={() => void proposalsQuery.refetch()}
              />
            ) : proposalsQuery.isLoading ? (
              <EmptyBlock>{t('common.loading', 'Loading...')}</EmptyBlock>
            ) : (
              <ProposalReviewPanel
                proposals={proposalsQuery.data?.proposals ?? []}
                busyProposalId={busyProposalId}
                onDecision={(proposalId, decision) => {
                  setBusyProposalId(proposalId);
                  decideProposalMutation.mutate({ proposalId, decision });
                }}
              />
            )
          )}

          {activeLane === 'graph' && (
            graphQuery.isError ? (
              <PersonalKnowledgeQueryState error={graphQuery.error} onRetry={() => void graphQuery.refetch()} />
            ) : graphQuery.isLoading ? (
              <EmptyBlock>{t('common.loading', 'Loading...')}</EmptyBlock>
            ) : (
              <GraphPanel graph={graphQuery.data} />
            )
          )}

          {activeLane === 'profile' && (
            <section className="personal-kb-panel">
              <div className="personal-kb-panel-heading">
                <div>
                  <h2>{t('personalKnowledge.profile')}</h2>
                  <p>{t('personalKnowledge.profileDesc')}</p>
                </div>
                <IconBrain size={18} stroke={1.7} />
              </div>
              <EmptyBlock>{t('personalKnowledge.profileEmpty')}</EmptyBlock>
            </section>
          )}

          {activeLane === 'grants' && (
            grantsQuery.isError ? (
              <PersonalKnowledgeQueryState error={grantsQuery.error} onRetry={() => void grantsQuery.refetch()} />
            ) : grantsQuery.isLoading ? (
              <EmptyBlock>{t('common.loading', 'Loading...')}</EmptyBlock>
            ) : (
              <GrantsPanel
                grants={grantsQuery.data?.grants ?? []}
                granteeType={granteeType}
                granteeId={granteeId}
                permission={grantPermission}
                requesterUserId={grantRequesterUserId}
                sessionId={grantSessionId}
                purpose={grantPurpose}
                delegationId={grantDelegationId}
                sensitivityCeiling={grantSensitivityCeiling}
                expiresAt={grantExpiresAt}
                onGranteeTypeChange={setGranteeType}
                onGranteeIdChange={setGranteeId}
                onPermissionChange={setGrantPermission}
                onRequesterUserIdChange={setGrantRequesterUserId}
                onSessionIdChange={setGrantSessionId}
                onPurposeChange={setGrantPurpose}
                onDelegationIdChange={setGrantDelegationId}
                onSensitivityCeilingChange={setGrantSensitivityCeiling}
                onExpiresAtChange={setGrantExpiresAt}
                onCreate={(event) => {
                  event.preventDefault();
                  if (granteeId.trim()) createGrantMutation.mutate();
                }}
                onDelete={(grantId) => {
                  setDeletingGrantId(grantId);
                  deleteGrantMutation.mutate(grantId);
                }}
                createPending={createGrantMutation.isPending}
                deletingGrantId={deletingGrantId}
              />
            )
          )}

          {activeSearch && (
            searchQuery.isError ? (
              <PersonalKnowledgeQueryState error={searchQuery.error} onRetry={() => void searchQuery.refetch()} />
            ) : searchQuery.isLoading ? (
              <EmptyBlock>{t('common.loading', 'Loading...')}</EmptyBlock>
            ) : (
              <SearchResults results={searchQuery.data?.results ?? []} query={activeSearch} />
            )
          )}
        </main>

        {detailQuery.isError ? (
          <aside className="personal-kb-detail">
            <PersonalKnowledgeQueryState error={detailQuery.error} onRetry={() => void detailQuery.refetch()} />
          </aside>
        ) : detailQuery.isLoading ? (
          <aside className="personal-kb-detail">
            <EmptyBlock>{t('common.loading', 'Loading...')}</EmptyBlock>
          </aside>
        ) : (
          <DocumentDetail
            document={detailQuery.data}
            onRebuild={(documentId) => rebuildMutation.mutate(documentId)}
            onToggleAgentSearchable={(document) =>
              patchMutation.mutate({
                documentId: document.document_id,
                body: { agent_searchable: !document.agent_searchable },
              })
            }
            onArchive={(documentId) => patchMutation.mutate({ documentId, body: { status: 'archived' } })}
            onRestore={(documentId) => restoreMutation.mutate(documentId)}
            rebuildPending={rebuildMutation.isPending}
            patchPending={patchMutation.isPending}
            restorePending={restoreMutation.isPending}
            actionError={documentActionError}
            revisions={revisionsQuery.data?.revisions ?? []}
            revisionsLoading={revisionsQuery.isLoading}
            revisionsError={revisionsQuery.isError ? revisionsQuery.error : undefined}
            rollbackPendingVersion={rollbackPendingVersion}
            onRetryRevisions={() => void revisionsQuery.refetch()}
            onRollback={(version) => {
              if (!activeDocumentId) return;
              setRollbackPendingVersion(version);
              rollbackMutation.mutate({ documentId: activeDocumentId, version });
            }}
          />
        )}
      </div>
    </div>
  );
}
