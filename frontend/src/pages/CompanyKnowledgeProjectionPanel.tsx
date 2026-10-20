import { useEffect, useState } from 'react';
import { useMutation, useQuery } from '@tanstack/react-query';
import { useTranslation } from 'react-i18next';
import { companyKnowledgeApi, type CompanyProjectionMapping } from '../api/domains/companyKnowledge';

function ProjectionDetails({ publicationKey }: { publicationKey: string }) {
  const { t } = useTranslation();
  const [activationKey, setActivationKey] = useState('');
  const [mapping, setMapping] = useState<CompanyProjectionMapping>({ entity_types: {}, property_refs: {}, link_types: {} });
  const [reason, setReason] = useState('');
  const query = useQuery({
    queryKey: ['company-knowledge-projection', publicationKey],
    queryFn: () => companyKnowledgeApi.getProjection(publicationKey),
  });
  const view = query.data;
  const schema = view?.schemas.find((item) => item.activation_id === activationKey);
  useEffect(() => {
    setMapping({ entity_types: {}, property_refs: {}, link_types: {} });
    setReason('');
  }, [activationKey, view?.candidate_hash]);
  const backfill = useMutation({
    mutationFn: () => companyKnowledgeApi.backfillProjection(publicationKey),
    onSuccess: () => { void query.refetch(); },
  });
  const confirm = useMutation({
    mutationFn: () => {
      if (!view || !schema || !reason.trim()) throw new Error('projection_confirmation_incomplete');
      return companyKnowledgeApi.confirmProjection(view, schema.activation_id, mapping, reason);
    },
    onSuccess: () => { void query.refetch(); },
  });
  const busy = backfill.isPending || confirm.isPending;
  const candidate = view?.candidate;
  const groups: Array<{ kind: keyof CompanyProjectionMapping; refs: string[] }> = candidate ? [
    { kind: 'entity_types', refs: [...new Set(candidate.entities.map((item) => item.type_ref))] },
    { kind: 'property_refs', refs: [...new Set([...candidate.entities.flatMap((item) => Object.keys(item.properties)), ...candidate.assertions.map((item) => item.predicate_ref)])] },
    { kind: 'link_types', refs: [...new Set(candidate.links.map((item) => item.type_ref))] },
  ] : [];
  return (
    <section className="company-control-form">
          {query.isPending && <p>{t('companyKnowledge.reviewLoading')}</p>}
          {query.isError && <p role="alert">{t('companyKnowledge.projection.unavailable')}</p>}
          {view && (
            <>
              <p>{t(view.status === 'active' ? 'companyKnowledge.projection.active' : 'companyKnowledge.projection.pending')}</p>
              {view.context_access === 'denied' && <p>{t('companyKnowledge.projection.contextDenied')}</p>}
              {view.status === 'skipped_by_override' && <p>{t('companyKnowledge.analysis.overridden')}</p>}
              {view.job?.model_execution === 'unknown' && <p role="alert">{t('companyKnowledge.analysis.unknown')}</p>}
              {view.job?.error_code === 'company_analysis_output_incomplete' && <p role="alert">{t('companyKnowledge.analysis.outputLimit')}</p>}
              <button type="button" className="btn btn-secondary btn-sm" disabled={busy} onClick={() => void query.refetch()}>
                {t('companyKnowledge.analysis.refresh')}
              </button>
              {view.can_manage && !candidate && (!view.job || view.job.can_retry || view.can_reanalyze) && (
                <button type="button" className="btn btn-secondary btn-sm" disabled={busy} onClick={() => backfill.mutate()}>
                  {t(view.job?.can_retry ? 'companyKnowledge.analysis.retry' : 'companyKnowledge.projection.backfill')}
                </button>
              )}
              {view.can_manage && candidate && view.status !== 'active' && (
                <>
                  <p>{t('companyKnowledge.projection.confirmDescription')}</p>
                  {candidate.entities.map((item) => (
                    <details key={item.key}>
                      <summary>{item.display_name} · {item.type_ref}</summary>
                      <pre>{JSON.stringify(item.properties, null, 2)}</pre>
                    </details>
                  ))}
                  {!view.schemas.length && <p>{t('companyKnowledge.projection.installSchema')}</p>}
                  <label>
                    <span>{t('companyKnowledge.projection.schema')}</span>
                    <select value={activationKey} disabled={busy} onChange={(event) => setActivationKey(event.target.value)}>
                      <option value="">{t('companyKnowledge.projection.selectSchema')}</option>
                      {view.schemas.map((item) => <option key={item.activation_id} value={item.activation_id}>{item.namespace}</option>)}
                    </select>
                  </label>
                  {schema && groups.flatMap(({ kind, refs }) => refs.map((ref) => (
                    <label key={`${kind}:${ref}`}>
                      <span>{ref}</span>
                      <select value={mapping[kind][ref] ?? (schema[kind].includes(ref) ? ref : '')} disabled={busy}
                        onChange={(event) => setMapping((current) => ({ ...current, [kind]: { ...current[kind], [ref]: event.target.value } }))}>
                        <option value="">{t('companyKnowledge.projection.selectType')}</option>
                        {schema[kind].map((type) => <option key={type} value={type}>{type}</option>)}
                      </select>
                    </label>
                  )))}
                  <label>
                    <span>{t('companyKnowledge.projection.reason')}</span>
                    <textarea value={reason} maxLength={10000} rows={3} onChange={(event) => setReason(event.target.value)} />
                  </label>
                  <button type="button" className="btn btn-primary btn-sm" disabled={busy || !reason.trim() || !schema ||
                    groups.some(({ kind, refs }) => refs.some((ref) => !(mapping[kind][ref] || schema[kind].includes(ref))))}
                    onClick={() => confirm.mutate()}>{t('companyKnowledge.projection.confirm')}</button>
                </>
              )}
            </>
          )}
          {(backfill.isError || confirm.isError) && <p role="alert">{t('companyKnowledge.actionUnconfirmed')}</p>}
    </section>
  );
}

export default function CompanyKnowledgeProjectionPanel({ publicationKey }: { publicationKey: string }) {
  const { t } = useTranslation();
  const [open, setOpen] = useState(false);
  return (
    <div>
      <button type="button" className="btn btn-secondary btn-sm" aria-expanded={open} onClick={() => setOpen(!open)}>
        {t('companyKnowledge.projection.title')}
      </button>
      {open && <ProjectionDetails publicationKey={publicationKey} />}
    </div>
  );
}
