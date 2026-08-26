import { useEffect, useMemo, useRef, useState, type FormEvent, type ReactNode } from 'react';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { Link } from 'react-router-dom';
import { useTranslation } from 'react-i18next';
import {
  IconBook,
  IconBuilding,
  IconDatabase,
  IconRefresh,
  IconShieldCheck,
  IconUsers,
} from '@tabler/icons-react';

import { agentApi } from '../api/domains/agents';
import {
  companyKnowledgeApi,
  type CompanyImportJobSummary,
  type CompanyImportPreview,
  type CompanyKnowledgeAccessRule,
  type CompanyKnowledgeArea,
  type CompanyKnowledgeAudience,
  type CompanyKnowledgeCapability,
  type CompanyKnowledgeIntake,
  type CompanyKnowledgePublicationLifecycle,
  type CompanyKnowledgeReview,
  type CompanyKnowledgeReviewWorkspace,
  type CompanyKnowledgeSensitivity,
  type CompanyOntologyStatus,
  type CompanySourceContractSummary,
  type LegacyKnowledgeCandidate,
} from '../api/domains/companyKnowledge';
import { enterpriseApi } from '../api/domains/enterprise';
import './CompanyKnowledgeControlPlane.css';

type ControlLane = 'intake' | 'review' | 'access' | 'lifecycle';

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

function intakeStatusLabel(value: string, t: ReturnType<typeof useTranslation>['t']): string {
  if (value === 'ready_for_review') return t('companyKnowledge.status.readyForReview', 'Ready for review');
  if (value === 'retry_required' || value === 'held') {
    return t('companyKnowledge.status.needsAttention', 'Needs your attention');
  }
  if (value === 'queued' || value === 'processing' || value === 'retry_scheduled') {
    return t('companyKnowledge.status.inProgress', 'In progress');
  }
  return t('companyKnowledge.status.completed', 'Completed');
}

function reviewStatusLabel(value: string, t: ReturnType<typeof useTranslation>['t']): string {
  if (value === 'submitted') return t('companyKnowledge.status.readyForReview', 'Ready for review');
  if (value === 'in_review') return t('companyKnowledge.status.inReview', 'In review');
  if (value === 'changes_requested') return t('companyKnowledge.status.changesRequested', 'Changes requested');
  if (value === 'approved') return t('companyKnowledge.status.approved', 'Approved');
  if (value === 'publish_failed') return t('companyKnowledge.status.publishNeedsRetry', 'Publish needs retry');
  return value.replaceAll('_', ' ');
}

function capabilityLabel(
  value: CompanyKnowledgeCapability,
  t: ReturnType<typeof useTranslation>['t'],
): string {
  if (value === 'find_and_read') return t('companyKnowledge.capabilities.findAndRead', 'Find and read');
  if (value === 'propose_updates') return t('companyKnowledge.capabilities.proposeUpdates', 'Propose updates');
  if (value === 'review_and_publish') {
    return t('companyKnowledge.capabilities.reviewAndPublish', 'Review and publish');
  }
  if (value === 'manage_lifecycle') {
    return t('companyKnowledge.capabilities.manageLifecycle', 'Manage lifecycle');
  }
  return t('companyKnowledge.capabilities.useCompanyModel', 'Use company model');
}

export function IntakeQueueView({
  intakes,
  retryingKey,
  onRetry,
}: {
  intakes: CompanyKnowledgeIntake[];
  retryingKey: string | null;
  onRetry: (intakeKey: string) => void;
}) {
  const { t } = useTranslation();
  if (!intakes.length) {
    return (
      <div className="company-control-empty">
        {t('companyKnowledge.intakesEmpty', 'No review submissions have been started from this account.')}
      </div>
    );
  }
  return (
    <div className="company-control-list">
      {intakes.map((intake) => (
        <article key={intake.intakeKey} className="company-control-row">
          <div>
            <strong>{intake.title}</strong>
            <small>
              {intake.kind === 'legacy'
                ? t('companyKnowledge.kind.legacy', 'Recovered company file')
                : t('companyKnowledge.kind.personal', 'Personal Knowledge submission')}
              {' · '}
              {intake.sourceLabel}
            </small>
            <span>
              {areaLabel(intake.area, t)} · {sensitivityLabel(intake.sensitivity, t)}
            </span>
          </div>
          <div className="company-control-row-actions">
            <span className={`ui-chip status-${intake.status}`}>
              {intakeStatusLabel(intake.status, t)}
            </span>
            {intake.recovery === 'manual' && (
              <button
                type="button"
                className="btn btn-secondary btn-sm"
                disabled={retryingKey === intake.intakeKey}
                onClick={() => onRetry(intake.intakeKey)}
              >
                <IconRefresh size={14} stroke={1.7} />
                {t('common.retry', 'Retry')}
              </button>
            )}
          </div>
        </article>
      ))}
    </div>
  );
}

export function ReviewQueueView({
  reviews,
  selectedKey,
  onSelect,
}: {
  reviews: CompanyKnowledgeReview[];
  selectedKey: string | null;
  onSelect: (reviewKey: string) => void;
}) {
  const { t } = useTranslation();
  if (!reviews.length) {
    return (
      <div className="company-control-empty">
        {t('companyKnowledge.reviewEmpty', 'No authorized review items are waiting for you.')}
      </div>
    );
  }
  return (
    <div className="company-control-list">
      {reviews.map((review) => (
        <button
          type="button"
          key={review.reviewKey}
          className={
            selectedKey === review.reviewKey
              ? 'company-control-row company-control-select active'
              : 'company-control-row company-control-select'
          }
          onClick={() => onSelect(review.reviewKey)}
        >
          <div>
            <strong>{review.title}</strong>
            <small>
              {review.createdBy === 'digital_employee'
                ? t('companyKnowledge.createdByEmployee', 'Proposed by a digital employee')
                : t('companyKnowledge.createdByMember', 'Submitted by a company member')}
            </small>
            <span>
              {areaLabel(review.area, t)} · {sensitivityLabel(review.sensitivity, t)}
            </span>
          </div>
          <span className={`ui-chip status-${review.status}`}>
            {reviewStatusLabel(review.status, t)}
          </span>
        </button>
      ))}
    </div>
  );
}

export function AccessRulesView({
  rules,
  revokingKey,
  actionReady,
  onRevoke,
}: {
  rules: CompanyKnowledgeAccessRule[];
  revokingKey: string | null;
  actionReady: boolean;
  onRevoke: (permissionKey: string) => void;
}) {
  const { t } = useTranslation();
  if (!rules.length) {
    return (
      <div className="company-control-empty">
        {t('companyKnowledge.accessEmpty', 'No managed Company Knowledge access rules are visible.')}
      </div>
    );
  }
  return (
    <div className="company-control-list">
      {rules.map((rule) => (
        <article key={rule.permissionKey} className="company-control-row">
          <div>
            <strong>{rule.audience}</strong>
            <small>{rule.resource}</small>
            <span>
              {rule.capabilities.map((capability) => capabilityLabel(capability, t)).join(' · ') ||
                t('companyKnowledge.noCapabilities', 'No active capability')}
            </span>
          </div>
          <div className="company-control-row-actions">
            <span className={`ui-chip ${rule.effect === 'deny' ? 'is-danger' : ''}`}>
              {rule.effect === 'deny'
                ? t('companyKnowledge.accessBlocked', 'Blocked')
                : t('companyKnowledge.accessAllowed', 'Allowed')}
            </span>
            {rule.active && (
              <button
                type="button"
                className="btn btn-secondary btn-sm"
                disabled={!actionReady || revokingKey === rule.permissionKey}
                onClick={() => onRevoke(rule.permissionKey)}
              >
                {t('companyKnowledge.revokeAccess', 'Remove access')}
              </button>
            )}
          </div>
        </article>
      ))}
    </div>
  );
}

