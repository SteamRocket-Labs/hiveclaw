import { type FormEvent, type ReactNode, useMemo, useState } from 'react';
import { Link } from 'react-router-dom';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { useTranslation } from 'react-i18next';
import {
  IconArchive,
  IconBrain,
  IconDatabase,
  IconFileText,
  IconSearch,
  IconShieldCheck,
  IconSitemap,
  IconUser,
} from '@tabler/icons-react';
import {
  knowledgeApi,
  type PersonalKnowledgeDocumentDetail,
  type PersonalKnowledgeDocumentSummary,
  type PersonalKnowledgeSearchResult,
} from '../api/domains/knowledge';
import './PersonalKnowledge.css';

type PersonalKnowledgeLane = 'inbox' | 'library' | 'graph' | 'profile' | 'grants';

const laneIcons: Record<PersonalKnowledgeLane, ReactNode> = {
  inbox: <IconArchive size={15} stroke={1.7} />,
  library: <IconFileText size={15} stroke={1.7} />,
  graph: <IconSitemap size={15} stroke={1.7} />,
  profile: <IconUser size={15} stroke={1.7} />,
  grants: <IconShieldCheck size={15} stroke={1.7} />,
};

function sourceLabel(sourceKind: string): string {
  if (sourceKind === 'paste') return '粘贴';
  if (sourceKind === 'link' || sourceKind === 'url') return '链接';
  if (sourceKind === 'upload') return '上传';
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
        </div>
      ))}
    </section>
  );
}

