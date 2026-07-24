import { useState, type FormEvent } from 'react';
import { useMutation } from '@tanstack/react-query';
import { useTranslation } from 'react-i18next';
import { IconBuilding, IconCheck } from '@tabler/icons-react';

import {
  companyKnowledgeApi,
  type CompanyKnowledgeArea,
} from '../../api/domains/companyKnowledge';
import './PersonalKnowledgePromotionCard.css';

interface PersonalKnowledgePromotionFormProps {
  title: string;
  area: CompanyKnowledgeArea;
  purpose: string;
  attested: boolean;
  pending: boolean;
  error: unknown;
  onTitleChange: (value: string) => void;
  onAreaChange: (value: CompanyKnowledgeArea) => void;
  onPurposeChange: (value: string) => void;
  onAttestedChange: (value: boolean) => void;
  onSubmit: (event: FormEvent) => void;
  onCancel: () => void;
}

export function PersonalKnowledgePromotionForm({
  title,
  area,
  purpose,
  attested,
  pending,
  error,
  onTitleChange,
  onAreaChange,
  onPurposeChange,
  onAttestedChange,
  onSubmit,
  onCancel,
}: PersonalKnowledgePromotionFormProps) {
  const { t } = useTranslation();
  const canSubmit = attested && purpose.trim().length > 0 && title.trim().length > 0 && !pending;

  return (
    <form className="personal-kb-company-form" onSubmit={onSubmit}>
      <div className="personal-kb-company-form-copy">
        <strong>{t('personalKnowledge.companyReviewTitle', 'Submit to Company review')}</strong>
        <small>
          {t(
            'personalKnowledge.companyReviewDescription',
            'This creates a review request. It does not publish or share this item automatically.',
          )}
        </small>
      </div>
      <label>
        <span>{t('personalKnowledge.companyReviewDocumentTitle', 'Company title')}</span>
        <input
          value={title}
          maxLength={300}
          onChange={(event) => onTitleChange(event.target.value)}
        />
      </label>
      <label>
        <span>{t('personalKnowledge.companyReviewArea', 'Company area')}</span>
        <select
          value={area}
          onChange={(event) => onAreaChange(event.target.value as CompanyKnowledgeArea)}
        >
          <option value="general">{t('companyKnowledge.areas.general', 'General')}</option>
          <option value="policies">{t('companyKnowledge.areas.policies', 'Policies')}</option>
          <option value="team_notes">{t('companyKnowledge.areas.teamNotes', 'Team notes')}</option>
          <option value="playbooks">{t('companyKnowledge.areas.playbooks', 'Playbooks')}</option>
          <option value="operations">{t('companyKnowledge.areas.operations', 'Operations')}</option>
        </select>
      </label>
      <label>
        <span>{t('personalKnowledge.companyReviewPurpose', 'Why should the company review this?')}</span>
        <textarea
          value={purpose}
          maxLength={1000}
          rows={3}
          onChange={(event) => onPurposeChange(event.target.value)}
        />
      </label>
      <label className="personal-kb-company-attestation">
        <input
          type="checkbox"
          checked={attested}
          onChange={(event) => onAttestedChange(event.target.checked)}
        />
        <span>
          {t(
            'personalKnowledge.companyReviewAttestation',
            'I confirm this Personal Knowledge item may enter Company review.',
          )}
        </span>
      </label>
      {Boolean(error) && (
        <div role="alert" className="personal-kb-company-error">
          {t(
            'personalKnowledge.companyReviewError',
            'The review request could not be submitted. Your Personal Knowledge item was not changed.',
          )}
        </div>
      )}
      <div className="personal-kb-company-form-actions">
        <button type="button" className="btn btn-secondary btn-sm" onClick={onCancel}>
          {t('common.cancel', 'Cancel')}
        </button>
        <button type="submit" className="btn btn-primary btn-sm" disabled={!canSubmit}>
          {pending
            ? t('personalKnowledge.companyReviewSubmitting', 'Submitting...')
            : t('personalKnowledge.companyReviewSubmit', 'Submit to Company review')}
        </button>
      </div>
    </form>
  );
}

export default function PersonalKnowledgePromotionCard({
  documentKey,
  documentTitle,
}: {
  documentKey: string;
  documentTitle: string;
}) {
  const { t } = useTranslation();
  const [open, setOpen] = useState(false);
  const [submitted, setSubmitted] = useState(false);
  const [title, setTitle] = useState(documentTitle);
  const [area, setArea] = useState<CompanyKnowledgeArea>('general');
  const [purpose, setPurpose] = useState('');
  const [attested, setAttested] = useState(false);
  const mutation = useMutation({
    mutationFn: () =>
      companyKnowledgeApi.submitPersonal({
        documentKey,
        title,
        area,
        purpose,
      }),
    onSuccess: () => {
      setSubmitted(true);
      setOpen(false);
      setPurpose('');
      setAttested(false);
    },
  });

  if (open) {
    return (
      <PersonalKnowledgePromotionForm
        title={title}
        area={area}
        purpose={purpose}
        attested={attested}
        pending={mutation.isPending}
        error={mutation.error}
        onTitleChange={setTitle}
        onAreaChange={setArea}
        onPurposeChange={setPurpose}
        onAttestedChange={setAttested}
        onSubmit={(event) => {
          event.preventDefault();
          if (attested && title.trim() && purpose.trim()) mutation.mutate();
        }}
        onCancel={() => setOpen(false)}
      />
    );
  }

  return (
    <div className="personal-kb-company-entry">
      <button
        type="button"
        className="btn btn-secondary btn-sm"
        onClick={() => {
          setTitle(documentTitle);
          setSubmitted(false);
          setOpen(true);
        }}
      >
        <IconBuilding size={14} stroke={1.7} />
        {t('personalKnowledge.companyReviewSubmit', 'Submit to Company review')}
      </button>
      {submitted && (
        <small className="personal-kb-company-success" role="status">
          <IconCheck size={13} stroke={1.8} />
          {t(
            'personalKnowledge.companyReviewQueued',
            'Submitted for review. Nothing has been published yet.',
          )}
        </small>
      )}
    </div>
  );
}