export function KnowledgeLifecycleView({
  publications,
  busyKey,
  actionReady,
  onLifecycleAction,
}: {
  publications: CompanyKnowledgePublicationLifecycle[];
  busyKey: string | null;
  actionReady: boolean;
  onLifecycleAction: (publication: CompanyKnowledgePublicationLifecycle) => void;
}) {
  const { t } = useTranslation();
  if (!publications.length) {
    return (
      <div className="company-control-empty">
        {t('companyKnowledge.lifecycleEmpty', 'No lifecycle items are available for this account.')}
      </div>
    );
  }
  return (
    <div className="company-control-list">
      {publications.map((publication) => (
        <article key={publication.publicationKey} className="company-control-row">
          <div>
            <strong>{publication.title}</strong>
            <small>
              {t('companyKnowledge.publishedVersion', 'Published version')} {publication.version}
            </small>
            <span>
              {areaLabel(publication.area, t)} · {sensitivityLabel(publication.sensitivity, t)}
            </span>
          </div>
          <div className="company-control-row-actions">
            <span className={`ui-chip status-${publication.status}`}>
              {publication.status === 'retired'
                ? t('companyKnowledge.status.retired', 'Retired')
                : t('companyKnowledge.status.active', 'Available')}
            </span>
            <button
              type="button"
              className="btn btn-secondary btn-sm"
              disabled={!actionReady || busyKey === publication.publicationKey}
              onClick={() => onLifecycleAction(publication)}
            >
              {publication.availableAction === 'restore'
                ? t('companyKnowledge.restore', 'Restore')
                : t('companyKnowledge.retire', 'Retire')}
            </button>
          </div>
        </article>
      ))}
    </div>
  );
}

export function OntologyStatusView({ status }: { status: CompanyOntologyStatus | null }) {
  const { t } = useTranslation();
  if (!status) {
    return (
      <div className="company-control-empty">
        {t('companyKnowledge.ontologyUnavailable', 'Company model status is unavailable.')}
      </div>
    );
  }
  const engineLabel =
    status.engineStatus === 'available'
      ? t('companyKnowledge.status.available', 'Available')
      : status.engineStatus === 'degraded'
        ? t('companyKnowledge.status.degraded', 'Degraded')
        : t('companyKnowledge.status.unavailable', 'Unavailable');
  return (
    <div className="company-ontology-status">
      <div className="company-control-metric">
        <span>{t('companyKnowledge.modelEngine', 'Company model engine')}</span>
        <strong>{engineLabel}</strong>
      </div>
      <div className="company-control-metric">
        <span>{t('companyKnowledge.installedModels', 'Installed domain packs')}</span>
        <strong>{status.installedPacks.length}</strong>
      </div>
      <div className="company-control-metric">
        <span>{t('companyKnowledge.activeModelReleases', 'Model releases')}</span>
        <strong>{status.releases.filter((release) => release.status === 'active').length}</strong>
      </div>
      <div className="company-ontology-packs">
        {status.installedPacks.map((pack) => (
          <span key={`${pack.name}:${pack.version}`}>
            <strong>{pack.name}</strong>
            <small>
              {t('companyKnowledge.version', 'Version')} {pack.version}
            </small>
          </span>
        ))}
      </div>
    </div>
  );
}

function ActionUnconfirmed() {
  const { t } = useTranslation();
  return (
    <div className="company-control-error" role="alert">
      {t(
        'companyKnowledge.actionUnconfirmed',
        'The action did not return a confirmed result. Refresh the current state before retrying.',
      )}
    </div>
  );
}

function LegacyIntakeForm({
  candidates,
  pending,
  error,
  onSubmit,
}: {
  candidates: LegacyKnowledgeCandidate[];
  pending: boolean;
  error: unknown;
  onSubmit: (input: {
    candidate: LegacyKnowledgeCandidate;
    title: string;
    purpose: string;
    area: CompanyKnowledgeArea;
    sensitivity: CompanyKnowledgeSensitivity;
  }) => void;
}) {
  const { t } = useTranslation();
  const [candidateIndex, setCandidateIndex] = useState('0');
  const [title, setTitle] = useState('');
  const [purpose, setPurpose] = useState('');
  const [area, setArea] = useState<CompanyKnowledgeArea>('general');
  const [sensitivity, setSensitivity] = useState<CompanyKnowledgeSensitivity>('company');
  const [attested, setAttested] = useState(false);
  const candidate = candidates[Number(candidateIndex)];

  return (
    <form
      className="company-control-form"
      onSubmit={(event: FormEvent) => {
        event.preventDefault();
        if (candidate && title.trim() && purpose.trim() && attested) {
          onSubmit({ candidate, title, purpose, area, sensitivity });
        }
      }}
    >
      <div className="company-control-form-copy">
        <strong>{t('companyKnowledge.recoverLegacyTitle', 'Recover a retired company file')}</strong>
        <small>
          {t(
            'companyKnowledge.recoverLegacyDescription',
            'Choose an exact archived file and send it through the same human review used for every Company Knowledge publication.',
          )}
        </small>
      </div>
      <label>
        <span>{t('companyKnowledge.archivedFile', 'Archived file')}</span>
        <select value={candidateIndex} onChange={(event) => setCandidateIndex(event.target.value)}>
          {candidates.map((item, index) => (
            <option key={`${item.label}:${index}`} value={String(index)}>
              {item.label} · {Math.max(1, Math.ceil(item.sizeBytes / 1024))} KB
            </option>
          ))}
        </select>
      </label>
      <label>
        <span>{t('companyKnowledge.companyTitle', 'Company title')}</span>
        <input value={title} maxLength={300} onChange={(event) => setTitle(event.target.value)} />
      </label>
      <div className="company-control-form-grid">
        <label>
          <span>{t('companyKnowledge.companyArea', 'Company area')}</span>
          <select value={area} onChange={(event) => setArea(event.target.value as CompanyKnowledgeArea)}>
            <option value="general">{t('companyKnowledge.areas.general', 'General')}</option>
            <option value="policies">{t('companyKnowledge.areas.policies', 'Policies')}</option>
            <option value="team_notes">{t('companyKnowledge.areas.teamNotes', 'Team notes')}</option>
            <option value="playbooks">{t('companyKnowledge.areas.playbooks', 'Playbooks')}</option>
            <option value="operations">{t('companyKnowledge.areas.operations', 'Operations')}</option>
          </select>
        </label>
        <label>
          <span>{t('companyKnowledge.visibility', 'Information level')}</span>
          <select
            value={sensitivity}
            onChange={(event) => setSensitivity(event.target.value as CompanyKnowledgeSensitivity)}
          >
            <option value="company">{t('companyKnowledge.sensitivity.company', 'Company-wide')}</option>
            <option value="personal_data">
              {t('companyKnowledge.sensitivity.personalData', 'Contains personal data')}
            </option>
            <option value="restricted">{t('companyKnowledge.sensitivity.restricted', 'Restricted')}</option>
          </select>
        </label>
      </div>
      <label>
        <span>{t('companyKnowledge.reviewPurpose', 'Why should this enter review?')}</span>
        <textarea value={purpose} rows={3} maxLength={1000} onChange={(event) => setPurpose(event.target.value)} />
      </label>
      <label className="company-control-attestation">
        <input type="checkbox" checked={attested} onChange={(event) => setAttested(event.target.checked)} />
        <span>
          {t(
            'companyKnowledge.legacyAttestation',
            'I confirm this archived file may enter Company Knowledge review.',
          )}
        </span>
      </label>
      {Boolean(error) && (
        <div className="company-control-error" role="alert">
          {t(
            'companyKnowledge.legacySubmitError',
            'The review request could not be created. No Company Knowledge item was published.',
          )}
        </div>
      )}
      <button
        type="submit"
        className="btn btn-primary btn-sm"
        disabled={!candidate || !title.trim() || !purpose.trim() || !attested || pending}
      >
        {pending
          ? t('companyKnowledge.submitting', 'Submitting...')
          : t('companyKnowledge.submitForReview', 'Submit for review')}
      </button>
    </form>
  );
}

