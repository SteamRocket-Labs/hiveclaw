import { type FormEvent, type ReactNode, useEffect, useMemo, useState } from 'react';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { useTranslation } from 'react-i18next';
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
  type PersonalKnowledgeGrantSummary,
  type PersonalKnowledgeGraphSummary,
  type PersonalKnowledgeJobSummary,
  type PersonalKnowledgeProposalSummary,
  type PersonalKnowledgeRevision,
  type PersonalKnowledgeSearchResult,
} from '../api/domains/knowledge';
import PersonalKnowledgeQueryState from '../components/PersonalKnowledgeQueryState';
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

const importFormats = [
  { label: 'PDF', helper: 'pdf' },
  { label: 'Word / DOCX', helper: 'docx' },
  { label: 'CSV / HTML', helper: 'csv · html' },
  { label: 'Markdown', helper: 'md · txt' },
  { label: '音频', helper: 'mp3 · wav · m4a' },
  { label: '视频', helper: 'mp4 · mov · webm' },
  { label: '图片', helper: 'png · jpg · webp' },
];

function sourceLabel(sourceKind: string): string {
  if (sourceKind === 'paste') return '粘贴';
  if (sourceKind === 'link' || sourceKind === 'url') return '链接';
  if (sourceKind === 'upload') return '上传';
  if (sourceKind === 'chat_attachment') return '聊天附件';
  if (sourceKind === 'agent') return '来自 Agent';
  return sourceKind || '未知';
}

