import { useEffect, useState } from 'react';
import { useQuery } from '@tanstack/react-query';
import { useTranslation } from 'react-i18next';

import AgentMindSection from './AgentMindSection';
import MarkdownRenderer from '../../components/MarkdownRenderer';
import PersonalKnowledgeQueryState from '../../components/PersonalKnowledgeQueryState';
import {
  knowledgeApi,
  type KnowledgeEntry,
  type KnowledgeOverview,
  type KnowledgePageSummary,
  type PersonalKnowledgeDocumentDetail,
  type PersonalKnowledgeDocumentSummary,
  type PersonalKnowledgeSearchResult,
} from '../../api/domains/knowledge';
import './AgentKnowledgeSection.css';

// 记忆 tab — the two-plane world (memory spec v1.2):
//   profile plane converges (self 自我认知 / profiles 人际与领域),
//   knowledge plane networks (knowledge 知识网络 / milestones 里程碑).
// First screen answers "这个员工记住了什么、最近学到什么、管线健康吗".
// Raw stays the admin escape hatch over soul.md / memory/.

type SubView = 'overview' | 'self' | 'profiles' | 'personal' | 'knowledge' | 'milestones' | 'timeline' | 'raw';

const SUBVIEWS: SubView[] = ['overview', 'self', 'profiles', 'personal', 'knowledge', 'milestones', 'timeline', 'raw'];

type AgentKnowledgeSectionProps = {
  agentId: string;
  canEdit: boolean;
  onNavigateTab?: (tab: string) => void;
};

const FAILURE_STATUS_STYLE: Record<string, { cls: string; fallback: string }> = {
  active: { cls: 'agent-knowledge-fm-active', fallback: 'active' },
  规避中: { cls: 'agent-knowledge-fm-mitigating', fallback: 'mitigating' },
  已根除: { cls: 'agent-knowledge-fm-resolved', fallback: 'resolved' },
};

const MEMORY_STATUS_CLASS: Record<string, string> = {
  remembered: 'agent-knowledge-memory-status--remembered',
  consolidating: 'agent-knowledge-memory-status--consolidating',
  needs_attention: 'agent-knowledge-memory-status--needs-attention',
  empty: 'agent-knowledge-memory-status--empty',
};

function resolveMemoryStatus(overview: KnowledgeOverview): NonNullable<KnowledgeOverview['memoryStatus']> {
  if (overview.memoryStatus) {
    if (Object.prototype.hasOwnProperty.call(MEMORY_STATUS_CLASS, overview.memoryStatus.state)) {
      return overview.memoryStatus;
    }
    // Unknown server states are contract failures, not employee-facing copy.
    return {
      ...overview.memoryStatus,
      state: 'needs_attention',
      issueCount: Math.max(1, overview.memoryStatus.issueCount),
    };
  }

  // Rolling-deploy compatibility for an older backend overview response.
  const longTermItems = overview.planes.self.entries
    + overview.planes.profiles.entries
    + overview.planes.knowledge.pages
    + overview.planes.milestones.pages;
  const pendingItems = Number(overview.pipeline?.pendingPackages || 0) + overview.planes.explicit.active;
  const availableForRecall = Boolean(longTermItems || overview.planes.explicit.active);
  const state = overview.pipeline?.stalled
    ? 'needs_attention'
    : pendingItems
      ? 'consolidating'
      : availableForRecall
        ? 'remembered'
        : 'empty';
  return {
    state,
    availableForRecall,
    recentMemoryAvailable: Boolean(overview.planes.explicit.active),
    longTermMemoryAvailable: Boolean(longTermItems),
    pendingConsolidation: Boolean(pendingItems),
    pendingItems,
    issueCount: overview.pipeline?.stalled ? 1 : 0,
  };
}

