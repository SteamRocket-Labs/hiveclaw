import { renderToStaticMarkup } from 'react-dom/server';
import { describe, expect, it, vi } from 'vitest';

vi.mock('react-i18next', () => ({
  useTranslation: () => ({
    t: (_key: string, fallback?: string) => fallback ?? _key,
  }),
}));

import {
  AccessRulesView,
  IntakeQueueView,
  KnowledgeLifecycleView,
  OntologyStatusView,
  ReviewQueueView,
} from './CompanyKnowledgeControlPlane';

describe('Company Knowledge control-plane business projections', () => {
  it('renders recoverable intake status without paths, hashes, jobs, or proposal identifiers', () => {
    const markup = renderToStaticMarkup(
      <IntakeQueueView
        intakes={[
          {
            intakeKey: 'job-secret-id',
            reviewKey: 'proposal-secret-id',
            kind: 'legacy',
            title: 'Onboarding playbook',
            sourceLabel: 'onboarding.md',
            area: 'playbooks',
            sensitivity: 'restricted',
            status: 'retry_required',
            recovery: 'manual',
            attemptCount: 5,
            reviewStatus: null,
            createdAt: '2026-07-24T00:00:00Z',
            updatedAt: '2026-07-24T00:00:00Z',
          },
        ]}
        retryingKey={null}
        onRetry={vi.fn()}
      />,
    );

    expect(markup).toContain('Onboarding playbook');
    expect(markup).toContain('Needs your attention');
    expect(markup).toContain('Retry');
    for (const forbidden of [
      'job-secret-id',
      'proposal-secret-id',
      'retired/team/private',
      'sha256',
      'proposal_id',
      'attempt_count',
    ]) {
      expect(markup).not.toContain(forbidden);
    }
  });

  it('renders review, access, lifecycle, and ontology states in business language only', () => {
    const reviewMarkup = renderToStaticMarkup(
      <ReviewQueueView
        reviews={[
          {
            reviewKey: 'proposal-secret-id',
            title: 'Employee Handbook',
            status: 'submitted',
            kind: 'personal',
            area: 'policies',
            sensitivity: 'personal_data',
            risk: 'normal',
            reason: 'Owner submitted a reviewed policy.',
            createdBy: 'company_member',
            stateVersion: 2,
            needsMaterialization: true,
            materialized: false,
            updatedAt: '2026-07-24T00:00:00Z',
          },
        ]}
        selectedKey={null}
        onSelect={vi.fn()}
      />,
    );
    const accessMarkup = renderToStaticMarkup(
      <AccessRulesView
        rules={[
          {
            permissionKey: 'permission-secret-id',
            audience: 'All employees',
            resource: 'All Company Knowledge',
            capabilities: ['find_and_read'],
            effect: 'allow',
            sensitivity: 'personal_data',
            active: true,
            expiresAt: null,
          },
        ]}
        revokingKey={null}
        actionReady
        onRevoke={vi.fn()}
      />,
    );
    const lifecycleMarkup = renderToStaticMarkup(
      <KnowledgeLifecycleView
        publications={[
          {
            publicationKey: 'publication-secret-id',
            documentKey: 'document-secret-id',
            title: 'Employee Handbook',
            status: 'retired',
            version: 3,
            area: 'policies',
            sensitivity: 'personal_data',
            validFrom: '2026-07-20T00:00:00Z',
            validUntil: '2026-07-24T00:00:00Z',
            availableAction: 'restore',
          },
        ]}
        busyKey={null}
        actionReady
        onLifecycleAction={vi.fn()}
      />,
    );
    const ontologyMarkup = renderToStaticMarkup(
      <OntologyStatusView
        status={{
          engineStatus: 'available',
          installedPacks: [{ name: 'Operations Pack', version: '1.2.0', status: 'active' }],
          releases: [{ area: 'operations', version: 2, status: 'active' }],
        }}
      />,
    );
    const markup = `${reviewMarkup}${accessMarkup}${lifecycleMarkup}${ontologyMarkup}`;

    expect(markup).toContain('Employee Handbook');
    expect(markup).toContain('Ready for review');
    expect(markup).toContain('All employees');
    expect(markup).toContain('Find and read');
    expect(markup).toContain('Restore');
    expect(markup).toContain('Operations Pack');
    expect(markup).toContain('Available');
    for (const forbidden of [
      'proposal-secret-id',
      'permission-secret-id',
      'publication-secret-id',
      'document-secret-id',
      'PL2_pii',
      'state_version',
      'principal',
      'content_hash',
      'artifact_ref',
    ]) {
      expect(markup).not.toContain(forbidden);
    }
  });

  it('keeps permission and lifecycle effects disabled until an operator records a reason', () => {
    const accessMarkup = renderToStaticMarkup(
      <AccessRulesView
        rules={[
          {
            permissionKey: 'permission-secret-id',
            audience: 'All employees',
            resource: 'All Company Knowledge',
            capabilities: ['find_and_read'],
            effect: 'allow',
            sensitivity: 'company',
            active: true,
            expiresAt: null,
          },
        ]}
        revokingKey={null}
        actionReady={false}
        onRevoke={vi.fn()}
      />,
    );
    const lifecycleMarkup = renderToStaticMarkup(
      <KnowledgeLifecycleView
        publications={[
          {
            publicationKey: 'publication-secret-id',
            documentKey: 'document-secret-id',
            title: 'Employee Handbook',
            status: 'active',
            version: 3,
            area: 'policies',
            sensitivity: 'company',
            validFrom: '2026-07-20T00:00:00Z',
            validUntil: null,
            availableAction: 'retire',
          },
        ]}
        busyKey={null}
        actionReady={false}
        onLifecycleAction={vi.fn()}
      />,
    );

    expect(accessMarkup).toContain('disabled=""');
    expect(lifecycleMarkup).toContain('disabled=""');
  });
});
