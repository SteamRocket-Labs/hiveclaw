import { IconAlertTriangle, IconShieldOff } from '@tabler/icons-react';
import { useTranslation } from 'react-i18next';

import { ApiError } from '../api/core';
import EmptyState from './ui/EmptyState';
import './PersonalKnowledgeQueryState.css';

export type PersonalKnowledgeQueryErrorKind = 'forbidden' | 'unavailable';

export function classifyPersonalKnowledgeQueryError(error: unknown): PersonalKnowledgeQueryErrorKind {
  if (error instanceof ApiError && error.status === 403) return 'forbidden';
  if (
    error
    && typeof error === 'object'
    && 'status' in error
    && Number((error as { status?: unknown }).status) === 403
  ) {
    return 'forbidden';
  }
  return 'unavailable';
}

export default function PersonalKnowledgeQueryState({
  error,
  onRetry,
}: {
  error: unknown;
  onRetry: () => void;
}) {
  const { t } = useTranslation();
  const kind = classifyPersonalKnowledgeQueryError(error);
  const forbidden = kind === 'forbidden';

  return (
    <div
      className={`personal-knowledge-query-state is-${kind}`}
      role="alert"
      data-personal-knowledge-state={kind}
    >
      <EmptyState
        icon={forbidden ? <IconShieldOff size={24} stroke={1.6} /> : <IconAlertTriangle size={24} stroke={1.6} />}
        title={forbidden
          ? t('personalKnowledge.accessDeniedTitle', 'Personal Knowledge access denied')
          : t('personalKnowledge.unavailableTitle', 'Personal Knowledge is temporarily unavailable')}
        description={forbidden
          ? t(
            'personalKnowledge.accessDeniedBody',
            'This is not an empty knowledge base. Your account or this Agent does not have permission to open this personal library; ask the owner or an administrator to grant access.',
          )
          : t(
            'personalKnowledge.unavailableBody',
            'The knowledge service could not be loaded. No empty-state conclusion was made; retry to restore the authoritative result.',
          )}
        action={(
          <button type="button" className="btn btn-secondary" onClick={onRetry}>
            {t('personalKnowledge.retryQuery', 'Retry')}
          </button>
        )}
      />
    </div>
  );
}
