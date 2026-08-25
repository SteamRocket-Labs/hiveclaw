import { renderToStaticMarkup } from 'react-dom/server';
import { describe, expect, it, vi } from 'vitest';

import enCatalog from '../i18n/en.json';
import zhCatalog from '../i18n/zh.json';

vi.mock('react-i18next', () => ({
  useTranslation: () => ({
    t: (_key: string, fallback?: string) => fallback ?? _key,
  }),
}));

import {
  AccessRulesView,
  DirectImportWizard,
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

// ---------------------------------------------------------------------------
// RC-02: direct import wizard surface (failing-first)
// ---------------------------------------------------------------------------

const baseJob = {
  jobKey: 'job-1',
  status: 'queued',
  lifecycleStatus: 'queued' as string,
  attemptCount: 0,
  maxAttempts: 5,
  terminal: false,
  retryable: false,
  cancellable: true,
  errorCode: null as string | null,
  title: 'Runbook',
  sourceFilename: 'runbook.pdf',
  namespace: 'company/general',
  sensitivity: 'internal',
  documentKey: null as string | null,
  proposalKey: null as string | null,
  cancelledAt: null as string | null,
};

const wizardProps = {
  contracts: [
    {
      contractKey: 'contract-1',
      stableSourceId: 'company-file-upload',
      status: 'active',
      version: 1,
      allowedNamespaces: ['company/general'],
      defaultSensitivity: 'PL1_public',
    },
  ],
  jobs: [] as Array<typeof baseJob>,
  uploading: false,
  actionError: null as string | null,
  busyJobKey: null as string | null,
  previewJobKey: null as string | null,
  preview: null as import('../api/domains/companyKnowledge').CompanyImportPreview | null,
  onUpload: () => {},
  onSelectContract: () => {},
  onCreateContract: () => {},
  onRetryJob: () => {},
  onCancelJob: () => {},
  onPreview: () => {},
  onCreateProposal: () => {},
};

describe('Company Knowledge direct import wizard', () => {
  it('renders only the vertically proven formats and a contract selector', () => {
    const markup = renderToStaticMarkup(<DirectImportWizard {...wizardProps} />);

    expect(markup).toContain('PDF');
    expect(markup).toContain('Word / DOCX');
    expect(markup).toContain('Markdown');
    for (const forbidden of ['csv', 'html', 'audio', 'video', 'image', 'xlsx', 'pptx']) {
      expect(markup.toLowerCase()).not.toContain(forbidden);
    }
    expect(markup).toContain('company-file-upload');
    expect(markup).toContain('type="file"');
    // The picker only offers the formats the intake actually accepts.
    expect(markup).toContain('accept=".pdf,.docx,.md,.markdown,.txt"');
  });

  it('shows lifecycle labels with cancel for queued jobs and no retry', () => {
    const markup = renderToStaticMarkup(<DirectImportWizard {...wizardProps} jobs={[baseJob]} />);

    expect(markup).toContain('Runbook');
    expect(markup).toContain('runbook.pdf');
    expect(markup).toContain('Queued');
    expect(markup).toContain('>Cancel<');
    expect(markup).not.toContain('>Retry<');
    expect(markup).not.toContain('>queued<');
  });

  it('shows retry for a failed retryable job with a localized typed error', () => {
    const markup = renderToStaticMarkup(
      <DirectImportWizard
        {...wizardProps}
        jobs={[{ ...baseJob, status: 'failed', lifecycleStatus: 'failed', terminal: true, retryable: true, cancellable: false, attemptCount: 1, errorCode: 'conversion_timeout' }]}
      />,
    );

    expect(markup).toContain('Failed');
    expect(markup).toContain('Conversion timed out; you can retry.');
    expect(markup).toContain('>Retry<');
    expect(markup).not.toContain('conversion_timeout');
  });

  it('maps the title conflict to localized prose without retry or raw code', () => {
    const markup = renderToStaticMarkup(
      <DirectImportWizard
        {...wizardProps}
        jobs={[{ ...baseJob, status: 'failed', lifecycleStatus: 'failed', terminal: true, retryable: false, cancellable: false, attemptCount: 1, errorCode: 'company_knowledge_import_title_conflict' }]}
      />,
    );

    expect(markup).toContain('This file matches an existing document but uses a different title.');
    expect(markup).not.toContain('company_knowledge_import_title_conflict');
    expect(markup).not.toContain('>Retry<');
  });

  it('maps unknown lifecycle and error codes to one neutral label without leaking raw codes', () => {
    const markup = renderToStaticMarkup(
      <DirectImportWizard
        {...wizardProps}
        jobs={[{ ...baseJob, status: 'mystery_state', lifecycleStatus: 'mystery_state', terminal: true, cancellable: false, errorCode: 'totally_unknown_code' }]}
      />,
    );

    expect(markup).toContain('Status unavailable');
    expect(markup).toContain('Import failed with an unspecified error.');
    expect(markup).not.toContain('mystery_state');
    expect(markup).not.toContain('totally_unknown_code');
  });

  it('offers preview and create proposal only for completed jobs, and shows the linked proposal state', () => {
    const completed = {
      ...baseJob,
      status: 'completed',
      lifecycleStatus: 'completed',
      terminal: true,
      cancellable: false,
      documentKey: 'doc-1',
    };
    const withProposal = { ...completed, proposalKey: 'proposal-1' };
    const markup = renderToStaticMarkup(<DirectImportWizard {...wizardProps} jobs={[completed, { ...withProposal, jobKey: 'job-2' }]} />);

    expect(markup).toContain('Preview');
    expect(markup).toContain('Create proposal');
    expect(markup).toContain('Submitted for review');
  });

  it('renders preview segments and action errors as alerts without raw codes', () => {
    const markup = renderToStaticMarkup(
      <DirectImportWizard
        {...wizardProps}
        jobs={[{ ...baseJob, status: 'completed', lifecycleStatus: 'completed', terminal: true, cancellable: false, documentKey: 'doc-1' }]}
        previewJobKey="job-1"
        preview={{
          jobKey: 'job-1',
          documentKey: 'doc-1',
          evidenceKey: 'ev-1',
          sourceKey: 'src-1',
          proposalKey: null,
          title: 'Runbook',
          namespace: 'company/general',
          sensitivity: 'internal',
          segments: [{ segmentKey: 'seg-1', position: 0, headingPath: ['Runbook', 'Table'], content: 'marker content', tokenCount: 4 }],
        }}
        actionError="retry_attempt_limit"
      />,
    );

    expect(markup).toContain('marker content');
    expect(markup).toContain('Runbook / Table');
    expect(markup).toContain('role="alert"');
    expect(markup).toContain('Retry limit reached.');
    expect(markup).not.toContain('retry_attempt_limit');
  });
});

describe('Company Knowledge direct import error catalog', () => {
  it('localizes the title-conflict code with bounded prose in both locales', () => {
    expect(enCatalog.companyKnowledge.directImport.errorTitleConflict).toBe(
      'This file matches an existing document but uses a different title.',
    );
    expect(zhCatalog.companyKnowledge.directImport.errorTitleConflict).toBe(
      '该文件内容与现有文档相同，但标题不同。',
    );
  });
});
