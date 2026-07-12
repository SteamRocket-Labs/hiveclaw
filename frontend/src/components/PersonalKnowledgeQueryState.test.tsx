import { renderToStaticMarkup } from 'react-dom/server';
import { describe, expect, it, vi } from 'vitest';

import { ApiError } from '../api/core';
import PersonalKnowledgeQueryState, {
  classifyPersonalKnowledgeQueryError,
} from './PersonalKnowledgeQueryState';

vi.mock('react-i18next', () => ({
  useTranslation: () => ({
    t: (_key: string, fallback?: string) => fallback ?? _key,
  }),
}));

describe('PersonalKnowledgeQueryState', () => {
  it('classifies 403 as an authority denial instead of an empty collection', () => {
    const error = new ApiError(403, 'Forbidden');

    expect(classifyPersonalKnowledgeQueryError(error)).toBe('forbidden');
    const html = renderToStaticMarkup(<PersonalKnowledgeQueryState error={error} onRetry={vi.fn()} />);

    expect(html).toContain('role="alert"');
    expect(html).toContain('data-personal-knowledge-state="forbidden"');
    expect(html).toContain('Personal Knowledge access denied');
    expect(html).toContain('This is not an empty knowledge base');
    expect(html).not.toContain('Personal KB is empty');
  });

  it('keeps operational failures distinct from both denial and a true empty state', () => {
    const error = new ApiError(503, 'Provider unavailable');

    expect(classifyPersonalKnowledgeQueryError(error)).toBe('unavailable');
    const html = renderToStaticMarkup(<PersonalKnowledgeQueryState error={error} onRetry={vi.fn()} />);

    expect(html).toContain('data-personal-knowledge-state="unavailable"');
    expect(html).toContain('Personal Knowledge is temporarily unavailable');
    expect(html).toContain('Retry');
  });
});
