import { useEffect, useRef, useState } from 'react';
import { useTranslation } from 'react-i18next';

import { enterpriseApi, type OriginalReference } from '../../api/domains/enterprise';

export default function OriginalReferenceList({ tenant, blobId, version, disabled, onRetire }: {
  tenant: string;
  blobId: string;
  version: number;
  disabled: boolean;
  onRetire: (reference: OriginalReference) => void;
}) {
  const { t, i18n } = useTranslation();
  const alive = useRef(true);
  const ticket = useRef(0);
  const [rows, setRows] = useState<OriginalReference[]>([]);
  const [cursor, setCursor] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState(false);
  async function load(after?: string) {
    const current = ++ticket.current;
    setLoading(true); setError(false);
    try {
      const result = await enterpriseApi.getOriginalReferences(tenant, blobId, after);
      if (!alive.current || current !== ticket.current) return;
      setRows(previous => after ? [...previous, ...result.items] : result.items); setCursor(result.next_cursor);
    } catch { if (alive.current && current === ticket.current) setError(true); }
    finally { if (alive.current && current === ticket.current) setLoading(false); }
  }
  useEffect(() => {
    alive.current = true;
    return () => { alive.current = false; ticket.current += 1; };
  }, []);
  useEffect(() => {
    setRows([]); setCursor(null); void load();
    // The keyed instance owns one authenticated company/original.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [version]);
  const types: Record<string, string> = { chat_upload: t('enterprise.originals.refUpload', 'Uploaded original'),
    channel_upload: t('enterprise.originals.refChannel', 'Channel original'), session_input: t('enterprise.originals.refInput', 'Work input'),
    session_tool_read: t('enterprise.originals.refRead', 'Employee source read') };
  const reasons: Record<string, string> = { legal_hold: t('enterprise.originals.refLegalHold', 'Legal retention is active.'),
    retention_hold: t('enterprise.originals.refRetentionHold', 'The retention deadline has not passed.'),
    work_reference: t('enterprise.originals.refWorkHold', 'Related work or another source holding still needs this reference.'),
    source_active: t('enterprise.originals.refSourceActive', 'The source owner has not withdrawn future use.'),
    reference_reconciliation_required: t('enterprise.originals.refUnverified', 'The source or closed-work evidence needs reconciliation.') };
  const date = (value: string) => new Date(value).toLocaleString(i18n?.resolvedLanguage || i18n?.language);
  return <div aria-label={t('enterprise.originals.manageReferences', 'Retained references')}>
    <h5>{t('enterprise.originals.manageReferences', 'Retained references')}</h5>
    {loading ? <p role="status">{t('common.loading', 'Loading...')}</p> : null}
    {rows.map(row => <article key={row.reference_id}>
      <p>{types[row.owner_type] || t('enterprise.originals.refOther', 'Other retained reference')} · {date(row.created_at)}</p>
      <p>{row.retirable ? t('enterprise.originals.refRetirable', 'Verified for reference retirement.')
        : reasons[row.hold_reason || ''] || t('enterprise.originals.refUnverified', 'The source or closed-work evidence needs reconciliation.')}</p>
      {row.pinned_until ? <p>{t('enterprise.originals.refUntil', 'Retain until {{date}}', { date: date(row.pinned_until) })}</p> : null}
      {row.retirable ? <button type="button" className="btn" disabled={disabled || loading} onClick={() => onRetire(row)}>
        {t('enterprise.originals.retireReference', 'Retire this reference')}</button> : null}
    </article>)}
    {!rows.length && !loading && !error ? <p>{t('enterprise.originals.refEmpty', 'No retained references remain for this original.')}</p> : null}
    {error ? <p role="alert">{t('enterprise.originals.refLoadFailed', 'Could not verify retained references.')}</p> : null}
    <button type="button" className="btn" disabled={disabled || loading} onClick={() => void load()}>{t('common.refresh', 'Refresh')}</button>
    {cursor ? <button type="button" className="btn" disabled={disabled || loading} onClick={() => void load(cursor)}>{t('enterprise.originals.more')}</button> : null}
  </div>;
}
