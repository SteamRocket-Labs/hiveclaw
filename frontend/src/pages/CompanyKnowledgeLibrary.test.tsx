import { renderToStaticMarkup } from 'react-dom/server';
import { describe, expect, it, vi } from 'vitest';

vi.mock('react-i18next', () => ({
  useTranslation: () => ({
    t: (_key: string, fallback?: string, options?: Record<string, unknown>) =>
      (fallback ?? _key).replace(/\{\{(\w+)\}\}/g, (match, name) =>
        options && name in options ? String(options[name]) : match,
      ),
  }),
}));

vi.mock('react-router-dom', () => ({
  Link: ({ to, children, className }: any) => (
    <a href={to} className={className}>
      {children}
    </a>
  ),
}));

vi.mock('../stores', () => ({
  useAuthStore: () => ({ role: 'org_admin' }),
}));

import { CompanyKnowledgeLibraryView, resolveLibrarySelection } from './CompanyKnowledgeLibrary';

describe('CompanyKnowledgeLibraryView', () => {
  it('shows only authorized business content and keeps internal references out of the DOM', () => {
    const markup = renderToStaticMarkup(
      <CompanyKnowledgeLibraryView
        documents={[
          {
            publicationKey: 'publication-secret-id',
            documentKey: 'document-secret-id',
            title: 'Employee Handbook',
            area: 'policies',
            sensitivity: 'personal_data',
            version: 4,
            validFrom: '2026-07-24T00:00:00Z',
            validUntil: null,
          },
        ]}
        selectedDocument={{
          publicationKey: 'publication-secret-id',
          documentKey: 'document-secret-id',
          title: 'Employee Handbook',
          area: 'policies',
          sensitivity: 'personal_data',
          version: 4,
          content: '# Leave\n\nEmployees receive 22 days of annual leave.',
          truncated: false,
        }}
        query=""
        isLoading={false}
        isReading={false}
        error={null}
        onQueryChange={vi.fn()}
        onSearch={vi.fn()}
        onSelect={vi.fn()}
        onRetry={vi.fn()}
        canManage
        selectedResultKey="publication-secret-id:document-secret-id"
      />,
    );

    expect(markup).toContain('Company Knowledge');
    expect(markup).toContain('Employee Handbook');
    expect(markup).toContain('Employees receive 22 days of annual leave.');
    expect(markup).toContain('href="/knowledge"');
    expect(markup).toContain('href="/enterprise/knowledge"');
    for (const forbidden of [
      'publication-secret-id',
      'document-secret-id',
      'company-publication://',
      'source_ref',
      'content_hash',
      'PL2_pii',
      'proposal_id',
      'job_id',
      'principal_id',
    ]) {
      expect(markup).not.toContain(forbidden);
    }
  });

  it('uses a permission-safe empty state that does not claim the company has no knowledge', () => {
    const markup = renderToStaticMarkup(
      <CompanyKnowledgeLibraryView
        documents={[]}
        selectedDocument={null}
        query=""
        isLoading={false}
        isReading={false}
        error={null}
        onQueryChange={vi.fn()}
        onSearch={vi.fn()}
        onSelect={vi.fn()}
        onRetry={vi.fn()}
        canManage={false}
        selectedResultKey={null}
      />,
    );

    expect(markup).toContain('No Company Knowledge is available to your account');
    expect(markup).toContain('Your access may be limited');
    expect(markup).not.toContain('The company has no knowledge');
    expect(markup).not.toContain('/enterprise/knowledge');
  });
});

// ---------------------------------------------------------------------------
// RC-02C / CKB-SEARCH-001: segment-level search hit identity + safe cues
// ---------------------------------------------------------------------------