function ReviewWorkspace({
  workspace,
  loading,
  error,
  actionError,
  busy,
  onMaterialize,
  onDecision,
  onPublish,
}: {
  workspace: CompanyKnowledgeReviewWorkspace | null;
  loading: boolean;
  error: unknown;
  actionError: unknown;
  busy: boolean;
  onMaterialize: (title: string, markdown: string) => void;
  onDecision: (decision: 'approve' | 'reject' | 'request_changes', reason: string) => void;
  onPublish: () => void;
}) {
  const { t } = useTranslation();
  const [title, setTitle] = useState('');
  const [markdown, setMarkdown] = useState('');
  const [reason, setReason] = useState('');
  const [attested, setAttested] = useState(false);

  useEffect(() => {
    setTitle(workspace?.candidateTitle ?? '');
    setMarkdown(workspace?.candidateMarkdown ?? '');
    setReason('');
    setAttested(false);
  }, [workspace]);

  if (loading) {
    return <div className="company-control-empty">{t('companyKnowledge.reviewLoading', 'Opening review...')}</div>;
  }
  if (error) {
    return (
      <div className="company-control-empty" role="alert">
        {t(
          'companyKnowledge.reviewUnavailable',
          'This review could not be opened with the current authority. No decision was recorded.',
        )}
      </div>
    );
  }
  if (!workspace) {
    return (
      <div className="company-control-empty">
        {t('companyKnowledge.selectReview', 'Select an item to review its business content.')}
      </div>
    );
  }
  const requiresContent = workspace.needsMaterialization && !workspace.materialized;
  const canReview = !requiresContent && workspace.status !== 'approved';

  return (
    <section className="company-review-workspace">
      <div>
        <span className="workbench-eyebrow">{reviewStatusLabel(workspace.status, t)}</span>
        <h2>{workspace.title}</h2>
        {workspace.reason && <p>{workspace.reason}</p>}
      </div>
      {requiresContent && (
        <>
          <label>
            <span>{t('companyKnowledge.reviewedTitle', 'Reviewed title')}</span>
            <input value={title} maxLength={300} onChange={(event) => setTitle(event.target.value)} />
          </label>
          <label>
            <span>{t('companyKnowledge.reviewedContent', 'Complete reviewed content')}</span>
            <textarea
              value={markdown}
              rows={14}
              onChange={(event) => setMarkdown(event.target.value)}
              placeholder={t(
                'companyKnowledge.reviewedContentPlaceholder',
                'Apply the proposed change to the complete content before approval.',
              )}
            />
          </label>
          <label className="company-control-attestation">
            <input type="checkbox" checked={attested} onChange={(event) => setAttested(event.target.checked)} />
            <span>
              {t(
                'companyKnowledge.reviewContentAttestation',
                'I confirm the proposed change is applied to this complete reviewed content.',
              )}
            </span>
          </label>
          <button
            type="button"
            className="btn btn-primary btn-sm"
            disabled={!title.trim() || !markdown.trim() || !attested || busy}
            onClick={() => onMaterialize(title, markdown)}
          >
            {t('companyKnowledge.saveReviewedContent', 'Save reviewed content')}
          </button>
        </>
      )}
      {canReview && (
        <>
          <label>
            <span>{t('companyKnowledge.reviewReason', 'Review reason')}</span>
            <textarea value={reason} rows={3} maxLength={10000} onChange={(event) => setReason(event.target.value)} />
          </label>
          <div className="company-control-actions">
            <button
              type="button"
              className="btn btn-secondary btn-sm"
              disabled={!reason.trim() || busy}
              onClick={() => onDecision('request_changes', reason)}
            >
              {t('companyKnowledge.requestChanges', 'Request changes')}
            </button>
            <button
              type="button"
              className="btn btn-secondary btn-sm"
              disabled={!reason.trim() || busy}
              onClick={() => onDecision('reject', reason)}
            >
              {t('companyKnowledge.reject', 'Reject')}
            </button>
            <button
              type="button"
              className="btn btn-primary btn-sm"
              disabled={!reason.trim() || busy}
              onClick={() => onDecision('approve', reason)}
            >
              {t('companyKnowledge.approve', 'Approve')}
            </button>
          </div>
        </>
      )}
      {workspace.status === 'approved' && (
        <button type="button" className="btn btn-primary btn-sm" disabled={busy} onClick={onPublish}>
          {t('companyKnowledge.publish', 'Publish to Company Library')}
        </button>
      )}
      {Boolean(actionError) && <ActionUnconfirmed />}
    </section>
  );
}

function AccessGrantForm({
  audiences,
  pending,
  error,
  onSubmit,
}: {
  audiences: CompanyKnowledgeAudience[];
  pending: boolean;
  error: unknown;
  onSubmit: (input: {
    audience: CompanyKnowledgeAudience;
    capabilities: CompanyKnowledgeCapability[];
    sensitivity: CompanyKnowledgeSensitivity;
    effect: 'allow' | 'deny';
  }) => void;
}) {
  const { t } = useTranslation();
  const [audienceIndex, setAudienceIndex] = useState('0');
  const [capabilities, setCapabilities] = useState<CompanyKnowledgeCapability[]>(['find_and_read']);
  const [sensitivity, setSensitivity] = useState<CompanyKnowledgeSensitivity>('personal_data');
  const [effect, setEffect] = useState<'allow' | 'deny'>('allow');
  const audience = audiences[Number(audienceIndex)];

  const toggleCapability = (capability: CompanyKnowledgeCapability) => {
    setCapabilities((current) =>
      current.includes(capability)
        ? current.filter((item) => item !== capability)
        : [...current, capability],
    );
  };

  return (
    <form
      className="company-control-form"
      onSubmit={(event: FormEvent) => {
        event.preventDefault();
        if (audience && capabilities.length) {
          onSubmit({ audience, capabilities, sensitivity, effect });
        }
      }}
    >
      <div className="company-control-form-grid">
        <label>
          <span>{t('companyKnowledge.audience', 'Audience')}</span>
          <select value={audienceIndex} onChange={(event) => setAudienceIndex(event.target.value)}>
            {audiences.map((item, index) => (
              <option key={`${item.kind}:${item.key}`} value={String(index)}>
                {item.label}
              </option>
            ))}
          </select>
        </label>
        <label>
          <span>{t('companyKnowledge.accessDecision', 'Access decision')}</span>
          <select value={effect} onChange={(event) => setEffect(event.target.value as 'allow' | 'deny')}>
            <option value="allow">{t('companyKnowledge.accessAllowed', 'Allowed')}</option>
            <option value="deny">{t('companyKnowledge.accessBlocked', 'Blocked')}</option>
          </select>
        </label>
        <label>
          <span>{t('companyKnowledge.visibility', 'Information level')}</span>
          <select
            value={sensitivity}
            onChange={(event) => setSensitivity(event.target.value as CompanyKnowledgeSensitivity)}
          >
            <option value="company">{t('companyKnowledge.sensitivity.company', 'Company-wide')}</option>
            <option value="personal_data">
              {t('companyKnowledge.sensitivity.personalData', 'Contains personal data')}
            </option>
            <option value="restricted">{t('companyKnowledge.sensitivity.restricted', 'Restricted')}</option>
          </select>
        </label>
      </div>
      <fieldset className="company-control-capabilities">
        <legend>{t('companyKnowledge.businessCapabilities', 'Business capabilities')}</legend>
        {(
          [
            'find_and_read',
            'propose_updates',
            'review_and_publish',
            'manage_lifecycle',
            'use_company_model',
          ] as CompanyKnowledgeCapability[]
        ).map((capability) => (
          <label key={capability}>
            <input
              type="checkbox"
              checked={capabilities.includes(capability)}
              onChange={() => toggleCapability(capability)}
            />
            <span>{capabilityLabel(capability, t)}</span>
          </label>
        ))}
      </fieldset>
      {Boolean(error) && (
        <div className="company-control-error" role="alert">
          {t(
            'companyKnowledge.accessSaveError',
            'The access rule could not be saved. Existing access was not changed.',
          )}
        </div>
      )}
      <button
        type="submit"
        className="btn btn-primary btn-sm"
        disabled={!audience || capabilities.length === 0 || pending}
      >
        {t('companyKnowledge.saveAccess', 'Save access rule')}
      </button>
    </form>
  );
}

