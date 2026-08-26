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
  type DirectImportBoundaryState,
  type DirectImportPreviewState,
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
  contractsState: 'ready' as DirectImportBoundaryState,
  jobsState: 'ready' as DirectImportBoundaryState,
  previewState: 'idle' as DirectImportPreviewState,
  onUpload: () => {},
  onSelectContract: () => {},
  onCreateContract: () => {},
  onRetryJob: () => {},
  onCancelJob: () => {},
  onPreview: () => {},
  onCreateProposal: () => {},
  onRetryContracts: () => {},
  onRetryJobs: () => {},
  onRetryPreview: () => {},
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

  it('shows a scoped loading state for contracts without selector or create form', () => {
    const markup = renderToStaticMarkup(
      <DirectImportWizard {...wizardProps} contractsState='loading' />,
    );

    expect(markup).toContain('Loading source contracts…');
    expect(markup).not.toContain('<select');
    expect(markup).not.toContain('Create source contract');
  });

  it('treats a contracts error as unavailable even with stale data, with retry', () => {
    const markup = renderToStaticMarkup(
      <DirectImportWizard {...wizardProps} contractsState='error' />,
    );

    expect(markup).toContain('Source contracts are temporarily unavailable');
    expect(markup).toContain('No empty-state conclusion was made and no action was taken.');
    expect(markup).toContain('role="alert"');
    expect(markup).toContain('>Retry<');
    expect(markup).not.toContain('<select');
    expect(markup).not.toContain('Create source contract');
  });

  it('keeps the create-contract empty form only for ready contracts', () => {
    const markup = renderToStaticMarkup(
      <DirectImportWizard {...wizardProps} contracts={[]} contractsState='ready' />,
    );

    expect(markup).toContain('Create source contract');
    expect(markup).not.toContain('Loading source contracts…');
    expect(markup).not.toContain('Source contracts are temporarily unavailable');
  });

  it('shows jobs loading without the empty conclusion or stale rows', () => {
    const markup = renderToStaticMarkup(
      <DirectImportWizard {...wizardProps} jobs={[baseJob]} jobsState='loading' />,
    );

    expect(markup).toContain('Loading import jobs…');
    expect(markup).not.toContain('No import jobs yet.');
    expect(markup).not.toContain('Runbook');
  });

  it('treats a jobs error as unavailable even with stale rows, with retry', () => {
    const markup = renderToStaticMarkup(
      <DirectImportWizard {...wizardProps} jobs={[baseJob]} jobsState='error' />,
    );

    expect(markup).toContain('Import jobs are temporarily unavailable');
    expect(markup).toContain('No empty-state conclusion was made and no action was taken.');
    expect(markup).toContain('role="alert"');
    expect(markup).toContain('>Retry<');
    expect(markup).not.toContain('No import jobs yet.');
    expect(markup).not.toContain('Runbook');
  });

  it('keeps the jobs empty conclusion only for ready jobs', () => {
    const markup = renderToStaticMarkup(
      <DirectImportWizard {...wizardProps} jobs={[]} jobsState='ready' />,
    );

    expect(markup).toContain('No import jobs yet.');
    expect(markup).not.toContain('Loading import jobs…');
    expect(markup).not.toContain('Import jobs are temporarily unavailable');
  });

  it('renders no preview region while idle', () => {
    const markup = renderToStaticMarkup(<DirectImportWizard {...wizardProps} />);

    expect(markup).not.toContain('Segment preview');
  });

  it('renders no preview region for an idle state even with a stale key and stale segments', () => {
    const markup = renderToStaticMarkup(
      <DirectImportWizard
        {...wizardProps}
        previewJobKey="job-1"
        previewState="idle"
        preview={{
          jobKey: 'job-1',
          documentKey: 'doc-1',
          evidenceKey: 'ev-1',
          sourceKey: 'src-1',
          proposalKey: null,
          title: 'Runbook',
          namespace: 'company/general',
          sensitivity: 'internal',
          segments: [
            { segmentKey: 'seg-1', position: 0, headingPath: ['Runbook'], content: 'marker content', tokenCount: 2 },
          ],
        }}
      />,
    );

    expect(markup).not.toContain('Segment preview');
    expect(markup).not.toContain('marker content');
  });

  it('keeps stale contracts mechanically non-actionable outside ready', () => {
    // Ready is the only state whose authority (selector identity) exists in
    // the DOM and whose submit button can ever enable; loading/error with
    // stale data leave no authority identity and a disabled submit.
    const ready = renderToStaticMarkup(<DirectImportWizard {...wizardProps} />);
    expect(ready).toContain('<option value="contract-1" selected="">company-file-upload · v1</option>');

    for (const staleState of ['loading', 'error'] as const) {
      const stale = renderToStaticMarkup(<DirectImportWizard {...wizardProps} contractsState={staleState} />);

      expect(stale).not.toContain('<option');
      expect(stale).not.toContain('company-file-upload');
      expect(stale).not.toContain('Create source contract');
      expect(stale).toContain('<button type="submit" class="btn btn-primary btn-sm" disabled=""');
    }
  });

  it('shows preview loading without stale segment content', () => {
    const markup = renderToStaticMarkup(
      <DirectImportWizard
        {...wizardProps}
        previewJobKey="job-1"
        previewState='loading'
        preview={{
          jobKey: 'job-1',
          documentKey: 'doc-1',
          evidenceKey: 'ev-1',
          sourceKey: 'src-1',
          proposalKey: null,
          title: 'Runbook',
          namespace: 'company/general',
          sensitivity: 'internal',
          segments: [
            { segmentKey: 'seg-1', position: 0, headingPath: ['Runbook'], content: 'marker content', tokenCount: 2 },
          ],
        }}
      />,
    );

    expect(markup).toContain('Loading segment preview…');
    expect(markup).not.toContain('marker content');
  });

  it('treats a preview error as unavailable with retry and no stale segments', () => {
    const markup = renderToStaticMarkup(
      <DirectImportWizard
        {...wizardProps}
        previewJobKey="job-1"
        previewState='error'
        preview={{
          jobKey: 'job-1',
          documentKey: 'doc-1',
          evidenceKey: 'ev-1',
          sourceKey: 'src-1',
          proposalKey: null,
          title: 'Runbook',
          namespace: 'company/general',
          sensitivity: 'internal',
          segments: [
            { segmentKey: 'seg-1', position: 0, headingPath: ['Runbook'], content: 'marker content', tokenCount: 2 },
          ],
        }}
      />,
    );

    expect(markup).toContain('Segment preview is temporarily unavailable');
    expect(markup).toContain('role="alert"');
    expect(markup).toContain('>Retry<');
    expect(markup).not.toContain('marker content');
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
        previewState='ready'
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

describe('Company Knowledge access audience catalog', () => {
  it('localizes the platform administrator audience in both locales', () => {
    expect(enCatalog.companyKnowledge.audiences.platformAdmins).toBe('Platform administrators');
    expect(zhCatalog.companyKnowledge.audiences.platformAdmins).toBe('平台管理员');
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

  it('localizes all direct-import query-state titles in both locales', () => {
    const enDirect = enCatalog.companyKnowledge.directImport;
    const zhDirect = zhCatalog.companyKnowledge.directImport;

    expect(enDirect.contractsLoading).toBe('Loading source contracts…');
    expect(zhDirect.contractsLoading).toBe('正在加载来源契约…');
    expect(enDirect.contractsUnavailable).toBe('Source contracts are temporarily unavailable');
    expect(zhDirect.contractsUnavailable).toBe('来源契约暂时不可用');
    expect(enDirect.jobsLoading).toBe('Loading import jobs…');
    expect(zhDirect.jobsLoading).toBe('正在加载导入任务…');
    expect(enDirect.jobsUnavailable).toBe('Import jobs are temporarily unavailable');
    expect(zhDirect.jobsUnavailable).toBe('导入任务暂时不可用');
    expect(enDirect.previewLoading).toBe('Loading segment preview…');
    expect(zhDirect.previewLoading).toBe('正在加载分段预览…');
    expect(enDirect.previewUnavailable).toBe('Segment preview is temporarily unavailable');
    expect(zhDirect.previewUnavailable).toBe('分段预览暂时不可用');
  });
});
