import { renderToStaticMarkup } from 'react-dom/server';
import { describe, expect, it, vi } from 'vitest';

vi.mock('react-i18next', () => ({
  useTranslation: () => ({
    t: (_key: string, fallback?: string) => fallback ?? _key,
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

import { CompanyKnowledgeLibraryView } from './CompanyKnowledgeLibrary';

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
      />,
    );

    expect(markup).toContain('No Company Knowledge is available to your account');
    expect(markup).toContain('Your access may be limited');
    expect(markup).not.toContain('The company has no knowledge');
    expect(markup).not.toContain('/enterprise/knowledge');
  });
});