function DocumentDetail({ document }: { document?: PersonalKnowledgeDocumentDetail }) {
  const { t } = useTranslation();
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
      <div className="personal-kb-detail-actions">
        <button type="button" className="btn btn-secondary btn-sm">
          {t('personalKnowledge.rebuildIndex', '重建索引')}
        </button>
        <button type="button" className="btn btn-secondary btn-sm">
          {document.agent_searchable
            ? t('personalKnowledge.agentSearchable', '允许 Agent 检索')
            : t('personalKnowledge.agentBlocked', '禁止 Agent 检索')}
        </button>
        <button type="button" className="btn btn-secondary btn-sm">
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

  const documentsQuery = useQuery({
    queryKey: ['personal-knowledge-documents'],
    queryFn: () => knowledgeApi.myPersonalDocuments(),
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
      void queryClient.invalidateQueries({ queryKey: ['personal-knowledge-documents'] });
      void queryClient.invalidateQueries({ queryKey: ['personal-knowledge-document', result.document_id] });
    },
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

  const onIngest = (event: FormEvent) => {
    event.preventDefault();
    if (markdown.trim()) ingestMutation.mutate();
  };

  const lanes: Array<{ key: PersonalKnowledgeLane; label: string; helper: string }> = [
    { key: 'inbox', label: t('personalKnowledge.inbox', '收集箱'), helper: t('personalKnowledge.inboxHelper', '投喂与管线') },
    { key: 'library', label: t('personalKnowledge.library', '文库'), helper: t('personalKnowledge.libraryHelper', 'canonical MD') },
    { key: 'graph', label: t('personalKnowledge.graph', '知识网'), helper: t('personalKnowledge.graphHelper', '实体与关系') },
    { key: 'profile', label: t('personalKnowledge.profile', '画像'), helper: t('personalKnowledge.profileHelper', 'taste / profile') },
    { key: 'grants', label: t('personalKnowledge.grants', '授权'), helper: t('personalKnowledge.grantsHelper', 'Agent 检索边界') },
  ];

  return (
    <div className="personal-kb-page">
      <header className="personal-kb-header">
        <div>
          <span className="personal-kb-eyebrow">HIVE · Personal Knowledge</span>
          <h1>{t('personalKnowledge.title', '个人知识库')}</h1>
          <p>{t('personalKnowledge.subtitle', 'Owner 级别的一份真相：文档、笔记、画像、授权入口都从这里进入。')}</p>
        </div>
        <Link to="/enterprise/memory" className="btn btn-secondary">
          <IconShieldCheck size={15} stroke={1.7} />
          {t('personalKnowledge.companyReadonly', '企业库（只读）')}
        </Link>
      </header>

      <div className="personal-kb-shell">
        <nav className="personal-kb-rail" aria-label={t('personalKnowledge.navLabel', 'Personal knowledge sections')}>
          {lanes.map((lane) => (
            <button
              key={lane.key}
              type="button"
              className={`personal-kb-rail-item ${activeLane === lane.key ? 'active' : ''}`}
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

        <main className="personal-kb-main">
          <section className="personal-kb-toolbar">
            <form className="personal-kb-search" onSubmit={onSearch}>
              <IconSearch size={16} stroke={1.7} />
              <input
                value={searchInput}
                onChange={(event) => setSearchInput(event.target.value)}
                placeholder={t('personalKnowledge.searchPlaceholder', '搜索你的一切经手信息...')}
              />
            </form>
            <button
              type="submit"
              form="personal-kb-intake-form"
              className="btn btn-primary"
              disabled={!markdown.trim() || ingestMutation.isPending}
            >
              {ingestMutation.isPending ? t('common.saving', 'Saving...') : t('personalKnowledge.feed', '+ 投喂')}
            </button>
          </section>

          <div className="personal-kb-stats" aria-label={t('personalKnowledge.stats', 'Personal knowledge stats')}>
            <span>{stats.documents} {t('personalKnowledge.docUnit', '文档')}</span>
            <span>{stats.segments} {t('personalKnowledge.segmentUnit', '段落')}</span>
            <span>{stats.searchable} {t('personalKnowledge.searchableUnit', 'Agent 可检索')}</span>
          </div>

          <section className="personal-kb-panel personal-kb-intake">
            <div className="personal-kb-panel-heading">
              <div>
                <h2>{t('personalKnowledge.inboxTitle', '收集箱')}</h2>
                <p>{t('personalKnowledge.inboxDesc', '当前后端真相支持 Markdown / notes 直投；上传和 URL 转换继续走后端统一摄取能力后再打开。')}</p>
              </div>
              <IconDatabase size={18} stroke={1.7} />
            </div>
            <form id="personal-kb-intake-form" onSubmit={onIngest} className="personal-kb-intake-form">
              <input
                value={title}
                onChange={(event) => setTitle(event.target.value)}
                placeholder={t('personalKnowledge.titlePlaceholder', '文档标题')}
              />
              <textarea
                value={markdown}
                onChange={(event) => setMarkdown(event.target.value)}
                placeholder={t('personalKnowledge.markdownPlaceholder', '粘贴 Markdown、会议纪要、研究笔记或可归档内容...')}
              />
            </form>
          </section>

          <SearchResults results={searchQuery.data?.results ?? []} />

          <section className="personal-kb-panel">
            <div className="personal-kb-panel-heading">
              <div>
                <h2>{t('personalKnowledge.libraryTitle', '文库')}</h2>
                <p>{t('personalKnowledge.libraryDesc', '这里是 Owner scope 的 canonical documents，不属于任何单个 Agent。')}</p>
              </div>
              <IconFileText size={18} stroke={1.7} />
            </div>
            {documentsQuery.isLoading && <EmptyBlock>{t('common.loading', 'Loading...')}</EmptyBlock>}
            {!documentsQuery.isLoading && documents.length === 0 && (
              <EmptyBlock>{t('personalKnowledge.empty', '个人知识库为空。先从收集箱投喂一条 Markdown。')}</EmptyBlock>
            )}
            <div className="personal-kb-document-list">
              {documents.map((document) => (
                <button
                  key={document.document_id}
                  type="button"
                  className={`personal-kb-doc ${activeDocumentId === document.document_id ? 'active' : ''}`}
                  onClick={() => setSelectedDocumentId(document.document_id)}
                >
                  <span className="personal-kb-doc-head">
                    <strong>{document.title}</strong>
                    <small>{sourceLabel(document.source_kind)}</small>
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

          <section className="personal-kb-lower-grid">
            <div className="personal-kb-panel">
              <div className="personal-kb-panel-heading">
                <div>
                  <h2>{t('personalKnowledge.graph', '知识网')}</h2>
                  <p>{t('personalKnowledge.graphDesc', 'M1 先展示文档、段落、source refs；实体图谱以后端抽取结果为准。')}</p>
                </div>
                <IconSitemap size={18} stroke={1.7} />
              </div>
              <div className="personal-kb-mini-grid">
                <span><IconDatabase size={14} />{stats.documents} docs</span>
                <span><IconBrain size={14} />profile plane</span>
              </div>
            </div>
            <div className="personal-kb-panel">
              <div className="personal-kb-panel-heading">
                <div>
                  <h2>{t('personalKnowledge.grants', '授权')}</h2>
                  <p>{t('personalKnowledge.grantsDesc', 'Owner 默认可读写；Agent / user grant 由后端 knowledge_grants 判定。')}</p>
                </div>
                <IconShieldCheck size={18} stroke={1.7} />
              </div>
              <div className="personal-kb-mini-grid">
                <span>{t('personalKnowledge.ownerGrant', 'Owner default grant')}</span>
                <span>{t('personalKnowledge.agentSearchGate', 'agent_searchable gate')}</span>
              </div>
            </div>
          </section>
        </main>

        <DocumentDetail document={detailQuery.data} />
      </div>
    </div>
  );
}
