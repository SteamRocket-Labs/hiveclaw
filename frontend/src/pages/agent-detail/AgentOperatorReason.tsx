import React from 'react';
import { useTranslation } from 'react-i18next';

export default function AgentOperatorReason({
  value,
  onChange,
}: {
  value: string;
  onChange: (value: string) => void;
}) {
  const { t } = useTranslation();
  const [draft, setDraft] = React.useState(value);
  React.useEffect(() => setDraft(value), [value]);
  const normalizedDraft = draft.trim();
  return (
    <form
      className="agent-detail-operator-reason"
      data-testid="agent-operator-reason"
      onSubmit={(event) => {
        event.preventDefault();
        if (!normalizedDraft || normalizedDraft === value) return;
        onChange(normalizedDraft);
      }}
    >
      <label htmlFor="agent-operator-reason-input">
        {t('agent.operator.reason', 'Operator inspection reason')}
      </label>
      <input
        id="agent-operator-reason-input"
        className="input"
        value={draft}
        onChange={(event) => setDraft(event.target.value)}
        maxLength={1000}
        placeholder={t('agent.operator.reasonPlaceholder', 'Enter the incident or review reason before inspecting another user’s data')}
        autoComplete="off"
      />
      <button
        type="submit"
        className="btn btn-secondary"
        disabled={!normalizedDraft || normalizedDraft === value}
      >
        {value
          ? t('agent.operator.applyReason', 'Apply inspection reason')
          : t('agent.operator.beginInspection', 'Begin inspection')}
      </button>
      {value ? (
        <button
          type="button"
          className="btn btn-secondary"
          onClick={() => {
            setDraft('');
            onChange('');
          }}
        >
          {t('agent.operator.endInspection', 'End inspection')}
        </button>
      ) : null}
      <span>{t('agent.operator.readOnly', 'Operator inspection is audited and read-only.')}</span>
    </form>
  );
}