export function knowledgeEntryHeading(entry: KnowledgeEntry, fallback: string): string {
  const firstLine = (entry.content || '').split('\n').find((line) => line.startsWith('### '));
  if (firstLine) return firstLine.replace(/^###\s+/, '');
  return entry.preview.trim() || fallback;
}

function entryStatusLine(entry: KnowledgeEntry): string {
  const line = (entry.content || '').split('\n').find((raw) => raw.trim().startsWith('- 状态:'));
  return line ? line.trim().replace(/^- 状态:\s*/, '') : '';
}

function formatKnowledgeTimestamp(value: string, language: string): string {
  const parsed = new Date(value);
  if (Number.isNaN(parsed.getTime())) return value;
  return new Intl.DateTimeFormat(language, { dateStyle: 'medium', timeStyle: 'short' }).format(parsed);
}

function personalSensitivityLabel(value: string, t: ReturnType<typeof useTranslation>['t']): string {
  return t(`agent.knowledge.personalSensitivity.${value}`, t('agent.knowledge.personalSensitivity.unknown'));
}

function personalStatusLabel(value: string, t: ReturnType<typeof useTranslation>['t']): string {
  return t(`agent.knowledge.personalStatus.${value}`, t('agent.knowledge.personalStatus.unknown'));
}

function profileStatusLabel(value: string, t: ReturnType<typeof useTranslation>['t']): string {
  const key = value === '规避中' ? 'mitigating' : value === '已根除' ? 'resolved' : value;
  return t(`agent.knowledge.profileStatus.${key}`, t('agent.knowledge.profileStatus.recorded'));
}

function memoryEventKindLabel(value: string, t: ReturnType<typeof useTranslation>['t']): string {
  if (value.startsWith('curation:')) return t('agent.knowledge.eventKind.curation');
  if (value === 'dream:consolidation') return t('agent.knowledge.eventKind.consolidation');
  return t('agent.knowledge.eventKind.update');
}

function memoryEventOutcomeLabel(value: string, t: ReturnType<typeof useTranslation>['t']): string {
  if (['accepted', 'approved', 'committed', 'completed', 'success'].includes(value)) {
    return t('agent.knowledge.eventOutcome.completed');
  }
  if (['held', 'pending', 'needs_attention'].includes(value)) {
    return t('agent.knowledge.eventOutcome.waiting');
  }
  return t('agent.knowledge.eventOutcome.recorded');
}

function OverviewCards({
  overview,
  canOpenIdentity,
  onOpenSubView,
  onNavigateTab,
}: {
  overview: KnowledgeOverview;
  canOpenIdentity: boolean;
  onOpenSubView: (view: SubView) => void;
  onNavigateTab?: (tab: string) => void;
}) {
  const { t, i18n } = useTranslation();
  const fm = overview.planes.self.failureModes;
  const memoryStatus = resolveMemoryStatus(overview);
  const statusClass = MEMORY_STATUS_CLASS[memoryStatus.state] ?? MEMORY_STATUS_CLASS.empty;
  const descriptionState = memoryStatus.state === 'consolidating' && !memoryStatus.recentMemoryAvailable
    ? 'consolidating_pending'
    : memoryStatus.state;
  return (
    <div className="agent-knowledge-overview-grid">
      <div className="agent-knowledge-card">
        <h2 className="agent-knowledge-card-title">🧬 {t('agent.knowledge.identityCard')}</h2>
        <div className="agent-knowledge-card-body">
          <div>{t('agent.knowledge.soulSections', 'Soul sections')}: {overview.identity.sections}</div>
          {canOpenIdentity && (
            <button
              className="btn btn-sm agent-knowledge-inline-btn"
              type="button"
              onClick={() => onOpenSubView('raw')}
            >
              {t('agent.knowledge.viewCurrentIdentity')} →
            </button>
          )}
          <div>
            {t('agent.knowledge.pendingSoul')}: {overview.identity.pendingSoulCandidates}
            {overview.identity.pendingSoulCandidates > 0 && onNavigateTab && (
              <button className="btn btn-sm agent-knowledge-inline-btn" onClick={() => onNavigateTab('evolution')}>
                {t('agent.knowledge.goApprove')} →
              </button>
            )}
          </div>
        </div>
      </div>
      <div className="agent-knowledge-card agent-knowledge-card--clickable" onClick={() => onOpenSubView('self')}>
        <h2 className="agent-knowledge-card-title">🪞 {t('agent.knowledge.selfCard')}</h2>
        <div className="agent-knowledge-card-body">
          <div>{t('agent.knowledge.selfEntries')}: {overview.planes.self.entries}</div>
          <div className="agent-knowledge-row-gap">
            {t('agent.knowledge.failureModes')}:{' '}
            <span className="agent-knowledge-fm-active">{fm.active} {t('agent.knowledge.active')}</span>
            {' · '}
            <span className="agent-knowledge-fm-mitigating">{fm.mitigating} {t('agent.knowledge.mitigating')}</span>
            {' · '}
            <span className="agent-knowledge-fm-resolved">{fm.resolved} {t('agent.knowledge.resolved')}</span>
          </div>
        </div>
      </div>
      <div className="agent-knowledge-card">
        <h2 className="agent-knowledge-card-title">🗺 {t('agent.knowledge.planesCard')}</h2>
        <div className="agent-knowledge-card-body">
          <div className="agent-knowledge-plane-link" onClick={() => onOpenSubView('profiles')}>
            👥 {t('agent.knowledge.profilesPlane')}: {overview.planes.profiles.entries}
          </div>
          <div className="agent-knowledge-plane-link" onClick={() => onOpenSubView('knowledge')}>
            📚 {t('agent.knowledge.knowledgePlane')}: {overview.planes.knowledge.pages}
          </div>
          <div className="agent-knowledge-plane-link" onClick={() => onOpenSubView('milestones')}>
            🏁 {t('agent.knowledge.milestonesPlane')}: {overview.planes.milestones.pages}
          </div>
          <div>📌 {t('agent.knowledge.explicitPlane')}: {overview.planes.explicit.active}</div>
        </div>
      </div>
      <div className="agent-knowledge-card">
        <h2 className="agent-knowledge-card-title">🧠 {t('agent.knowledge.memoryStatusCard', 'Memory status')}</h2>
        <div className="agent-knowledge-card-body">
          <div className={`agent-knowledge-memory-status ${statusClass}`}>
            {t(`agent.knowledge.memoryState.${memoryStatus.state}`, memoryStatus.state)}
          </div>
          <div className="agent-knowledge-memory-description">
            {t(`agent.knowledge.memoryStateDescription.${descriptionState}`, '')}
          </div>
          <div>
            {t('agent.knowledge.availableForRecall', 'Available for future conversations')}:{' '}
            {memoryStatus.availableForRecall
              ? t('agent.knowledge.available', 'Available')
              : t('agent.knowledge.notAvailableYet', 'Not available yet')}
          </div>
          <div>
            {t('agent.knowledge.longTermMemory', 'Long-term memory')}:{' '}
            {memoryStatus.longTermMemoryAvailable
              ? t('agent.knowledge.remembered', 'Remembered')
              : t('agent.knowledge.notConsolidatedYet', 'Not consolidated yet')}
          </div>
          {memoryStatus.pendingItems > 0 && (
            <div>
              {t('agent.knowledge.pendingMemoryItems', 'Organizing {{count}} recent experiences', {
                count: memoryStatus.pendingItems,
              })}
            </div>
          )}
          {memoryStatus.issueCount > 0 && (
            <>
              <div className="agent-knowledge-memory-issues">
                {t('agent.knowledge.memoryIssueItems', '{{count}} experiences awaiting recovery', {
                  count: memoryStatus.issueCount,
                })}
              </div>
              <div className="agent-knowledge-memory-recovery">
                {t('agent.knowledge.memoryRecoveryHint')}
              </div>
            </>
          )}
          {overview.growth?.generatedAt && (
            <div className="agent-knowledge-growth-updated">
              {t('agent.knowledge.growthUpdated')}{' '}
              {formatKnowledgeTimestamp(overview.growth.generatedAt, i18n.language)}
            </div>
          )}
        </div>
      </div>
      <div className="agent-knowledge-card">
        <h2 className="agent-knowledge-card-title">🔗 {t('agent.knowledge.capabilitiesCard')}</h2>
        <div className="agent-knowledge-card-body">
          <div>{t('agent.knowledge.skillsLinked', 'Skills')}: {overview.linkedCapabilities.skillsReferenced}</div>
          <div>{t('agent.knowledge.skillCandidates', 'Skill candidates')}: {overview.linkedCapabilities.skillCandidates}</div>
          <div>{t('agent.knowledge.workflowCandidates', 'Workflow candidates')}: {overview.linkedCapabilities.workflowsReferenced}</div>
        </div>
      </div>
    </div>
  );
}

function ProfileEntryCard({ entry }: { entry: KnowledgeEntry }) {
  const { t } = useTranslation();
  const status = entryStatusLine(entry);
  const statusStyle = FAILURE_STATUS_STYLE[status];
  return (
    <div className="agent-knowledge-card">
      <div className="agent-knowledge-entry-head">
        <strong className="agent-knowledge-entry-title">
          {knowledgeEntryHeading(entry, t('agent.knowledge.untitledEntry'))}
        </strong>
        {status && (
          <span className={`badge agent-knowledge-status-badge${statusStyle ? ` ${statusStyle.cls}` : ''}`}>
            {profileStatusLabel(status, t)}
          </span>
        )}
      </div>
      <div className="agent-knowledge-entry-preview">{entry.preview}</div>
    </div>
  );
}

function PlaneEntriesView({ entries, emptyText }: { entries: KnowledgeEntry[]; emptyText: string }) {
  if (!entries.length) {
    return <p className="agent-knowledge-empty">{emptyText}</p>;
  }
  return (
    <div className="agent-knowledge-entries-grid">
      {entries.map((entry) => (
        <ProfileEntryCard key={entry.id} entry={entry} />
      ))}
    </div>
  );
}

function PagesView({
  pages,
  selectedPageId,
  onSelect,
  pageDetail,
  emptyText,
}: {
  pages: KnowledgePageSummary[];
  selectedPageId: string | null;
  onSelect: (id: string) => void;
  pageDetail: ReturnType<typeof useQuery<any>>['data'];
  emptyText: string;
}) {
  const { t } = useTranslation();
  return (
    <div className={`agent-knowledge-pages${selectedPageId ? ' is-split' : ''}`}>
      <div className="agent-knowledge-pages-list">
        {pages.map((page) => (
          <button
            key={page.id}
            className={`btn btn-sm agent-knowledge-page-btn ${selectedPageId === page.id ? 'btn-primary' : ''}`}
            onClick={() => onSelect(page.id)}
          >
            {page.kind === 'knowledge' ? '📖' : '🏁'} {page.title}
          </button>
        ))}
        {pages.length === 0 && <p className="agent-knowledge-empty">{emptyText}</p>}
      </div>
      {selectedPageId && pageDetail && (
        <div className="agent-knowledge-page-detail">
          <div className="agent-knowledge-card">
            <MarkdownRenderer content={pageDetail.markdown} />
          </div>
          {(pageDetail.links?.outgoing?.length > 0 || pageDetail.links?.incoming?.length > 0) && (
            <div className="agent-knowledge-card">
              <h4 className="agent-knowledge-card-title">🔗 {t('agent.knowledge.linkedPages', 'Linked pages')}</h4>
              <div className="agent-knowledge-links">
                {[...(pageDetail.links?.outgoing ?? []), ...(pageDetail.links?.incoming ?? [])].map(
                  (link: { page_id: string; title: string; rel_type: string; exists: boolean }, index: number) =>
                    link.exists ? (
                      <button
                        key={`${link.page_id}-${index}`}
                        className="btn btn-sm"
                        onClick={() => onSelect(link.page_id)}
                      >
                        {link.rel_type} → {link.title}
                      </button>
                    ) : (
                      <span
                        key={`${link.page_id}-${index}`}
                        className="badge agent-knowledge-link-missing"
                        title={t('agent.knowledge.pageNotCreated', 'Page not created yet')}
                      >
                        {link.rel_type} → {link.title} ✦
                      </span>
                    ),
                )}
              </div>
            </div>
          )}
        </div>
      )}
    </div>
  );
}

type PersonalKnowledgeViewProps = {
  documents: PersonalKnowledgeDocumentSummary[];
  selectedDocument?: PersonalKnowledgeDocumentDetail;
  searchResults: PersonalKnowledgeSearchResult[];
  isLoading: boolean;
  error?: unknown;
  searchError?: unknown;
  documentError?: unknown;
  selectedDocumentId: string | null;
  searchQuery: string;
  onRetry: () => void;
  onSearchQueryChange: (value: string) => void;
  onRunSearch: () => void;
  onSelectDocument: (documentId: string) => void;
};

export function PersonalKnowledgeView({
  documents,
  selectedDocument,
  searchResults,
  isLoading,
  error,
  searchError,
  documentError,
  selectedDocumentId,
  searchQuery,
  onRetry,
  onSearchQueryChange,
  onRunSearch,
  onSelectDocument,
}: PersonalKnowledgeViewProps) {
  const { t } = useTranslation();
  if (error) {
    return (
      <div className="agent-knowledge-personal">
        <PersonalKnowledgeQueryState error={error} onRetry={onRetry} />
      </div>
    );
  }
  if (isLoading) {
    return (
      <div className="agent-knowledge-personal" data-testid="agent-personal-knowledge-loading">
        <p className="agent-knowledge-loading">{t('common.loading', 'Loading…')}</p>
      </div>
    );
  }
  return (
    <div className="agent-knowledge-personal">
      <div className="agent-knowledge-personal-toolbar">
        <form
          className="agent-knowledge-personal-search"
          onSubmit={(event) => {
            event.preventDefault();
            onRunSearch();
          }}
        >
          <input
            className="input agent-knowledge-personal-search-input"
            value={searchQuery}
            onChange={(event) => onSearchQueryChange(event.target.value)}
            placeholder={t('agent.knowledge.personalSearchPlaceholder', 'Search Personal KB...')}
          />
          <button className="btn btn-sm" type="submit">
            {t('agent.knowledge.personalSearch', 'Search')}
          </button>
        </form>
      </div>

      <div className="agent-knowledge-card agent-knowledge-personal-readonly">
        <strong>{t('agent.knowledge.personalReadonlyTitle', 'View only')}</strong>
        <span>
          {t(
            'agent.knowledge.personalReadonlyDesc',
            'Agent Detail can search and inspect Personal KB evidence, but ingestion and permission changes belong to the global Personal Knowledge workspace.',
          )}
        </span>
        <a className="btn btn-secondary btn-sm" href="/knowledge">
          {t('agent.knowledge.personalOpenGlobal', 'Open Personal KB')}
        </a>
      </div>

      <div className="agent-knowledge-personal-grid">
        <div className="agent-knowledge-personal-list">
          <h4 className="agent-knowledge-card-title">{t('agent.knowledge.personalLibrary', 'Personal KB')}</h4>
          {documents.map((document) => (
            <button
              key={document.document_id}
              className={`agent-knowledge-card agent-knowledge-personal-doc ${
                selectedDocumentId === document.document_id ? 'is-selected' : ''
              }`}
              onClick={() => onSelectDocument(document.document_id)}
              type="button"
            >
              <strong>{document.title}</strong>
              <span>
                {document.segment_count} {t('agent.knowledge.personalSegments', 'segments')} ·{' '}
                {personalSensitivityLabel(document.sensitivity, t)}
              </span>
            </button>
          ))}
          {documents.length === 0 && (
            <p className="agent-knowledge-empty">
              {t(
                'agent.knowledge.personalEmpty',
                'No personal knowledge is available here yet. Open Personal Knowledge to import material.',
              )}
            </p>
          )}
        </div>

        <div className="agent-knowledge-personal-detail">
          {searchError ? (
            <PersonalKnowledgeQueryState error={searchError} onRetry={onRetry} />
          ) : searchResults.length > 0 && (
            <div className="agent-knowledge-card">
              <h4 className="agent-knowledge-card-title">{t('agent.knowledge.personalSearchResults', 'Search results')}</h4>
              {searchResults.map((result) => (
                <div key={result.segment_id} className="agent-knowledge-personal-result">
                  <strong>{result.title}</strong>
                  <span>{result.heading_path.join(' / ')}</span>
                  <p>{result.snippet}</p>
                </div>
              ))}
            </div>
          )}

          {documentError ? (
            <PersonalKnowledgeQueryState error={documentError} onRetry={onRetry} />
          ) : selectedDocument ? (
            <div className="agent-knowledge-card">
              <div className="agent-knowledge-entry-head">
                <strong className="agent-knowledge-entry-title">{selectedDocument.title}</strong>
                <span className="badge">{personalStatusLabel(selectedDocument.status, t)}</span>
              </div>
              <div className="agent-knowledge-personal-segments">
                {selectedDocument.segments.map((segment) => (
                  <div key={segment.segment_id} className="agent-knowledge-personal-segment">
                    <div className="agent-knowledge-entry-meta">
                      {t('agent.knowledge.personalSegmentNumber', 'Section {{count}}', {
                        count: segment.position + 1,
                      })}
                      {segment.heading_path.length > 0 ? ` · ${segment.heading_path.join(' / ')}` : ''}
                    </div>
                    <p>{segment.content}</p>
                  </div>
                ))}
              </div>
            </div>
          ) : (
            <p className="agent-knowledge-empty">
              {t('agent.knowledge.personalSelectPrompt', 'Select a Personal KB document to inspect source segments.')}
            </p>
          )}
        </div>
      </div>
    </div>
  );
}

export default function AgentKnowledgeSection({ agentId, canEdit, onNavigateTab }: AgentKnowledgeSectionProps) {
  const { t, i18n } = useTranslation();
  const [subView, setSubView] = useState<SubView>('overview');
  const [selectedPageId, setSelectedPageId] = useState<string | null>(null);
  const [selectedPersonalDocumentId, setSelectedPersonalDocumentId] = useState<string | null>(null);
  const [personalSearchInput, setPersonalSearchInput] = useState('');
  const [personalSearchQuery, setPersonalSearchQuery] = useState('');
  const availableSubViews = canEdit ? SUBVIEWS : SUBVIEWS.filter((view) => view !== 'raw');

  useEffect(() => {
    if (!canEdit && subView === 'raw') {
      setSubView('overview');
    }
  }, [canEdit, subView]);

  useEffect(() => {
    setSelectedPageId(null);
  }, [subView]);

  const overviewQuery = useQuery({
    queryKey: ['knowledge-overview', agentId],
    queryFn: () => knowledgeApi.overview(agentId),
    enabled: subView === 'overview',
  });
  const entriesQuery = useQuery({
    queryKey: ['knowledge-entries', agentId],
    queryFn: () => knowledgeApi.entries(agentId),
    enabled: subView === 'self' || subView === 'profiles',
  });
  const pagesQuery = useQuery({
    queryKey: ['knowledge-pages', agentId],
    queryFn: () => knowledgeApi.pages(agentId),
    enabled: subView === 'knowledge' || subView === 'milestones',
  });
  const pageQuery = useQuery({
    queryKey: ['knowledge-page', agentId, selectedPageId],
    queryFn: () => knowledgeApi.page(agentId, selectedPageId as string),
    enabled: (subView === 'knowledge' || subView === 'milestones') && !!selectedPageId,
  });
  const eventsQuery = useQuery({
    queryKey: ['knowledge-events', agentId],
    queryFn: () => knowledgeApi.events(agentId),
    enabled: subView === 'timeline',
  });
  const personalDocumentsQuery = useQuery({
    queryKey: ['knowledge-personal-documents', agentId],
    queryFn: () => knowledgeApi.personalDocuments(agentId),
    enabled: subView === 'personal',
  });
  const personalSearchQueryResult = useQuery({
    queryKey: ['knowledge-personal-search', agentId, personalSearchQuery],
    queryFn: () => knowledgeApi.personalSearch(agentId, personalSearchQuery),
    enabled: subView === 'personal' && personalSearchQuery.trim().length > 0,
  });
  const personalDocumentQuery = useQuery({
    queryKey: ['knowledge-personal-document', agentId, selectedPersonalDocumentId],
    queryFn: () => knowledgeApi.personalDocument(agentId, selectedPersonalDocumentId as string),
    enabled: subView === 'personal' && !!selectedPersonalDocumentId,
  });

  const allEntries = entriesQuery.data?.entries ?? [];
  const selfEntries = allEntries.filter((entry) => entry.file.endsWith('self/self.md'));
  const profileEntries = allEntries.filter((entry) => entry.file.includes('memory/profiles/'));
  const allPages = pagesQuery.data?.pages ?? [];
  const knowledgePages = allPages.filter((page) => page.kind === 'knowledge');
  const milestonePages = allPages.filter((page) => page.kind === 'milestone');
  const personalDocuments = personalDocumentsQuery.data?.documents ?? [];
  const personalSearchResults = personalSearchQueryResult.data?.results ?? [];

  return (
    <div className="agent-knowledge-root">
      <div className="agent-knowledge-subviews">
        {availableSubViews.map((view) => (
          <button
            key={view}
            className={`btn btn-sm ${subView === view ? 'btn-primary' : ''}`}
            onClick={() => setSubView(view)}
          >
            {t(`agent.knowledge.subview.${view}`, view)}
          </button>
        ))}
      </div>

      {subView === 'overview' &&
        (overviewQuery.data ? (
          <OverviewCards
            overview={overviewQuery.data}
            canOpenIdentity={canEdit}
            onOpenSubView={setSubView}
            onNavigateTab={onNavigateTab}
          />
        ) : (
          <p className="agent-knowledge-loading">{t('common.loading', 'Loading…')}</p>
        ))}

      {subView === 'self' && (
        <PlaneEntriesView
          entries={selfEntries}
          emptyText={t(
            'agent.knowledge.noSelf',
          )}
        />
      )}

      {subView === 'profiles' && (
        <PlaneEntriesView
          entries={profileEntries}
          emptyText={t('agent.knowledge.noProfiles')}
        />
      )}

      {subView === 'personal' && (
        <PersonalKnowledgeView
          documents={personalDocuments}
          selectedDocument={personalDocumentQuery.data}
          searchResults={personalSearchResults}
          isLoading={personalDocumentsQuery.isLoading}
          error={personalDocumentsQuery.isError ? personalDocumentsQuery.error : undefined}
          searchError={personalSearchQueryResult.isError ? personalSearchQueryResult.error : undefined}
          documentError={personalDocumentQuery.isError ? personalDocumentQuery.error : undefined}
          selectedDocumentId={selectedPersonalDocumentId}
          searchQuery={personalSearchInput}
          onRetry={() => {
            void personalDocumentsQuery.refetch();
            if (personalSearchQuery.trim()) void personalSearchQueryResult.refetch();
            if (selectedPersonalDocumentId) void personalDocumentQuery.refetch();
          }}
          onSearchQueryChange={setPersonalSearchInput}
          onRunSearch={() => setPersonalSearchQuery(personalSearchInput.trim())}
          onSelectDocument={setSelectedPersonalDocumentId}
        />
      )}

      {subView === 'knowledge' && (
        <PagesView
          pages={knowledgePages}
          selectedPageId={selectedPageId}
          onSelect={setSelectedPageId}
          pageDetail={pageQuery.data}
          emptyText={t('agent.knowledge.noPages')}
        />
      )}

      {subView === 'milestones' && (
        <PagesView
          pages={milestonePages}
          selectedPageId={selectedPageId}
          onSelect={setSelectedPageId}
          pageDetail={pageQuery.data}
          emptyText={t('agent.knowledge.noMilestones')}
        />
      )}

      {subView === 'timeline' && (
        <div className="agent-knowledge-timeline">
          {(eventsQuery.data?.events ?? []).map((event, index) => (
            <div key={`${event.at}-${index}`} className="agent-knowledge-card agent-knowledge-timeline-row">
              <span className="agent-knowledge-timeline-time">
                {formatKnowledgeTimestamp(event.at, i18n.language)}
              </span>
              <span className="badge agent-knowledge-mr-2">{memoryEventKindLabel(event.kind, t)}</span>
              <span className="agent-knowledge-mr-2">{memoryEventOutcomeLabel(event.outcome, t)}</span>
              <span className="agent-knowledge-timeline-summary">{event.summary}</span>
            </div>
          ))}
          {eventsQuery.data && eventsQuery.data.events.length === 0 && (
            <p className="agent-knowledge-loading">{t('agent.knowledge.noEvents', 'No memory events recorded yet.')}</p>
          )}
        </div>
      )}

      {canEdit && subView === 'raw' && <AgentMindSection agentId={agentId} canEdit={canEdit} />}
    </div>
  );
}
