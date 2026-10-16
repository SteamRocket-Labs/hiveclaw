import { useId, useState, type FormEvent } from 'react';
import { useTranslation } from 'react-i18next';
import type { AgentTool } from '../../api/domains/tools';
import { useAuthStore } from '../../stores';
import './ToolsManager.css';

export default function ToolConfigurationForm({ tool, onSave, onCancel, submitLabel }: {
  tool: AgentTool;
  onSave: (config: Record<string, unknown>) => Promise<void>;
  onCancel?: () => void;
  submitLabel?: string;
}) {
  const { t } = useTranslation();
  const formId = useId();
  const fields = tool.config_schema?.fields || [];
  const [data, setData] = useState<Record<string, any>>(() => ({ ...tool.global_config, ...tool.agent_config }));
  const [json, setJson] = useState(() => JSON.stringify(tool.agent_config || {}, null, 2));
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState('');
  const save = async (event: FormEvent) => {
    event.preventDefault();
    if (saving) return;
    setSaving(true);
    setError('');
    try {
      const hasSchema = !!tool.config_schema?.fields?.length;
      const payload = hasSchema ? { ...tool.agent_config } : JSON.parse(json || '{}');
      if (hasSchema) {
        const inherited = { ...tool.global_config, ...tool.agent_config };
        for (const [key, value] of Object.entries(data)) {
          if (!Object.is(value, inherited[key])) payload[key] = value;
        }
      }
      await onSave(payload);
    } catch (failure) {
      setError(failure instanceof Error ? failure.message : t('agent.configuration.saveFailed', 'Could not save configuration.'));
    } finally {
      setSaving(false);
    }
  };
  return <form onSubmit={(event) => void save(event)}>
    {fields.length > 0 ? (
      <div className="tools-manager-fields">
        {(fields as any[])
          .filter((field: any) => {
            if (!field.depends_on) return true;
            return Object.entries(field.depends_on).every(([dependencyKey, dependencyValues]: [string, any]) =>
              (dependencyValues as string[]).includes(data[dependencyKey] ?? ''),
            );
          })
          .map((field: any) => {
            const userFromStore = useAuthStore.getState().user;
            const currentUserRole = userFromStore?.role;
            const isReadOnly = field.read_only_for_roles?.includes(currentUserRole);
            return (
              <div key={field.key}>
                <label className="tools-manager-field-label" htmlFor={`${formId}-${field.key}`}>
                  {field.label}
                  {isReadOnly && <span className="tools-manager-field-note">(Admin only)</span>}
                </label>
                {field.type === 'password' ? (
                  <input
                    id={`${formId}-${field.key}`}
                    type="password"
                    autoComplete="new-password"
                    className="form-input"
                    value={data[field.key] ?? ''}
                    placeholder={field.placeholder || 'Leave blank to use global default'}
                    onChange={(event) => setData((previous) => ({ ...previous, [field.key]: event.target.value }))}
                  />
                ) : field.type === 'select' ? (
                  <select id={`${formId}-${field.key}`} className="form-input" value={data[field.key] ?? field.default ?? ''} onChange={(event) => setData((previous) => ({ ...previous, [field.key]: event.target.value }))}>
                    {(field.options || []).map((option: any) => (
                      <option key={option.value} value={option.value}>
                        {option.label}
                      </option>
                    ))}
                  </select>
                ) : field.type === 'number' ? (
                  <input
                    id={`${formId}-${field.key}`}
                    type="number"
                    className="form-input"
                    value={data[field.key] ?? field.default ?? ''}
                    placeholder={field.placeholder || ''}
                    min={field.min}
                    max={field.max}
                    onChange={(event) => setData((previous) => ({ ...previous, [field.key]: event.target.value ? Number(event.target.value) : '' }))}
                  />
                ) : (
                  <input
                    id={`${formId}-${field.key}`}
                    type="text"
                    className="form-input"
                    value={data[field.key] ?? ''}
                    placeholder={field.placeholder || 'Leave blank to use global default'}
                    onChange={(event) => setData((previous) => ({ ...previous, [field.key]: event.target.value }))}
                  />
                )}
              </div>
            );
          })}
      </div>
    ) : (
      <div>
        <label className="tools-manager-field-label" htmlFor={formId}>Config JSON (Agent Override)</label>
        <textarea
          id={formId}
          className="form-input tools-manager-json-input"
          value={json}
          onChange={(event) => setJson(event.target.value)}
          placeholder="{}"
        />
      </div>
    )}

    {error && <p role="alert">{error}</p>}
    <div className="tools-manager-modal-footer">
      {onCancel && <button type="button" className="btn btn-secondary" onClick={onCancel} disabled={saving}>{t('common.cancel', 'Cancel')}</button>}
      <button type="submit" className="btn btn-primary" disabled={saving}>
        {saving ? t('common.saving', 'Saving…') : submitLabel || t('common.save', 'Save')}
      </button>
    </div>
  </form>;
}
