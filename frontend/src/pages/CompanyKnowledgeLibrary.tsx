import { useEffect, useMemo, useState, type FormEvent } from 'react';
import { useQuery } from '@tanstack/react-query';
import { Link } from 'react-router-dom';
import { useTranslation } from 'react-i18next';
import {
  IconArrowRight,
  IconBuilding,
  IconDatabase,
  IconRefresh,
  IconSearch,
  IconUser,
} from '@tabler/icons-react';

import {
  companyKnowledgeApi,
  type CompanyKnowledgeSensitivity,
  type CompanyLibraryDocument,
  type CompanyLibraryDocumentDetail,
  type CompanyLibrarySearchHit,
} from '../api/domains/companyKnowledge';
import MarkdownRenderer from '../components/MarkdownRenderer';
import { isAdministratorRole } from '../roles';
import { useAuthStore } from '../stores';
import './CompanyKnowledgeLibrary.css';

function areaLabel(value: string, t: ReturnType<typeof useTranslation>['t']): string {
  if (value === 'policies') return t('companyKnowledge.areas.policies', 'Policies');
  if (value === 'team_notes') return t('companyKnowledge.areas.teamNotes', 'Team notes');
  if (value === 'playbooks') return t('companyKnowledge.areas.playbooks', 'Playbooks');
  if (value === 'operations') return t('companyKnowledge.areas.operations', 'Operations');
  if (value === 'general') return t('companyKnowledge.areas.general', 'General');
  return value.replaceAll('_', ' ');
}

function sensitivityLabel(
  value: CompanyKnowledgeSensitivity,
  t: ReturnType<typeof useTranslation>['t'],
): string {
  if (value === 'personal_data') {
    return t('companyKnowledge.sensitivity.personalData', 'Contains personal data');
  }
  if (value === 'restricted') return t('companyKnowledge.sensitivity.restricted', 'Restricted');
  if (value === 'credential') return t('companyKnowledge.sensitivity.credential', 'Credential-protected');
  return t('companyKnowledge.sensitivity.company', 'Company-wide');
}

type LibraryResult = CompanyLibraryDocument | CompanyLibrarySearchHit;

// Unique identity for one result row. Search hits are segment-level (RC-02C):
// repeated hits of the same document differ only by their internal segmentKey.
// Used for React keys and selection identity only — never rendered into the
// DOM. List-mode documents keep publication:document identity. Malformed
// legacy responses without a segment identity safely fall back to the
// document identity.
export function libraryResultKey(document: LibraryResult): string {
  const segmentKey = (document as CompanyLibrarySearchHit).segmentKey;
  const base = `${document.publicationKey}:${document.documentKey}`;
  return segmentKey ? `${base}:${segmentKey}` : base;
}

function searchHitOf(document: LibraryResult): CompanyLibrarySearchHit | null {
  const hit = document as CompanyLibrarySearchHit;
  return typeof hit.segmentKey === 'string' && hit.segmentKey.length > 0 ? hit : null;
}

// Deterministic selection: keep the current selection only while its full
// result identity is present in the current result set; otherwise reset to
// the first current result. A stale segment of the same publication is never
// retained (RC-02C).
export function resolveLibrarySelection(
  documents: LibraryResult[],
  selected: LibraryResult | null,
): LibraryResult | null {
  if (!documents.length) return null;
  if (selected) {
    const selectedKey = libraryResultKey(selected);
    if (documents.some((item) => libraryResultKey(item) === selectedKey)) return selected;
  }
  return documents[0];
}

interface CompanyKnowledgeLibraryViewProps {
  documents: LibraryResult[];
  selectedDocument: CompanyLibraryDocumentDetail | null;
  selectedResultKey: string | null;
  query: string;
  isLoading: boolean;
  isReading: boolean;
  error: unknown;
  onQueryChange: (value: string) => void;
  onSearch: () => void;
  onSelect: (document: LibraryResult) => void;
  onRetry: () => void;
  canManage: boolean;
}

