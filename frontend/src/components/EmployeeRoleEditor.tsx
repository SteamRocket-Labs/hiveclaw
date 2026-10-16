import { useEffect, useId, useRef, useState } from 'react';
import { useTranslation } from 'react-i18next';
import { agentApi, type EmployeeRoleSettings } from '../api/domains/agents';

export default function EmployeeRoleEditor({ agentId, value, onSaved, onCancel, saveLabel, onBusyChange }: {
  agentId: string; value: EmployeeRoleSettings;
  onSaved: (saved: EmployeeRoleSettings) => void | Promise<unknown>;
  onCancel?: () => void; saveLabel?: string; onBusyChange?: (busy: boolean) => void;
}) {
  const { t } = useTranslation();
  const id = useId();
  const active = useRef(true);
  const [base, setBase] = useState(value);
  const [draft, setDraft] = useState(value.role_description);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const [reviewed, setReviewed] = useState(false);
  useEffect(() => { active.current = true; return () => { active.current = false; }; }, []);
  const readback = async () => {
    const current = await agentApi.getById(agentId);
    if (!current.role_revision) throw new Error(t('agent.roleSettings.unavailable'));
    return { role_description: current.role_description, role_revision: current.role_revision };
  };
  const act = async (operation: () => Promise<unknown>) => {
    if (busy) return;
    setBusy(true); onBusyChange?.(true); setError('');
    try { await operation(); }
    catch (failure) { if (active.current) setError(failure instanceof Error ? failure.message : t('agent.roleSettings.failed')); }
    finally { if (active.current) setBusy(false); onBusyChange?.(false); }
  };
  return <section aria-label={t('agent.roleSettings.title')}>
    <label htmlFor={id}>{t('agent.roleSettings.title')}</label>
    <textarea id={id} className="input" style={{ width: '100%' }} rows={5} maxLength={4000}
      value={draft} disabled={busy} onChange={(event) => setDraft(event.target.value)} />
    <p>{t('agent.roleSettings.scope')}</p>
    {error && <p role="alert">{error}</p>}
    {reviewed && base.role_description !== draft && <details>
      <summary>{t('agent.roleSettings.current')}</summary>
      <p style={{ whiteSpace: 'pre-wrap' }}>{base.role_description}</p>
      <button className="btn btn-secondary" type="button" disabled={busy} onClick={() => setDraft(base.role_description)}>{t('agent.roleSettings.useCurrent')}</button>
    </details>}
    <div style={{ display: 'flex', flexWrap: 'wrap', gap: 'var(--space-2)', marginTop: 'var(--space-3)' }}>
      <button className="btn btn-primary" type="button" disabled={busy || !base.role_revision} onClick={() => void act(async () => {
        let saved = base;
        if (draft !== base.role_description) {
          const result = await agentApi.update(agentId, { role_description: draft, expected_role_revision: base.role_revision });
          if (!result.role_revision) throw new Error(t('agent.roleSettings.unavailable'));
          saved = { role_description: result.role_description, role_revision: result.role_revision };
        }
        if (!active.current) return;
        setBase(saved); setReviewed(false);
        await onSaved(saved);
      })}>{saveLabel || t('common.save')}</button>
      <button className="btn btn-secondary" type="button" disabled={busy} onClick={() => void act(async () => {
        const current = await readback();
        if (active.current) { setBase(current); setReviewed(true); }
      })}>{t('agent.roleSettings.read')}</button>
      {onCancel && <button className="btn btn-secondary" type="button" disabled={busy} onClick={onCancel}>{t('common.cancel')}</button>}
    </div>
  </section>;
}