describe('CompanyKnowledgeLibraryView — segment search hits (RC-02C)', () => {
  const baseDocument = {
    publicationKey: 'publication-secret-id',
    documentKey: 'document-secret-id',
    title: 'Weekend Runbook',
    area: 'general',
    sensitivity: 'company' as const,
    version: 1,
    validFrom: '2026-08-26T00:00:00Z',
    validUntil: null,
  };

  it('distinguishes repeated segment hits of one document with localized passage cues and safe snippets, and activates exactly one segment card', () => {
    const markup = renderToStaticMarkup(
      <CompanyKnowledgeLibraryView
        documents={[
          { ...baseDocument, snippet: 'Alpha passage snippet with the marker.', segmentKey: 'segment-secret-1' },
          { ...baseDocument, snippet: 'Beta passage snippet with the marker.', segmentKey: 'segment-secret-2' },
        ]}
        selectedDocument={null}
        query="marker"
        isLoading={false}
        isReading={false}
        error={null}
        onQueryChange={vi.fn()}
        onSearch={vi.fn()}
        onSelect={vi.fn()}
        onRetry={vi.fn()}
        canManage={false}
        selectedResultKey="publication-secret-id:document-secret-id:segment-secret-2"
      />,
    );

    // Both hits are visibly and accessibly distinguishable even though the
    // document title/area/sensitivity are identical.
    expect(markup).toContain('Matching passage 1');
    expect(markup).toContain('Matching passage 2');
    expect(markup).toContain('Alpha passage snippet with the marker.');
    expect(markup).toContain('Beta passage snippet with the marker.');

    // Exactly one active card, and it is the selected segment identity
    // (passage 2), not every card of the same publication.
    const cards = markup.split('<button').slice(1);
    const activeCards = cards.filter((card) => card.includes('company-library-card active'));
    expect(activeCards).toHaveLength(1);
    expect(activeCards[0]).toContain('Matching passage 2');
    expect(activeCards[0]).toContain('Beta passage snippet with the marker.');

    // Privacy contract: no raw segment identity, source refs, or forensic
    // fields in the rendered DOM.
    for (const forbidden of [
      'segment-secret-1',
      'segment-secret-2',
      'segment_id',
      'source_ref',
      'company-publication://',
      'score_trace',
      'content_hash',
    ]) {
      expect(markup).not.toContain(forbidden);
    }
  });

  it('keeps normal unsearched library cards free of search-only passage cues and snippets', () => {
    const markup = renderToStaticMarkup(
      <CompanyKnowledgeLibraryView
        documents={[baseDocument]}
        selectedDocument={null}
        query=""
        isLoading={false}
        isReading={false}
        error={null}
        onQueryChange={vi.fn()}
        onSearch={vi.fn()}
        onSelect={vi.fn()}
        onRetry={vi.fn()}
        canManage={false}
        selectedResultKey={null}
      />,
    );

    expect(markup).toContain('Weekend Runbook');
    expect(markup).not.toContain('Matching passage');
    expect(markup).not.toContain('company-library-snippet');
  });
});

describe('resolveLibrarySelection (RC-02C)', () => {
  const baseDocument = {
    publicationKey: 'publication-1',
    documentKey: 'document-1',
    title: 'Runbook',
    area: 'general',
    sensitivity: 'company' as const,
    version: 1,
    validFrom: '',
    validUntil: null,
  };

  it('resets selection by full result identity, not by publication only', () => {
    const seg1 = { ...baseDocument, snippet: 'Alpha', segmentKey: 'segment-1' };
    const seg2 = { ...baseDocument, snippet: 'Beta', segmentKey: 'segment-2' };
    const seg3 = { ...baseDocument, snippet: 'Gamma', segmentKey: 'segment-3' };

    // The selected segment remains active while it is in the current results.
    expect(resolveLibrarySelection([seg1, seg2], seg2)).toBe(seg2);

    // A new result set for the SAME publication/document but without the
    // selected segment must not retain the stale segment selection.
    expect(resolveLibrarySelection([seg3], seg2)).toBe(seg3);
    expect(resolveLibrarySelection([], seg2)).toBeNull();

    // List mode (no segment identity) keeps publication:document semantics.
    expect(resolveLibrarySelection([baseDocument], baseDocument)).toBe(baseDocument);
    expect(resolveLibrarySelection([baseDocument], null)).toBe(baseDocument);
  });
});
