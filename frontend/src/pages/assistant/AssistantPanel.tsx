import { useState } from 'react';
import { Link } from 'react-router-dom';
import { useTranslation } from 'react-i18next';
import type { AssistantBinding } from '../../api/domains/assistant';
import './assistant.css';

interface AssistantPanelProps {
  payload: AssistantBinding | null;
  loading: boolean;
  errorState?: string | null;
  actionError?: string | null;
  busy?: boolean;
  onOpen: (replaceUnavailable: boolean) => void;
  onSelect: (agentId: string) => void;
  onRetry: () => void;
}

function stateText(state: string, t: (key: string) => string): string {
  const keys: Record<string, string> = {
    unbound: 'empty', model_unconfigured: 'modelMissing', model_forbidden: 'modelForbidden',
    provision_incomplete: 'pending', employee_deleted: 'deleted', employee_inactive: 'inactive',
    transferred: 'transferred', inaccessible: 'inaccessible', ineligible: 'inaccessible', denied: 'denied',
    cross_company: 'crossCompany', worker_unavailable: 'workerUnavailable', worker_unknown: 'workerUnknown',
    active: 'bound', company_required: 'companyRequired', scope_changed: 'scopeChanged', legacy_unresolved: 'legacyUnresolved',
  };
  return t(`assistant.${keys[state] || 'unavailable'}`);
}

/** Explicit assistant settings; never shown on the conversation landing. */
export default function AssistantPanel({
  payload,
  loading,
  errorState,
  actionError,
  busy = false,
  onOpen,
  onSelect,
  onRetry,
}: AssistantPanelProps) {
  const { t } = useTranslation();
  const [chosen, setChosen] = useState<string | null>(null);
  const state = errorState || payload?.state || (loading ? 'loading' : 'unbound');
  const replaceStates = new Set(['employee_deleted', 'employee_inactive', 'inaccessible', 'cross_company']);

  return (
    <section className="assistant-entry" aria-labelledby="assistant-title">
      <header className="page-header">
        <h1 id="assistant-title">{t('assistant.title')}</h1>
      </header>
      <div className="assistant-card" role="status" aria-live="polite" data-assistant-state={state}>
        {loading ? <p>{t('assistant.loading')}</p> : <p>{stateText(state, t)}</p>}
        {payload?.agent ? (
          <p>
            <span>{t('assistant.identity')}</span>
            {' '}
            <strong>{payload.agent.name}</strong>
          </p>
        ) : null}
      </div>
      {actionError ? <p role="alert">{actionError}</p> : null}
      {payload && payload.candidates.length > 0 ? (
        <fieldset className="assistant-candidates">
          <legend>{t('assistant.chooseAnother')}</legend>
          {payload.candidates.map((candidate) => (
            <label key={candidate.id}>
              <input
                type="radio"
                name="assistant-candidate"
                value={candidate.id}
                checked={chosen === candidate.id}
                onChange={() => setChosen(candidate.id)}
              />
              <span>{candidate.name}</span>
            </label>
          ))}
        </fieldset>
      ) : null}
      <div className="assistant-actions">
        {state === 'unbound' || state === 'ineligible' ? (
          <button className="btn btn-primary" type="button" disabled={busy} onClick={() => onOpen(false)}>
            {t('assistant.open')}
          </button>
        ) : null}
        {replaceStates.has(state) ? (
          <button className="btn btn-primary" type="button" disabled={busy} onClick={() => onOpen(true)}>
            {t('assistant.open')}
          </button>
        ) : null}
        <button className="btn btn-secondary" type="button" disabled={busy || !chosen} onClick={() => chosen && onSelect(chosen)}>
          {t('assistant.select')}
        </button>
        <button className="btn btn-secondary" type="button" disabled={busy} onClick={onRetry}>
          {t('assistant.retry')}
        </button>
        {payload?.entry.kind === 'session' || payload?.entry.kind === 'employee' ? (
          <Link className="btn btn-secondary" to={`/agents/${payload.entry.agent_id}#chat`}>{t('assistant.continue')}</Link>
        ) : null}
      </div>
    </section>
  );
}