function formatDate(value: string | null): string {
  if (!value) return '';
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return '';
  return new Intl.DateTimeFormat('zh-CN', { month: '2-digit', day: '2-digit' }).format(date);
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

function SearchResults({ results }: { results: PersonalKnowledgeSearchResult[] }) {
  const { t } = useTranslation();
  if (results.length === 0) return null;
  return (
    <section className="personal-kb-panel personal-kb-search-results">
      <div className="personal-kb-panel-heading">
        <h2>{t('personalKnowledge.searchResults', '搜索结果')}</h2>
      </div>
      {results.map((result) => (
        <div key={result.segment_id} className="personal-kb-result">
          <strong>{result.title}</strong>
          <span>{result.heading_path.join(' / ')}</span>
          <p>{result.snippet}</p>
          <code>{result.source_ref}</code>
          {result.score_trace && <small>score_trace · {result.score.toFixed(3)}</small>}
        </div>
      ))}
    </section>
  );
}

function ImportJobs({
  jobs,
  onRetry,
  busyJobId,
}: {
  jobs: PersonalKnowledgeJobSummary[];
  onRetry: (jobId: string) => void;
  busyJobId?: string | null;
}) {
  const { t } = useTranslation();
  if (jobs.length === 0) {
    return <EmptyBlock>{t('personalKnowledge.noJobs', '还没有导入任务。')}</EmptyBlock>;
  }
  return (
    <div className="personal-kb-jobs">
      {jobs.map((job) => {
        const filename = String(job.metadata?.source_filename || job.document_id);
        const canRetry = job.status === 'failed' || job.status === 'degraded';
        return (
          <div key={job.job_id} className="personal-kb-job-row">
            <div>
              <strong>{filename}</strong>
              <span>
                {job.stage} · {job.status} · {t('personalKnowledge.attempts', '尝试')} {job.attempt_count}
              </span>
              {job.error_message && <code>{job.error_message}</code>}
            </div>
            {canRetry && (
              <button
                type="button"
                className="btn btn-secondary btn-sm"
                disabled={busyJobId === job.job_id}
                onClick={() => onRetry(job.job_id)}
              >
                <IconRefresh size={14} stroke={1.7} />
                {t('personalKnowledge.retry', '重试')}
              </button>
            )}
          </div>
        );
      })}
    </div>
  );
}

function InboxPanel({
  title,
  markdown,
  url,
  selectedFile,
  jobs,
  jobsLoading,
  jobsError,
  busyJobId,
  onTitleChange,
  onMarkdownChange,
  onUrlChange,
  onFileChange,
  onPasteSubmit,
  onFileSubmit,
  onUrlSubmit,
  onRetryJob,
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
  onTitleChange: (value: string) => void;
  onMarkdownChange: (value: string) => void;
  onUrlChange: (value: string) => void;
  onFileChange: (file: File | null) => void;
  onPasteSubmit: (event: FormEvent) => void;
  onFileSubmit: (event: FormEvent) => void;
  onUrlSubmit: (event: FormEvent) => void;
  onRetryJob: (jobId: string) => void;
  onRetryJobsQuery: () => void;
  pastePending: boolean;
  filePending: boolean;
  urlPending: boolean;
}) {
  const { t } = useTranslation();
  return (
    <section className="personal-kb-panel personal-kb-intake">
      <div className="personal-kb-panel-heading">
        <div>
          <h2>{t('personalKnowledge.inboxTitle', '收集箱')}</h2>
          <p>{t('personalKnowledge.inboxDesc', '文件、URL、Markdown、聊天附件都进入同一条后端摄取管线；失败和降级会在任务里可见。')}</p>
        </div>
        <IconDatabase size={18} stroke={1.7} />
      </div>

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
          <strong>{t('personalKnowledge.dropOrChoose', '拖拽或选择文件')}</strong>
          <span>{selectedFile ? selectedFile.name : t('personalKnowledge.dropHelper', 'PDF、DOCX、CSV、HTML、Markdown、音频、视频或图片')}</span>
          <input
            type="file"
            onChange={(event) => onFileChange(event.target.files?.[0] ?? null)}
          />
        </label>
        <div className="personal-kb-format-grid">
          {importFormats.map((format) => (
            <span key={format.label}>
              <strong>{format.label}</strong>
              <small>{format.helper}</small>
            </span>
          ))}
        </div>
        <button type="submit" className="btn btn-primary" disabled={!selectedFile || filePending}>
          {filePending ? t('personalKnowledge.importing', '导入中') : t('personalKnowledge.importFile', '导入文件')}
        </button>
      </form>

      <form className="personal-kb-url-form" onSubmit={onUrlSubmit}>
        <div>
          <strong>{t('personalKnowledge.urlImport', 'URL 导入')}</strong>
          <span>{t('personalKnowledge.urlImportDesc', '网页会由后端转换为 canonical Markdown。')}</span>
        </div>
        <input
          value={url}
          onChange={(event) => onUrlChange(event.target.value)}
          placeholder="https://example.com/research"
        />
        <button type="submit" className="btn btn-secondary" disabled={!url.trim() || urlPending}>
          <IconWorld size={14} stroke={1.7} />
          {t('personalKnowledge.importUrl', '导入 URL')}
        </button>
      </form>

      <form id="personal-kb-intake-form" onSubmit={onPasteSubmit} className="personal-kb-intake-form">
        <input
          value={title}
          onChange={(event) => onTitleChange(event.target.value)}
          placeholder={t('personalKnowledge.titlePlaceholder', '文档标题')}
        />
        <textarea
          value={markdown}
          onChange={(event) => onMarkdownChange(event.target.value)}
          placeholder={t('personalKnowledge.markdownPlaceholder', '粘贴 Markdown、会议纪要、研究笔记或可归档内容...')}
        />
        <div className="personal-kb-panel-actions">
          <button type="submit" className="btn btn-primary" disabled={!markdown.trim() || pastePending}>
            {pastePending ? t('common.saving', 'Saving...') : t('personalKnowledge.feed', '+ 投喂')}
          </button>
        </div>
      </form>

      <div className="personal-kb-subsection">
        <h3>{t('personalKnowledge.importJobs', '导入任务')}</h3>
        {jobsError ? (
          <PersonalKnowledgeQueryState error={jobsError} onRetry={onRetryJobsQuery} />
        ) : jobsLoading ? (
          <EmptyBlock>{t('common.loading', 'Loading...')}</EmptyBlock>
        ) : (
          <ImportJobs jobs={jobs} onRetry={onRetryJob} busyJobId={busyJobId} />
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
          <h2>{t('personalKnowledge.libraryTitle', '文库')}</h2>
          <p>{t('personalKnowledge.libraryDesc', '这里是 Owner scope 的 canonical documents，不属于任何单个 Agent。')}</p>
        </div>
        <IconFileText size={18} stroke={1.7} />
      </div>
      {isLoading && <EmptyBlock>{t('common.loading', 'Loading...')}</EmptyBlock>}
      {!isLoading && documents.length === 0 && (
        <EmptyBlock>{t('personalKnowledge.empty', '个人知识库为空。先从收集箱投喂一条内容。')}</EmptyBlock>
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
              <small>{sourceLabel(document.source_kind)} · {document.status}</small>
            </span>
            <span className="personal-kb-doc-tags">
              {documentTags(document).map((tag) => <em key={tag}>{tag}</em>)}
            </span>
            <span className="personal-kb-doc-meta">
              {formatDate(document.created_at)}
              {formatDate(document.created_at) ? ' · ' : ''}
              {document.segment_count} {t('personalKnowledge.segmentUnit', '段落')} · {document.sensitivity}
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
          <h2>{t('personalKnowledge.graph', '知识网')}</h2>
          <p>{t('personalKnowledge.graphDesc', '实体、关系和断言都来自后端抽取结果，空状态不在前端伪造语义。')}</p>
        </div>
        <IconSitemap size={18} stroke={1.7} />
      </div>
      <div className="personal-kb-mini-grid">
        <span>{entities.length} entities</span>
        <span>{links.length} links</span>
        <span>{assertions.length} assertions</span>
      </div>
      {entities.length === 0 ? (
        <EmptyBlock>{t('personalKnowledge.graphEmpty', '还没有实体图谱。导入并完成抽取后会出现在这里。')}</EmptyBlock>
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

function GrantsPanel({
  grants,
  granteeType,
  granteeId,
  permission,
  onGranteeTypeChange,
  onGranteeIdChange,
  onPermissionChange,
  onCreate,
  onDelete,
  createPending,
  deletingGrantId,
}: {
  grants: PersonalKnowledgeGrantSummary[];
  granteeType: 'user' | 'agent' | 'session';
  granteeId: string;
  permission: 'read' | 'search' | 'manage';
  onGranteeTypeChange: (value: 'user' | 'agent' | 'session') => void;
  onGranteeIdChange: (value: string) => void;
  onPermissionChange: (value: 'read' | 'search' | 'manage') => void;
  onCreate: (event: FormEvent) => void;
  onDelete: (grantId: string) => void;
  createPending: boolean;
  deletingGrantId?: string | null;
}) {
  const { t } = useTranslation();
  return (
    <section className="personal-kb-panel">
      <div className="personal-kb-panel-heading">
        <div>
          <h2>{t('personalKnowledge.grants', '授权')}</h2>
          <p>{t('personalKnowledge.grantsDesc', 'Owner 默认可读写；Agent、user、session grant 都由后端 knowledge_grants 判定。')}</p>
        </div>
        <IconShieldCheck size={18} stroke={1.7} />
      </div>
      <form className="personal-kb-grant-form" onSubmit={onCreate}>
        <select value={granteeType} onChange={(event) => onGranteeTypeChange(event.target.value as 'user' | 'agent' | 'session')}>
          <option value="agent">agent</option>
          <option value="user">user</option>
          <option value="session">session</option>
        </select>
        <input value={granteeId} onChange={(event) => onGranteeIdChange(event.target.value)} placeholder="grantee UUID" />
        <select value={permission} onChange={(event) => onPermissionChange(event.target.value as 'read' | 'search' | 'manage')}>
          <option value="search">search</option>
          <option value="read">read</option>
          <option value="manage">manage</option>
        </select>
        <button type="submit" className="btn btn-primary" disabled={!granteeId.trim() || createPending}>
          {t('personalKnowledge.createGrant', '创建授权')}
        </button>
      </form>
      <div className="personal-kb-grant-list">
        {grants.map((grant) => (
          <div key={grant.grant_id} className="personal-kb-grant-row">
            <div>
              <strong>{grant.grantee_type}:{grant.grantee_id}</strong>
              <span>{grant.permission} · {grant.resource_type}</span>
              {grant.expires_at && <code>{grant.expires_at}</code>}
            </div>
            <button
              type="button"
              className="btn btn-secondary btn-sm"
              disabled={deletingGrantId === grant.grant_id}
              onClick={() => onDelete(grant.grant_id)}
            >
              <IconTrash size={14} stroke={1.7} />
              {t('personalKnowledge.revoke', '撤销')}
            </button>
          </div>
        ))}
        {grants.length === 0 && <EmptyBlock>{t('personalKnowledge.noGrants', '还没有额外授权。')}</EmptyBlock>}
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
          <h2>{t('personalKnowledge.proposals', 'Agent 提案')}</h2>
          <p>{t('personalKnowledge.proposalsDesc', 'Agent 只能提交候选；Owner 在这里核对 diff、来源和敏感级别后决定是否写入。')}</p>
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
            <div className="personal-kb-preview-title">{t('personalKnowledge.proposalDiff', '候选 diff')}</div>
            <pre className="personal-kb-diff" aria-label={t('personalKnowledge.proposalDiff', '候选 diff')}>
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
                  {t('personalKnowledge.approveProposal', '批准并写入')}
                </button>
                <button
                  type="button"
                  className="btn btn-secondary btn-sm"
                  disabled={busyProposalId === proposal.proposal_id}
                  onClick={() => onDecision(proposal.proposal_id, 'reject')}
                >
                  {t('personalKnowledge.rejectProposal', '拒绝')}
                </button>
              </div>
            )}
          </article>
        ))}
        {proposals.length === 0 && (
          <EmptyBlock>{t('personalKnowledge.noProposals', '当前没有 Agent 提案。')}</EmptyBlock>
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
      <div className="personal-kb-preview-title">{t('personalKnowledge.revisionHistory', '版本历史')}</div>
      {revisions.map((revision, index) => (
        <div key={revision.id} className="personal-kb-revision-row">
          <div>
            <strong>{t('personalKnowledge.version', '版本')} {revision.version}</strong>
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
              {t('personalKnowledge.rollbackVersion', '回滚到此版本')}
            </button>
          )}
        </div>
      ))}
      {revisions.length === 0 && <small>{t('personalKnowledge.noRevisions', '尚无可回滚版本。')}</small>}
    </div>
  );
}

function DocumentDetail({
  document,
  onRebuild,
  onToggleAgentSearchable,
  onArchive,
  rebuildPending,
  patchPending,
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
  rebuildPending: boolean;
  patchPending: boolean;
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
        <EmptyBlock>{t('personalKnowledge.selectDocument', '选择一条文档查看 source refs 和段落证据。')}</EmptyBlock>
      </aside>
    );
  }

  return (
    <aside className="personal-kb-detail">
      <div className="personal-kb-detail-head">
        <div>
          <span className="personal-kb-eyebrow">{t('personalKnowledge.detailEyebrow', '文档详情')}</span>
          <h2>{document.title}</h2>
        </div>
        <span className="ui-chip">{document.status}</span>
      </div>
      {imagePreview && (
        <div className="personal-kb-source-preview">
          <div className="personal-kb-preview-title">{t('personalKnowledge.sourceImagePreview', '源图片预览')}</div>
          {imagePreviewUrl ? (
            <img src={imagePreviewUrl} alt={imagePreview.filename} />
          ) : imagePreviewQuery.isError ? (
            <PersonalKnowledgeQueryState
              error={imagePreviewQuery.error}
              onRetry={() => void imagePreviewQuery.refetch()}
            />
          ) : (
            <div className="personal-kb-source-preview-placeholder">
              {t('personalKnowledge.sourceImagePreviewLoading', '正在加载源图片...')}
            </div>
          )}
          <small>{imagePreview.filename}</small>
        </div>
      )}
      <div className="personal-kb-preview">
        <div className="personal-kb-preview-title">{t('personalKnowledge.mdPreview', 'MD 预览')}</div>
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
        <h3>{t('personalKnowledge.evidenceChain', '证据链')}</h3>
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
        <button
          type="button"
          className="btn btn-secondary btn-sm"
          disabled={rebuildPending}
          onClick={() => onRebuild(document.document_id)}
        >
          <IconRefresh size={14} stroke={1.7} />
          {t('personalKnowledge.rebuildIndex', '重建索引')}
        </button>
        <button
          type="button"
          className="btn btn-secondary btn-sm"
          disabled={patchPending}
          onClick={() => onToggleAgentSearchable(document)}
        >
          {document.agent_searchable
            ? t('personalKnowledge.blockAgentSearch', '禁止 Agent 检索')
            : t('personalKnowledge.allowAgentSearch', '允许 Agent 检索')}
        </button>
        <button
          type="button"
          className="btn btn-secondary btn-sm"
          disabled={patchPending}
          onClick={() => onArchive(document.document_id)}
        >
          {t('personalKnowledge.archive', '归档')}
        </button>
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
  const [granteeType, setGranteeType] = useState<'user' | 'agent' | 'session'>('agent');
  const [granteeId, setGranteeId] = useState('');
  const [grantPermission, setGrantPermission] = useState<'read' | 'search' | 'manage'>('search');
  const [deletingGrantId, setDeletingGrantId] = useState<string | null>(null);
  const [busyProposalId, setBusyProposalId] = useState<string | null>(null);
  const [rollbackPendingVersion, setRollbackPendingVersion] = useState<number | null>(null);

  const documentsQuery = useQuery({
    queryKey: ['personal-knowledge-documents'],
    queryFn: () => knowledgeApi.myPersonalDocuments(),
  });
  const jobsQuery = useQuery({
    queryKey: ['personal-knowledge-import-jobs'],
    queryFn: () => knowledgeApi.myPersonalImportJobs(),
    enabled: activeLane === 'inbox',
  });
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
    if (activeDocumentId) void queryClient.invalidateQueries({ queryKey: ['personal-knowledge-document', activeDocumentId] });
    if (activeDocumentId) void queryClient.invalidateQueries({ queryKey: ['personal-knowledge-revisions', activeDocumentId] });
  };

  const ingestMutation = useMutation({
    mutationFn: () =>
      knowledgeApi.myPersonalIngest({
        title: title.trim() || t('personalKnowledge.untitled', '未命名笔记'),
        markdown,
        source_kind: 'paste',
        source_uri: 'browser://knowledge/personal',
        agent_searchable: true,
        sensitivity: 'internal',
      }),
    onSuccess: (result) => {
      setTitle('');
      setMarkdown('');
      setSelectedDocumentId(result.document_id);
      invalidatePersonalKb();
    },
  });
  const importFileMutation = useMutation({
    mutationFn: (file: File) => knowledgeApi.myPersonalImportFile(file, { title: title.trim() || file.name, sensitivity: 'internal' }),
    onSuccess: (result) => {
      setSelectedFile(null);
      setSelectedDocumentId(result.document_id);
      invalidatePersonalKb();
    },
  });
  const importUrlMutation = useMutation({
    mutationFn: () => knowledgeApi.myPersonalImportUrl({ url: url.trim(), title: title.trim() || undefined, sensitivity: 'internal' }),
    onSuccess: (result) => {
      setUrl('');
      setSelectedDocumentId(result.document_id);
      invalidatePersonalKb();
    },
  });
  const retryMutation = useMutation({
    mutationFn: (jobId: string) => knowledgeApi.myPersonalRetryImportJob(jobId),
    onSuccess: (result) => {
      setBusyJobId(null);
      setSelectedDocumentId(result.document_id);
      invalidatePersonalKb();
    },
    onError: () => setBusyJobId(null),
  });
  const patchMutation = useMutation({
    mutationFn: ({ documentId, body }: { documentId: string; body: { agent_searchable?: boolean; status?: string } }) =>
      knowledgeApi.myPersonalPatchDocument(documentId, body),
    onSuccess: (document) => {
      setSelectedDocumentId(document.document_id);
      invalidatePersonalKb();
    },
  });
  const rebuildMutation = useMutation({
    mutationFn: (documentId: string) => knowledgeApi.myPersonalRebuildDocument(documentId),
    onSuccess: (result) => {
      setSelectedDocumentId(result.document_id);
      invalidatePersonalKb();
    },
  });
  const createGrantMutation = useMutation({
    mutationFn: () =>
      knowledgeApi.myPersonalCreateGrant({
        resource_type: 'scope',
        grantee_type: granteeType,
        grantee_id: granteeId.trim(),
        permission: grantPermission,
      }),
    onSuccess: () => {
      setGranteeId('');
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
    setActiveSearch(searchInput.trim());
  };

  const lanes: Array<{ key: PersonalKnowledgeLane; label: string; helper: string }> = [
    { key: 'inbox', label: t('personalKnowledge.inbox', '收集箱'), helper: t('personalKnowledge.inboxHelper', '投喂与管线') },
    { key: 'proposals', label: t('personalKnowledge.proposals', 'Agent 提案'), helper: t('personalKnowledge.proposalsHelper', '审批与 diff') },
    { key: 'library', label: t('personalKnowledge.library', '文库'), helper: t('personalKnowledge.libraryHelper', 'canonical MD') },
    { key: 'graph', label: t('personalKnowledge.graph', '知识网'), helper: t('personalKnowledge.graphHelper', '实体与关系') },
    { key: 'profile', label: t('personalKnowledge.profile', '画像'), helper: t('personalKnowledge.profileHelper', 'taste / profile') },
    { key: 'grants', label: t('personalKnowledge.grants', '授权'), helper: t('personalKnowledge.grantsHelper', 'Agent 检索边界') },
  ];

  const pageChrome = (
    <>
      <header className="personal-kb-header">
        <div>
          <span className="personal-kb-eyebrow">HIVE · Personal Knowledge</span>
          <h1>{t('personalKnowledge.title', '个人知识库')}</h1>
          <p>{t('personalKnowledge.subtitle', 'Owner 级别的一份真相：文档、笔记、画像、授权入口都从这里进入。')}</p>
        </div>
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
        <form className="personal-kb-search" onSubmit={onSearch}>
          <IconSearch size={16} stroke={1.7} />
          <input
            value={searchInput}
            onChange={(event) => setSearchInput(event.target.value)}
            placeholder={t('personalKnowledge.searchPlaceholder', '搜索你的一切经手信息...')}
          />
        </form>
        <button type="button" className="btn btn-primary" onClick={() => setActiveLane('inbox')}>
          {t('personalKnowledge.feed', '+ 投喂')}
        </button>
      </section>

      <div className="personal-kb-stats" aria-label={t('personalKnowledge.stats', 'Personal knowledge stats')}>
        <span>{stats.documents} {t('personalKnowledge.docUnit', '文档')}</span>
        <span>{stats.segments} {t('personalKnowledge.segmentUnit', '段落')}</span>
        <span>{stats.searchable} {t('personalKnowledge.searchableUnit', 'Agent 可检索')}</span>
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
              onRetryJobsQuery={() => void jobsQuery.refetch()}
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
                  <h2>{t('personalKnowledge.profile', '画像')}</h2>
                  <p>{t('personalKnowledge.profileDesc', '画像来自后端抽取和归档结果；当前页面只显示 Owner scope 的入口与状态，不在前端手写语义。')}</p>
                </div>
                <IconBrain size={18} stroke={1.7} />
              </div>
              <EmptyBlock>{t('personalKnowledge.profileEmpty', '后端尚未产出 profile plane。')}</EmptyBlock>
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
                onGranteeTypeChange={setGranteeType}
                onGranteeIdChange={setGranteeId}
                onPermissionChange={setGrantPermission}
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

          {searchQuery.isError ? (
            <PersonalKnowledgeQueryState error={searchQuery.error} onRetry={() => void searchQuery.refetch()} />
          ) : searchQuery.isLoading ? (
            <EmptyBlock>{t('common.loading', 'Loading...')}</EmptyBlock>
          ) : (
            <SearchResults results={searchQuery.data?.results ?? []} />
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
            rebuildPending={rebuildMutation.isPending}
            patchPending={patchMutation.isPending}
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