function SectionError({ onRetry, title }: { onRetry: () => void; title?: string }) {
  const { t } = useTranslation();
  return (
    <div className="company-control-empty" role="alert">
      <strong>{title ?? t('companyKnowledge.sectionUnavailable', 'This section is temporarily unavailable')}</strong>
      <span>
        {t(
          'companyKnowledge.sectionUnavailableDescription',
          'No empty-state conclusion was made and no action was taken.',
        )}
      </span>
      <button type="button" className="btn btn-secondary btn-sm" onClick={onRetry}>
        <IconRefresh size={14} stroke={1.7} />
        {t('common.retry', 'Retry')}
      </button>
    </div>
  );
}

// ---------------------------------------------------------------------------
// RC-02: direct file import wizard (admin intake lane)
// ---------------------------------------------------------------------------

const directImportFormats = [
  { labelKey: 'companyKnowledge.directImport.formatPdf', fallback: 'PDF', helper: 'pdf' },
  { labelKey: 'companyKnowledge.directImport.formatDocx', fallback: 'Word / DOCX', helper: 'docx' },
  { labelKey: 'companyKnowledge.directImport.formatMarkdown', fallback: 'Markdown', helper: 'md · txt' },
] as const;

function companyImportLifecycleLabel(lifecycleStatus: string, t: ReturnType<typeof useTranslation>['t']): string {
  switch (lifecycleStatus) {
    case 'queued':
      return t('companyKnowledge.directImport.statusQueued', 'Queued');
    case 'running':
      return t('companyKnowledge.directImport.statusRunning', 'Running');
    case 'completed':
      return t('companyKnowledge.directImport.statusCompleted', 'Completed');
    case 'failed':
      return t('companyKnowledge.directImport.statusFailed', 'Failed');
    case 'cancelled':
      return t('companyKnowledge.directImport.statusCancelled', 'Cancelled');
    case 'held':
      return t('companyKnowledge.directImport.statusHeld', 'On hold');
    default:
      return t('companyKnowledge.directImport.statusUnknown', 'Status unavailable');
  }
}

function companyImportErrorLabel(errorCode: string | null, t: ReturnType<typeof useTranslation>['t']): string | null {
  if (!errorCode) return null;
  switch (errorCode) {
    case 'conversion_failed':
      return t('companyKnowledge.directImport.errorConversionFailed', 'The file could not be converted.');
    case 'conversion_timeout':
      return t('companyKnowledge.directImport.errorConversionTimeout', 'Conversion timed out; you can retry.');
    case 'source_missing':
      return t('companyKnowledge.directImport.errorSourceMissing', 'The uploaded source is no longer available.');
    case 'unsupported_file_type':
      return t('companyKnowledge.directImport.errorUnsupportedFileType', 'This file type is not supported.');
    case 'import_payload_invalid':
      return t('companyKnowledge.directImport.errorImportPayloadInvalid', 'The import request was invalid.');
    case 'company_knowledge_import_title_conflict':
      return t('companyKnowledge.directImport.errorTitleConflict', 'This file matches an existing document but uses a different title.');
    case 'company_knowledge_import_attempts_exhausted':
      return t('companyKnowledge.directImport.errorAttemptLimit', 'Retry limit reached.');
    case 'worker_error':
    case 'import_failed':
      return t('companyKnowledge.directImport.errorImportFailed', 'Import failed with an unspecified error.');
    default:
      return t('companyKnowledge.directImport.errorUnknown', 'Import failed with an unspecified error.');
  }
}

function companyImportActionErrorLabel(code: string | null, t: ReturnType<typeof useTranslation>['t']): string {
  switch (code) {
    case 'upload_too_large':
      return t('companyKnowledge.directImport.actionUploadTooLarge', 'The file is too large to import.');
    case 'not_retryable':
      return t('companyKnowledge.directImport.actionNotRetryable', 'This import cannot be retried now.');
    case 'retry_attempt_limit':
      return t('companyKnowledge.directImport.actionRetryAttemptLimit', 'Retry limit reached.');
    case 'not_cancellable_while_running':
      return t('companyKnowledge.directImport.actionNotCancellableRunning', 'A running import cannot be cancelled.');
    case 'not_cancellable_terminal':
      return t('companyKnowledge.directImport.actionNotCancellableTerminal', 'This import has already finished.');
    case 'preview_requires_completed':
      return t('companyKnowledge.directImport.actionPreviewRequiresCompleted', 'Preview is available once the import completes.');
    case 'proposal_requires_completed_import':
      return t('companyKnowledge.directImport.actionProposalRequiresCompleted', 'A proposal can be created once the import completes.');
    case 'company_knowledge_admin_required':
      return t('companyKnowledge.directImport.actionAdminRequired', 'A company admin must perform this action.');
    default:
      return t('companyKnowledge.directImport.actionUnknown', 'The action could not be completed.');
  }
}

/** Map any action failure to one bounded code for the alert surface. */
export function companyImportActionErrorCode(error: unknown): string {
  const data = (error as { data?: unknown } | null)?.data;
  if (data && typeof data === 'object' && 'code' in data) {
    const code = (data as { code?: unknown }).code;
    if (typeof code === 'string' && code) return code;
  }
  return 'unknown';
}

export type DirectImportBoundaryState = 'loading' | 'error' | 'ready';
export type DirectImportPreviewState = 'idle' | 'loading' | 'error' | 'ready';