export function CompanyKnowledgeLibraryView({
  documents,
  selectedDocument,
  selectedResultKey,
  query,
  isLoading,
  isReading,
  error,
  onQueryChange,
  onSearch,
  onSelect,
  onRetry,
  canManage,
}: CompanyKnowledgeLibraryViewProps) {
  const { t } = useTranslation();

  return (
    <div className="company-library-page">
      <header className="company-library-hero">
        <div>
          <span className="workbench-eyebrow">
            {t('companyKnowledge.libraryEyebrow', 'Authorized company library')}
          </span>
          <h1>{t('companyKnowledge.libraryTitle', 'Company Knowledge')}</h1>
          <p>
            {t(
              'companyKnowledge.librarySubtitle',
              'Search and read the current company knowledge your account is allowed to use.',
            )}
          </p>
        </div>
        <IconBuilding size={24} stroke={1.6} />
      </header>

      <div className="company-library-links">
        <Link to="/knowledge" className="btn btn-secondary btn-sm">
          <IconUser size={14} stroke={1.7} />
          {t('companyKnowledge.backToPersonal', 'Personal Knowledge')}
        </Link>
        {canManage && (
          <Link to="/enterprise/knowledge" className="btn btn-secondary btn-sm">
            <IconDatabase size={14} stroke={1.7} />
            {t('companyKnowledge.openControlPlane', 'Manage Company Knowledge')}
          </Link>
        )}
      </div>

      <form
        className="company-library-search"
        onSubmit={(event: FormEvent) => {
          event.preventDefault();
          onSearch();
        }}
      >
        <IconSearch size={17} stroke={1.7} />
        <input
          value={query}
          onChange={(event) => onQueryChange(event.target.value)}
          placeholder={t('companyKnowledge.searchPlaceholder', 'Search policies, playbooks, and company guidance')}
          aria-label={t('companyKnowledge.searchLabel', 'Search Company Knowledge')}
        />
        <button type="submit" className="btn btn-primary btn-sm" disabled={!query.trim()}>
          {t('common.search', 'Search')}
        </button>
      </form>

      {error ? (
        <section className="company-library-state" role="alert">
          <h2>{t('companyKnowledge.unavailableTitle', 'Company Knowledge is temporarily unavailable')}</h2>
          <p>
            {t(
              'companyKnowledge.unavailableDescription',
              'No empty-library conclusion was made. Retry to restore the authorized result.',
            )}
          </p>
          <button type="button" className="btn btn-secondary btn-sm" onClick={onRetry}>
            <IconRefresh size={14} stroke={1.7} />
            {t('common.retry', 'Retry')}
          </button>
        </section>
      ) : isLoading ? (
        <section className="company-library-state">
          {t('companyKnowledge.loading', 'Loading authorized Company Knowledge...')}
        </section>
      ) : documents.length === 0 ? (
        <section className="company-library-state">
          <h2>
            {t(
              'companyKnowledge.safeEmptyTitle',
              'No Company Knowledge is available to your account',
            )}
          </h2>
          <p>
            {t(
              'companyKnowledge.safeEmptyDescription',
              'Your access may be limited, or no current publication matched this search.',
            )}
          </p>
        </section>
      ) : (
        <div className="company-library-shell">
          <section className="company-library-list" aria-label={t('companyKnowledge.results', 'Results')}>
            {documents.map((document, index) => {
              const hit = searchHitOf(document);
              return (
                <button
                  type="button"
                  key={libraryResultKey(document)}
                  className={
                    selectedResultKey !== null && selectedResultKey === libraryResultKey(document)
                      ? 'company-library-card active'
                      : 'company-library-card'
                  }
                  onClick={() => onSelect(document)}
                >
                  <span>
                    <strong>{document.title}</strong>
                    <small>
                      {areaLabel(document.area, t)} · {sensitivityLabel(document.sensitivity, t)}
                    </small>
                    {hit && (
                      <small className="company-library-passage">
                        {t('companyKnowledge.matchingPassage', 'Matching passage {{index}}', {
                          index: index + 1,
                        })}
                      </small>
                    )}
                    {hit?.snippet ? (
                      <small className="company-library-snippet">{hit.snippet}</small>
                    ) : null}
                  </span>
                  <IconArrowRight size={15} stroke={1.7} />
                </button>
              );
            })}
          </section>

          <article className="company-library-reader">
            {isReading ? (
              <div className="company-library-state">
                {t('companyKnowledge.reading', 'Opening the authorized publication...')}
              </div>
            ) : selectedDocument ? (
              <>
                <div className="company-library-reader-head">
                  <div>
                    <span>{areaLabel(selectedDocument.area, t)}</span>
                    <h2>{selectedDocument.title}</h2>
                  </div>
                  <span className="ui-chip">
                    {t('companyKnowledge.publishedVersion', 'Published version')} {selectedDocument.version}
                  </span>
                </div>
                <MarkdownRenderer content={selectedDocument.content} />
                {selectedDocument.truncated && (
                  <small className="company-library-truncated">
                    {t(
                      'companyKnowledge.truncated',
                      'This view reached its reading limit. Refine the search to open the relevant section.',
                    )}
                  </small>
                )}
              </>
            ) : (
              <div className="company-library-state">
                {t('companyKnowledge.selectDocument', 'Select an item to read it.')}
              </div>
            )}
          </article>
        </div>
      )}
    </div>
  );
}

export default function CompanyKnowledgeLibrary() {
  const user = useAuthStore((state) => state.user);
  const [query, setQuery] = useState('');
  const [activeQuery, setActiveQuery] = useState('');
  const [selected, setSelected] = useState<LibraryResult | null>(null);
  const libraryQuery = useQuery({
    queryKey: ['company-knowledge-library'],
    queryFn: () => companyKnowledgeApi.listLibrary(),
  });
  const searchQuery = useQuery({
    queryKey: ['company-knowledge-search', activeQuery],
    queryFn: () => companyKnowledgeApi.searchLibrary(activeQuery),
    enabled: activeQuery.length > 0,
  });
  const documents = useMemo(
    () => (activeQuery ? searchQuery.data?.results ?? [] : libraryQuery.data?.documents ?? []),
    [activeQuery, libraryQuery.data?.documents, searchQuery.data?.results],
  );

  useEffect(() => {
    const next = resolveLibrarySelection(documents, selected);
    if (next !== selected) setSelected(next);
  }, [documents, selected]);

  const readQuery = useQuery({
    queryKey: ['company-knowledge-read', selected?.publicationKey, selected?.documentKey],
    queryFn: () =>
      companyKnowledgeApi.readLibrary(
        selected?.documentKey ?? '',
        selected?.publicationKey ?? '',
      ),
    enabled: Boolean(selected),
  });
  const activeListQuery = activeQuery ? searchQuery : libraryQuery;

  return (
    <CompanyKnowledgeLibraryView
      documents={documents}
      selectedDocument={readQuery.data ?? null}
      selectedResultKey={selected ? libraryResultKey(selected) : null}
      query={query}
      isLoading={activeListQuery.isLoading}
      isReading={readQuery.isLoading}
      error={activeListQuery.isError ? activeListQuery.error : readQuery.isError ? readQuery.error : null}
      onQueryChange={setQuery}
      onSearch={() => setActiveQuery(query.trim())}
      onSelect={setSelected}
      onRetry={() => {
        void activeListQuery.refetch();
        if (selected) void readQuery.refetch();
      }}
      canManage={isAdministratorRole(user?.role)}
    />
  );
}
