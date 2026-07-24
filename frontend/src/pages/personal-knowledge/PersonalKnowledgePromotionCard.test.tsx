import { renderToStaticMarkup } from 'react-dom/server';
import { describe, expect, it, vi } from 'vitest';

vi.mock('react-i18next', () => ({
  useTranslation: () => ({
    t: (_key: string, fallback?: string) => fallback ?? _key,
  }),
}));

import { PersonalKnowledgePromotionForm } from './PersonalKnowledgePromotionCard';

describe('PersonalKnowledgePromotionForm', () => {
  it('requires an explicit Company scope-change confirmation without exposing implementation fields', () => {
    const markup = renderToStaticMarkup(
      <PersonalKnowledgePromotionForm
        title="Owner onboarding note"
        area="team_notes"
        purpose="Share the reviewed note with the company."
        attested={false}
        pending={false}
        error={null}
        onTitleChange={vi.fn()}
        onAreaChange={vi.fn()}
        onPurposeChange={vi.fn()}
        onAttestedChange={vi.fn()}
        onSubmit={vi.fn()}
        onCancel={vi.fn()}
      />,
    );

    expect(markup).toContain('Submit to Company review');
    expect(markup).toContain('This creates a review request');
    expect(markup).toContain('I confirm this Personal Knowledge item may enter Company review');
    expect(markup).toContain('disabled=""');
    for (const forbidden of [
      'document_id',
      'source_ref',
      'content_hash',
      'proposal_id',
      'job_id',
      'attest_scope_change',
      'company/team-notes',
    ]) {
      expect(markup).not.toContain(forbidden);
    }
  });
});