export function DirectImportWizard({
  contracts,
  contractsState,
  jobs,
  jobsState,
  uploading,
  actionError,
  busyJobKey,
  previewJobKey,
  preview,
  previewState,
  onUpload,
  onSelectContract,
  onCreateContract,
  onRetryJob,
  onCancelJob,
  onPreview,
  onCreateProposal,
  onRetryContracts,
  onRetryJobs,
  onRetryPreview,
}: {
  contracts: CompanySourceContractSummary[];
  contractsState: DirectImportBoundaryState;
  jobs: CompanyImportJobSummary[];
  jobsState: DirectImportBoundaryState;
  uploading: boolean;
  actionError: string | null;
  busyJobKey: string | null;
  previewJobKey: string | null;
  preview: CompanyImportPreview | null;
  previewState: DirectImportPreviewState;
  onUpload: (input: { file: File; contract: CompanySourceContractSummary; title: string; purpose: string }) => void;
  onSelectContract: (contractKey: string) => void;
  onCreateContract: (input: { stable_source_id: string; allowed_namespaces: string[]; default_sensitivity: string }) => void;
  onRetryJob: (jobKey: string) => void;
  onCancelJob: (jobKey: string) => void;
  onPreview: (jobKey: string) => void;
  onCreateProposal: (jobKey: string) => void;
  onRetryContracts: () => void;
  onRetryJobs: () => void;
  onRetryPreview: () => void;
}) {
  const { t } = useTranslation();
  // Only ready may use contract data: in loading/error even a stale array is
  // mechanically non-actionable (no authority, no upload authorization).
  const activeContracts =
    contractsState === 'ready' ? contracts.filter((contract) => contract.status === 'active') : [];
  const [selectedContractKey, setSelectedContractKey] = useState('');
  const [file, setFile] = useState<File | null>(null);
  const [title, setTitle] = useState('');
  const [purpose, setPurpose] = useState('');
  const [newContractName, setNewContractName] = useState('');
  const selectedContract =
    activeContracts.find((contract) => contract.contractKey === selectedContractKey) ?? activeContracts[0] ?? null;
  const namespace = selectedContract?.allowedNamespaces[0] ?? 'company/general';
  const canUpload = contractsState === 'ready' && Boolean(file && selectedContract && title.trim() && !uploading);

  return (
    <section className="company-control-panel">
      <div className="company-control-panel-head">
        <div>
          <h2>{t('companyKnowledge.directImport.title', 'Import company files')}</h2>
          <p>
            {t(
              'companyKnowledge.directImport.description',
              'Upload a document, review the converted segments, then submit it for the same human review as every publication.',
            )}
          </p>
        </div>
      </div>

      {actionError && (
        <div className="company-control-error" role="alert">
          {companyImportActionErrorLabel(actionError, t)}
        </div>
      )}

      <form
        className="company-control-form"
        onSubmit={(event: FormEvent) => {
          event.preventDefault();
          if (contractsState === 'ready' && file && selectedContract && title.trim()) {
            onUpload({ file, contract: selectedContract, title: title.trim(), purpose: purpose.trim() });
          }
        }}
      >
        {contractsState === 'loading' ? (
          <div className="company-control-empty">
            {t('companyKnowledge.directImport.contractsLoading', 'Loading source contracts…')}
          </div>
        ) : contractsState === 'error' ? (
          <SectionError
            title={t('companyKnowledge.directImport.contractsUnavailable', 'Source contracts are temporarily unavailable')}
            onRetry={onRetryContracts}
          />
        ) : activeContracts.length === 0 ? (
          <div className="company-control-form-grid">
            <label>
              <span>{t('companyKnowledge.directImport.contractName', 'Source contract name')}</span>
              <input
                value={newContractName}
                maxLength={300}
                onChange={(event) => setNewContractName(event.target.value)}
                placeholder={t('companyKnowledge.directImport.contractNamePlaceholder', 'company-file-upload')}
              />
            </label>
            <button
              type="button"
              className="btn btn-secondary btn-sm"
              disabled={!newContractName.trim() || uploading}
              onClick={() =>
                onCreateContract({
                  stable_source_id: newContractName.trim(),
                  allowed_namespaces: ['company/general'],
                  default_sensitivity: 'PL1_public',
                })
              }
            >
              {t('companyKnowledge.directImport.createContract', 'Create source contract')}
            </button>
          </div>
        ) : (
          <label>
            <span>{t('companyKnowledge.directImport.sourceContract', 'Source contract')}</span>
            <select
              value={selectedContract?.contractKey ?? ''}
              onChange={(event) => {
                setSelectedContractKey(event.target.value);
                onSelectContract(event.target.value);
              }}
            >
              {activeContracts.map((contract) => (
                <option key={contract.contractKey} value={contract.contractKey}>
                  {contract.stableSourceId} · v{contract.version}
                </option>
              ))}
            </select>
          </label>
        )}

        <div className="personal-kb-format-grid">
          {directImportFormats.map((format) => {
            const formatLabels: Record<(typeof directImportFormats)[number]['labelKey'], string> = {
              'companyKnowledge.directImport.formatPdf': t('companyKnowledge.directImport.formatPdf', 'PDF'),
              'companyKnowledge.directImport.formatDocx': t('companyKnowledge.directImport.formatDocx', 'Word / DOCX'),
              'companyKnowledge.directImport.formatMarkdown': t('companyKnowledge.directImport.formatMarkdown', 'Markdown'),
            };
            return (
              <span key={format.labelKey}>
                <strong>{formatLabels[format.labelKey]}</strong>
                <small>{format.helper}</small>
              </span>
            );
          })}
        </div>

        <label>
          <span>{t('companyKnowledge.directImport.fileLabel', 'File')}</span>
          <input
            type="file"
            accept=".pdf,.docx,.md,.markdown,.txt"
            onChange={(event) => {
              const next = event.target.files?.[0] ?? null;
              setFile(next);
              if (next && !title.trim()) setTitle(next.name.replace(/\.[^.]+$/, ''));
            }}
          />
        </label>
        <label>
          <span>{t('companyKnowledge.directImport.companyTitle', 'Company title')}</span>
          <input value={title} maxLength={300} onChange={(event) => setTitle(event.target.value)} />
        </label>
        <label>
          <span>{t('companyKnowledge.directImport.purpose', 'Purpose (optional)')}</span>
          <textarea value={purpose} rows={2} maxLength={1000} onChange={(event) => setPurpose(event.target.value)} />
        </label>
        <button type="submit" className="btn btn-primary btn-sm" disabled={!canUpload}>
          {uploading
            ? t('companyKnowledge.directImport.uploading', 'Uploading...')
            : t('companyKnowledge.directImport.upload', 'Import file')}
        </button>
      </form>

      <div className="company-control-subsection">
        <h3>{t('companyKnowledge.directImport.jobsTitle', 'Import jobs')}</h3>
        {jobsState === 'loading' ? (
          <div className="company-control-empty">{t('companyKnowledge.directImport.jobsLoading', 'Loading import jobs…')}</div>
        ) : jobsState === 'error' ? (
          <SectionError
            title={t('companyKnowledge.directImport.jobsUnavailable', 'Import jobs are temporarily unavailable')}
            onRetry={onRetryJobs}
          />
        ) : jobs.length === 0 ? (
          <div className="company-control-empty">{t('companyKnowledge.directImport.jobsEmpty', 'No import jobs yet.')}</div>
        ) : (
          jobs.map((job) => {
            const errorLabel = companyImportErrorLabel(job.errorCode, t);
            const isCompleted = job.lifecycleStatus === 'completed' && job.documentKey;
            return (
              <article key={job.jobKey} className="company-control-row">
                <div>
                  <strong>{job.title || job.sourceFilename || job.jobKey}</strong>
                  <span>
                    {job.sourceFilename ? `${job.sourceFilename} · ` : ''}
                    {companyImportLifecycleLabel(job.lifecycleStatus, t)} · {t('companyKnowledge.directImport.attempts', 'attempts')} {job.attemptCount}/{job.maxAttempts}
                  </span>
                  {errorLabel && <small>{errorLabel}</small>}
                </div>
                <span className="company-control-row-actions">
                  {job.cancellable && (
                    <button
                      type="button"
                      className="btn btn-secondary btn-sm"
                      disabled={busyJobKey === job.jobKey}
                      onClick={() => onCancelJob(job.jobKey)}
                    >
                      {t('companyKnowledge.directImport.cancel', 'Cancel')}
                    </button>
                  )}
                  {job.retryable && (
                    <button
                      type="button"
                      className="btn btn-secondary btn-sm"
                      disabled={busyJobKey === job.jobKey}
                      onClick={() => onRetryJob(job.jobKey)}
                    >
                      {t('companyKnowledge.directImport.retry', 'Retry')}
                    </button>
                  )}
                  {isCompleted && !job.proposalKey && (
                    <>
                      <button
                        type="button"
                        className="btn btn-secondary btn-sm"
                        onClick={() => onPreview(job.jobKey)}
                      >
                        {t('companyKnowledge.directImport.preview', 'Preview')}
                      </button>
                      <button
                        type="button"
                        className="btn btn-primary btn-sm"
                        disabled={busyJobKey === job.jobKey}
                        onClick={() => onCreateProposal(job.jobKey)}
                      >
                        {t('companyKnowledge.directImport.createProposal', 'Create proposal')}
                      </button>
                    </>
                  )}
                  {job.proposalKey && (
                    <span className="ui-chip">{t('companyKnowledge.directImport.submittedForReview', 'Submitted for review')}</span>
                  )}
                </span>
              </article>
            );
          })
        )}
      </div>

      {previewJobKey && previewState !== 'idle' && (
        <div className="company-control-subsection">
          <h3>{t('companyKnowledge.directImport.previewTitle', 'Segment preview')}</h3>
          {previewState === 'loading' ? (
            <div className="company-control-empty">
              {t('companyKnowledge.directImport.previewLoading', 'Loading segment preview…')}
            </div>
          ) : previewState === 'error' ? (
            <SectionError
              title={t('companyKnowledge.directImport.previewUnavailable', 'Segment preview is temporarily unavailable')}
              onRetry={onRetryPreview}
            />
          ) : (
            preview && (
              <div className="company-control-preview">
                <strong>{preview.title}</strong>
                {preview.segments.map((segment) => (
                  <div key={segment.segmentKey} className="company-control-preview-segment">
                    <span>
                      #{segment.position + 1} {segment.headingPath.join(' / ')} · {segment.tokenCount} tok
                    </span>
                    <p>{segment.content}</p>
                  </div>
                ))}
              </div>
            )
          )}
        </div>
      )}
    </section>
  );
}

export default function CompanyKnowledgeControlPlane() {
  const { t } = useTranslation();
  const queryClient = useQueryClient();
  const [activeLane, setActiveLane] = useState<ControlLane>('intake');
  const [selectedReviewKey, setSelectedReviewKey] = useState<string | null>(null);
  const [retryingKey, setRetryingKey] = useState<string | null>(null);
  const [revokingKey, setRevokingKey] = useState<string | null>(null);
  const [lifecycleBusyKey, setLifecycleBusyKey] = useState<string | null>(null);
  const [accessReason, setAccessReason] = useState('');
  const [lifecycleReason, setLifecycleReason] = useState('');
  const [importBusyJobKey, setImportBusyJobKey] = useState<string | null>(null);
  const [importActionError, setImportActionError] = useState<string | null>(null);
  const [importSelectedContractKey, setImportSelectedContractKey] = useState('');
  const [previewJobKey, setPreviewJobKey] = useState<string | null>(null);

  const intakesQuery = useQuery({
    queryKey: ['company-knowledge-intakes'],
    queryFn: () => companyKnowledgeApi.listIntakes(),
  });
  const sourceContractsQuery = useQuery({
    queryKey: ['company-knowledge-source-contracts'],
    queryFn: () => companyKnowledgeApi.listSourceContracts(),
    enabled: activeLane === 'intake',
  });
  const importJobsQuery = useQuery({
    queryKey: ['company-knowledge-import-jobs'],
    queryFn: () => companyKnowledgeApi.listCompanyImportJobs(),
    enabled: activeLane === 'intake',
    // Lifecycle-aware polling: refresh only while any job is queued/running.
    refetchInterval: (query) => {
      const jobs = query.state.data ?? [];
      return jobs.some((job) => job.lifecycleStatus === 'queued' || job.lifecycleStatus === 'running') ? 3000 : false;
    },
  });
  const importJobs = importJobsQuery.data ?? [];
  const previewQuery = useQuery({
    queryKey: ['company-knowledge-import-preview', previewJobKey],
    queryFn: () => companyKnowledgeApi.getCompanyImportPreview(previewJobKey as string),
    enabled: Boolean(previewJobKey),
  });
  const legacyCandidatesQuery = useQuery({
    queryKey: ['company-knowledge-legacy-candidates'],
    queryFn: () => companyKnowledgeApi.listLegacyCandidates(),
    enabled: activeLane === 'intake',
  });
  const reviewsQuery = useQuery({
    queryKey: ['company-knowledge-review-queue'],
    queryFn: () => companyKnowledgeApi.listReviews(),
  });
  const accessQuery = useQuery({
    queryKey: ['company-knowledge-access-rules'],
    queryFn: () => companyKnowledgeApi.listAccessRules(),
    enabled: activeLane === 'access',
  });
  const publicationsQuery = useQuery({
    queryKey: ['company-knowledge-publication-lifecycle'],
    queryFn: () => companyKnowledgeApi.listPublicationLifecycle(),
    enabled: activeLane === 'lifecycle',
  });
  const ontologyQuery = useQuery({
    queryKey: ['company-knowledge-ontology-status'],
    queryFn: () => companyKnowledgeApi.getOntologyStatus(),
    enabled: activeLane === 'lifecycle',
  });
  const membersQuery = useQuery({
    queryKey: ['company-knowledge-audience-members'],
    queryFn: () => enterpriseApi.getOrgMembers(),
    enabled: activeLane === 'access',
  });
  const agentsQuery = useQuery({
    queryKey: ['company-knowledge-audience-agents'],
    queryFn: () => agentApi.list(),
    enabled: activeLane === 'access',
  });

  const reviews = reviewsQuery.data ?? [];
  const selectedReview = reviews.find((review) => review.reviewKey === selectedReviewKey) ?? null;
  const workspaceQuery = useQuery({
    queryKey: [
      'company-knowledge-review-workspace',
      selectedReview?.reviewKey,
      selectedReview?.stateVersion,
    ],
    queryFn: () =>
      companyKnowledgeApi.getReviewWorkspace(
        selectedReview as CompanyKnowledgeReview,
        intakesQuery.data ?? [],
      ),
    enabled: Boolean(selectedReview),
  });

  useEffect(() => {
    if (!reviews.length) {
      setSelectedReviewKey(null);
      return;
    }
    if (!selectedReviewKey || !reviews.some((review) => review.reviewKey === selectedReviewKey)) {
      setSelectedReviewKey(reviews[0].reviewKey);
    }
  }, [reviews, selectedReviewKey]);

  const invalidateCompanyKnowledge = () => {
    void queryClient.invalidateQueries({ queryKey: ['company-knowledge-access-rules'] });
    void queryClient.invalidateQueries({ queryKey: ['company-knowledge-intakes'] });
    void queryClient.invalidateQueries({ queryKey: ['company-knowledge-review-queue'] });
    void queryClient.invalidateQueries({ queryKey: ['company-knowledge-review-workspace'] });
    void queryClient.invalidateQueries({ queryKey: ['company-knowledge-publication-lifecycle'] });
    void queryClient.invalidateQueries({ queryKey: ['company-knowledge-library'] });
  };

  // When an import job transitions to a terminal lifecycle state, the intake,
  // review, publication, and library read models refresh without a reload.
  const importTerminalSignature = importJobs
    .filter((job) => job.lifecycleStatus !== 'queued' && job.lifecycleStatus !== 'running')
    .map((job) => `${job.jobKey}:${job.lifecycleStatus}`)
    .sort()
    .join('|');
  const previousImportTerminalRef = useRef<string | null>(null);
  useEffect(() => {
    const previous = previousImportTerminalRef.current;
    previousImportTerminalRef.current = importTerminalSignature;
    if (previous === null || previous === importTerminalSignature) return;
    invalidateCompanyKnowledge();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [importTerminalSignature]);

  const uploadImportMutation = useMutation({
    mutationFn: (input: { file: File; contract: CompanySourceContractSummary; title: string; purpose: string }) =>
      companyKnowledgeApi.uploadCompanyImportFile(input.file, {
        source_contract_id: input.contract.contractKey,
        source_contract_version: input.contract.version,
        title: input.title,
        proposed_namespace: input.contract.allowedNamespaces[0] ?? 'company/general',
        proposed_sensitivity: 'internal',
        purpose: input.purpose,
        idempotency_key: `company-import-${crypto.randomUUID()}`,
      }),
    onMutate: () => setImportActionError(null),
    onSuccess: () => {
      setImportActionError(null);
      void queryClient.invalidateQueries({ queryKey: ['company-knowledge-import-jobs'] });
    },
    onError: (error) => setImportActionError(companyImportActionErrorCode(error)),
  });
  const createContractMutation = useMutation({
    mutationFn: (input: { stable_source_id: string; allowed_namespaces: string[]; default_sensitivity: string }) =>
      companyKnowledgeApi.createSourceContract({
        stable_source_id: input.stable_source_id,
        accountable_steward_ref: 'role:org_admin',
        allowed_namespaces: input.allowed_namespaces,
        default_sensitivity: input.default_sensitivity,
      }),
    onMutate: () => setImportActionError(null),
    onSuccess: (contract) => {
      setImportActionError(null);
      setImportSelectedContractKey(contract.contractKey);
      void queryClient.invalidateQueries({ queryKey: ['company-knowledge-source-contracts'] });
    },
    onError: (error) => setImportActionError(companyImportActionErrorCode(error)),
  });
  const retryImportJobMutation = useMutation({
    mutationFn: (jobKey: string) => companyKnowledgeApi.retryCompanyImportJob(jobKey),
    onMutate: () => setImportActionError(null),
    onSuccess: () => {
      setImportBusyJobKey(null);
      setImportActionError(null);
      void queryClient.invalidateQueries({ queryKey: ['company-knowledge-import-jobs'] });
    },
    onError: (error) => {
      setImportBusyJobKey(null);
      setImportActionError(companyImportActionErrorCode(error));
    },
  });
  const cancelImportJobMutation = useMutation({
    mutationFn: (jobKey: string) => companyKnowledgeApi.cancelCompanyImportJob(jobKey),
    onMutate: () => setImportActionError(null),
    onSuccess: () => {
      setImportBusyJobKey(null);
      setImportActionError(null);
      void queryClient.invalidateQueries({ queryKey: ['company-knowledge-import-jobs'] });
    },
    onError: (error) => {
      setImportBusyJobKey(null);
      setImportActionError(companyImportActionErrorCode(error));
    },
  });
  const createProposalMutation = useMutation({
    mutationFn: (jobKey: string) => companyKnowledgeApi.createProposalFromImport(jobKey),
    onMutate: () => setImportActionError(null),
    onSuccess: () => {
      setImportBusyJobKey(null);
      setImportActionError(null);
      setPreviewJobKey(null);
      invalidateCompanyKnowledge();
      void queryClient.invalidateQueries({ queryKey: ['company-knowledge-import-jobs'] });
    },
    onError: (error) => {
      setImportBusyJobKey(null);
      setImportActionError(companyImportActionErrorCode(error));
    },
  });

  const legacyMutation = useMutation({
    mutationFn: companyKnowledgeApi.submitLegacy,
    onSuccess: () => {
      invalidateCompanyKnowledge();
      void queryClient.invalidateQueries({ queryKey: ['company-knowledge-legacy-candidates'] });
    },
  });
  const retryMutation = useMutation({
    mutationFn: (intakeKey: string) => companyKnowledgeApi.retryIntake(intakeKey),
    onSuccess: () => {
      setRetryingKey(null);
      invalidateCompanyKnowledge();
    },
    onError: () => setRetryingKey(null),
  });
  const materializeMutation = useMutation({
    mutationFn: ({
      workspace,
      title,
      markdown,
    }: {
      workspace: CompanyKnowledgeReviewWorkspace;
      title: string;
      markdown: string;
    }) => companyKnowledgeApi.materializeReview(workspace, { title, markdown }),
    onSuccess: invalidateCompanyKnowledge,
  });
  const decisionMutation = useMutation({
    mutationFn: ({
      workspace,
      decision,
      reason,
    }: {
      workspace: CompanyKnowledgeReviewWorkspace;
      decision: 'approve' | 'reject' | 'request_changes';
      reason: string;
    }) => companyKnowledgeApi.decideReview(workspace, decision, reason),
    onSuccess: invalidateCompanyKnowledge,
  });
  const publishMutation = useMutation({
    mutationFn: (workspace: CompanyKnowledgeReviewWorkspace) =>
      companyKnowledgeApi.publishReview(workspace),
    onSuccess: invalidateCompanyKnowledge,
  });
  const grantMutation = useMutation({
    mutationFn: companyKnowledgeApi.grantAccess,
    onSuccess: invalidateCompanyKnowledge,
  });
  const revokeMutation = useMutation({
    mutationFn: ({ permissionKey, reason }: { permissionKey: string; reason: string }) =>
      companyKnowledgeApi.revokeAccess(permissionKey, reason),
    onSuccess: () => {
      setRevokingKey(null);
      setAccessReason('');
      invalidateCompanyKnowledge();
    },
    onError: () => setRevokingKey(null),
  });
  const lifecycleMutation = useMutation({
    mutationFn: ({
      publication,
      reason,
    }: {
      publication: CompanyKnowledgePublicationLifecycle;
      reason: string;
    }) =>
      publication.availableAction === 'restore'
        ? companyKnowledgeApi.restorePublication(publication.publicationKey, reason)
        : companyKnowledgeApi.retirePublication(publication.publicationKey, reason),
    onSuccess: () => {
      setLifecycleBusyKey(null);
      setLifecycleReason('');
      invalidateCompanyKnowledge();
    },
    onError: () => setLifecycleBusyKey(null),
  });

  const audiences = useMemo<CompanyKnowledgeAudience[]>(() => {
    const roles: CompanyKnowledgeAudience[] = [
      { kind: 'role', key: 'role:member', label: t('companyKnowledge.audiences.allEmployees', 'All employees') },
      {
        kind: 'role',
        key: 'role:org_admin',
        label: t('companyKnowledge.audiences.companyAdmins', 'Company administrators'),
      },
      {
        kind: 'role',
        key: 'role:platform_admin',
        label: t('companyKnowledge.audiences.platformAdmins', 'Platform administrators'),
      },
    ];
    const members = (membersQuery.data ?? []).map((member) => ({
      kind: 'user' as const,
      key: member.id,
      label: member.name || member.email || t('companyKnowledge.audiences.member', 'Company member'),
    }));
    const agents = (agentsQuery.data ?? []).map((agent) => ({
      kind: 'agent' as const,
      key: agent.id,
      label: agent.name,
    }));
    return [...roles, ...members, ...agents];
  }, [agentsQuery.data, membersQuery.data, t]);

  const lanes: Array<{ key: ControlLane; label: string; icon: ReactNode }> = [
    { key: 'intake', label: t('companyKnowledge.tabs.intake', 'Intake & recovery'), icon: <IconDatabase size={15} /> },
    { key: 'review', label: t('companyKnowledge.tabs.review', 'Review & publish'), icon: <IconShieldCheck size={15} /> },
    { key: 'access', label: t('companyKnowledge.tabs.access', 'Access'), icon: <IconUsers size={15} /> },
    { key: 'lifecycle', label: t('companyKnowledge.tabs.lifecycle', 'Library & models'), icon: <IconBook size={15} /> },
  ];

  return (
    <div className="company-control-page">
      <header className="company-control-hero">
        <div>
          <span className="workbench-eyebrow">
            {t('companyKnowledge.controlEyebrow', 'Company operating console')}
          </span>
          <h1>{t('companyKnowledge.controlTitle', 'Company Knowledge')}</h1>
          <p>
            {t(
              'companyKnowledge.controlSubtitle',
              'Move reviewed evidence into the company library, decide what becomes official, and manage who can use it.',
            )}
          </p>
        </div>
        <IconBuilding size={24} stroke={1.6} />
      </header>

      <div className="company-control-links">
        <Link to="/knowledge/company" className="btn btn-secondary btn-sm">
          {t('companyKnowledge.openLibrary', 'Open Company Library')}
        </Link>
        <Link to="/knowledge" className="btn btn-secondary btn-sm">
          {t('companyKnowledge.openPersonal', 'Open Personal Knowledge')}
        </Link>
      </div>

      <nav className="company-control-tabs" aria-label={t('companyKnowledge.controlNav', 'Company Knowledge sections')}>
        {lanes.map((lane) => (
          <button
            key={lane.key}
            type="button"
            className={activeLane === lane.key ? 'active' : ''}
            aria-current={activeLane === lane.key ? 'page' : undefined}
            onClick={() => setActiveLane(lane.key)}
          >
            {lane.icon}
            {lane.label}
          </button>
        ))}
      </nav>

      {activeLane === 'intake' && (
        <div className="company-control-grid">
          <DirectImportWizard
            contracts={sourceContractsQuery.data ?? []}
            contractsState={
              sourceContractsQuery.isError ? 'error' : sourceContractsQuery.isPending ? 'loading' : 'ready'
            }
            jobs={importJobs}
            jobsState={importJobsQuery.isError ? 'error' : importJobsQuery.isPending ? 'loading' : 'ready'}
            uploading={uploadImportMutation.isPending || createContractMutation.isPending}
            actionError={importActionError}
            busyJobKey={importBusyJobKey}
            previewJobKey={previewJobKey}
            preview={previewQuery.data ?? null}
            previewState={
              !previewJobKey ? 'idle' : previewQuery.isError ? 'error' : previewQuery.isPending ? 'loading' : 'ready'
            }
            onUpload={(input) => uploadImportMutation.mutate(input)}
            onSelectContract={setImportSelectedContractKey}
            onCreateContract={(input) => createContractMutation.mutate(input)}
            onRetryJob={(jobKey) => {
              setImportBusyJobKey(jobKey);
              retryImportJobMutation.mutate(jobKey);
            }}
            onCancelJob={(jobKey) => {
              setImportBusyJobKey(jobKey);
              cancelImportJobMutation.mutate(jobKey);
            }}
            onPreview={(jobKey) => setPreviewJobKey(jobKey)}
            onCreateProposal={(jobKey) => {
              setImportBusyJobKey(jobKey);
              createProposalMutation.mutate(jobKey);
            }}
            onRetryContracts={() => void sourceContractsQuery.refetch()}
            onRetryJobs={() => void importJobsQuery.refetch()}
            onRetryPreview={() => void previewQuery.refetch()}
          />
          <section className="company-control-panel">
            <div className="company-control-panel-head">
              <div>
                <h2>{t('companyKnowledge.personalSubmissionTitle', 'Personal Knowledge submissions')}</h2>
                <p>
                  {t(
                    'companyKnowledge.personalSubmissionDescription',
                    'Owners start these requests from the Personal Knowledge item they want the company to review.',
                  )}
                </p>
              </div>
              <Link to="/knowledge" className="btn btn-secondary btn-sm">
                {t('companyKnowledge.choosePersonalItem', 'Choose a Personal item')}
              </Link>
            </div>
            {intakesQuery.isError ? (
              <SectionError onRetry={() => void intakesQuery.refetch()} />
            ) : (
              <IntakeQueueView
                intakes={intakesQuery.data ?? []}
                retryingKey={retryingKey}
                onRetry={(intakeKey) => {
                  setRetryingKey(intakeKey);
                  retryMutation.mutate(intakeKey);
                }}
              />
            )}
            {retryMutation.isError && <ActionUnconfirmed />}
          </section>
          <section className="company-control-panel">
            {legacyCandidatesQuery.isError ? (
              <SectionError onRetry={() => void legacyCandidatesQuery.refetch()} />
            ) : (
              <LegacyIntakeForm
                candidates={legacyCandidatesQuery.data?.candidates ?? []}
                pending={legacyMutation.isPending}
                error={legacyMutation.error}
                onSubmit={(input) => legacyMutation.mutate(input)}
              />
            )}
          </section>
        </div>
      )}

      {activeLane === 'review' && (
        <div className="company-control-review-grid">
          <section className="company-control-panel">
            <div className="company-control-panel-head">
              <div>
                <h2>{t('companyKnowledge.reviewQueueTitle', 'Review queue')}</h2>
                <p>
                  {t(
                    'companyKnowledge.reviewQueueDescription',
                    'Only items you are currently authorized to review appear here.',
                  )}
                </p>
              </div>
            </div>
            {reviewsQuery.isError ? (
              <SectionError onRetry={() => void reviewsQuery.refetch()} />
            ) : (
              <ReviewQueueView
                reviews={reviews}
                selectedKey={selectedReviewKey}
                onSelect={setSelectedReviewKey}
              />
            )}
          </section>
          <section className="company-control-panel">
            <ReviewWorkspace
              workspace={workspaceQuery.data ?? null}
              loading={workspaceQuery.isLoading}
              error={workspaceQuery.isError ? workspaceQuery.error : null}
              actionError={
                materializeMutation.error || decisionMutation.error || publishMutation.error
              }
              busy={
                materializeMutation.isPending ||
                decisionMutation.isPending ||
                publishMutation.isPending
              }
              onMaterialize={(title, markdown) => {
                if (workspaceQuery.data) {
                  materializeMutation.mutate({ workspace: workspaceQuery.data, title, markdown });
                }
              }}
              onDecision={(decision, reason) => {
                if (workspaceQuery.data) {
                  decisionMutation.mutate({ workspace: workspaceQuery.data, decision, reason });
                }
              }}
              onPublish={() => {
                if (workspaceQuery.data) publishMutation.mutate(workspaceQuery.data);
              }}
            />
          </section>
        </div>
      )}

      {activeLane === 'access' && (
        <div className="company-control-grid">
          <section className="company-control-panel">
            <div className="company-control-panel-head">
              <div>
                <h2>{t('companyKnowledge.accessTitle', 'Company Knowledge access')}</h2>
                <p>
                  {t(
                    'companyKnowledge.accessDescription',
                    'Assign business capabilities to company roles, members, or digital employees.',
                  )}
                </p>
              </div>
            </div>
            <AccessGrantForm
              audiences={audiences}
              pending={grantMutation.isPending}
              error={grantMutation.error}
              onSubmit={(input) => grantMutation.mutate(input)}
            />
          </section>
          <section className="company-control-panel">
            <label className="company-control-reason">
              <span>{t('companyKnowledge.removeAccessReason', 'Reason for removing access')}</span>
              <input
                value={accessReason}
                maxLength={1000}
                onChange={(event) => setAccessReason(event.target.value)}
              />
            </label>
            {accessQuery.isError ? (
              <SectionError onRetry={() => void accessQuery.refetch()} />
            ) : (
              <AccessRulesView
                rules={accessQuery.data ?? []}
                revokingKey={revokingKey}
                actionReady={Boolean(accessReason.trim())}
                onRevoke={(permissionKey) => {
                  if (!accessReason.trim()) return;
                  setRevokingKey(permissionKey);
                  revokeMutation.mutate({ permissionKey, reason: accessReason });
                }}
              />
            )}
            {revokeMutation.isError && <ActionUnconfirmed />}
          </section>
        </div>
      )}

      {activeLane === 'lifecycle' && (
        <div className="company-control-grid">
          <section className="company-control-panel">
            <div className="company-control-panel-head">
              <div>
                <h2>{t('companyKnowledge.lifecycleTitle', 'Published knowledge lifecycle')}</h2>
                <p>
                  {t(
                    'companyKnowledge.lifecycleDescription',
                    'Retire outdated guidance or restore a reviewed version. Both actions remain recoverable and audited.',
                  )}
                </p>
              </div>
            </div>
            <label className="company-control-reason">
              <span>{t('companyKnowledge.lifecycleReason', 'Reason for this lifecycle change')}</span>
              <input
                value={lifecycleReason}
                maxLength={1000}
                onChange={(event) => setLifecycleReason(event.target.value)}
              />
            </label>
            {publicationsQuery.isError ? (
              <SectionError onRetry={() => void publicationsQuery.refetch()} />
            ) : (
              <KnowledgeLifecycleView
                publications={publicationsQuery.data ?? []}
                busyKey={lifecycleBusyKey}
                actionReady={Boolean(lifecycleReason.trim())}
                onLifecycleAction={(publication) => {
                  if (!lifecycleReason.trim()) return;
                  setLifecycleBusyKey(publication.publicationKey);
                  lifecycleMutation.mutate({ publication, reason: lifecycleReason });
                }}
              />
            )}
            {lifecycleMutation.isError && <ActionUnconfirmed />}
          </section>
          <section className="company-control-panel">
            <div className="company-control-panel-head">
              <div>
                <h2>{t('companyKnowledge.ontologyTitle', 'Company model status')}</h2>
                <p>
                  {t(
                    'companyKnowledge.ontologyDescription',
                    'Current domain packs and releases used for governed company concepts.',
                  )}
                </p>
              </div>
            </div>
            {ontologyQuery.isError ? (
              <SectionError onRetry={() => void ontologyQuery.refetch()} />
            ) : (
              <OntologyStatusView status={ontologyQuery.data ?? null} />
            )}
          </section>
        </div>
      )}
    </div>
  );
}
