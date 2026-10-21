import { useState } from 'react';
import { useInfiniteQuery, useQuery } from '@tanstack/react-query';
import { Link } from 'react-router-dom';
import { useTranslation } from 'react-i18next';
import { knowledgeApi, type GrowthWorkComparison, type GrowthWorkResult } from '../../api/domains/knowledge';

export function GrowthWorkComparisonReport({ comparison }: { comparison: GrowthWorkComparison }) {
  const { t } = useTranslation();
  const unknown = t('agent.evolution.workComparison.unknown');
  const status = {
    unavailable: t('agent.evolution.workComparison.unavailable'),
    not_comparable: t('agent.evolution.workComparison.notComparable'),
    insufficient_evidence: t('agent.evolution.workComparison.insufficientEvidence'),
    recorded_conditions_match: t('agent.evolution.workComparison.conditionsMatch'),
  };
  const labels: Record<string, string> = {
    model: t('agent.evolution.workComparison.model'),
    reasoning: t('agent.evolution.workComparison.reasoning'),
    business_input: t('agent.evolution.workComparison.input'),
    authorized_evidence: t('agent.evolution.workComparison.evidence'),
    tools: t('agent.evolution.workComparison.tools'),
    budget: t('agent.evolution.workComparison.budget'),
  };
  const conditionStatus = {
    same: t('agent.evolution.workComparison.same'),
    different: t('agent.evolution.workComparison.different'),
    unknown,
  };
  const recorded = (value: number | null | undefined) => value == null ? unknown : String(value);
  const baseline = comparison.baseline;
  const candidate = comparison.candidate;
  if (comparison.status === 'unavailable' || !baseline || !candidate) {
    return <p role="status">{status.unavailable}</p>;
  }
  const metrics: { label: string; value: (work: GrowthWorkResult) => string }[] = [
    { label: t('agent.evolution.workComparison.duration'), value: w => recorded(w.duration_seconds) },
    { label: t('agent.evolution.workComparison.tokens'), value: w => recorded(w.usage.effective_tokens) },
    { label: t('agent.evolution.workComparison.cost'), value: w => w.cost.amount == null || !w.cost.currency
      ? unknown : `${w.cost.amount} ${w.cost.currency}` },
    { label: t('agent.evolution.workComparison.corrections'), value: w => recorded(w.human_corrections) },
    { label: t('agent.evolution.workComparison.feedback'), value: w => `${w.feedback.useful} 👍 / ${w.feedback.misleading} 👎` },
  ];
  return <div className="agent-growth-work-report">
    <p role="status">{status[comparison.status]}</p>
    <p className="u-meta u-tertiary">{t('agent.evolution.workComparison.observations')}</p>
    <table className="agent-evolution-table">
      <thead><tr><th /><th>{t('agent.evolution.workComparison.baseline')}</th><th>{t('agent.evolution.workComparison.candidate')}</th></tr></thead>
      <tbody>{metrics.map(row => <tr key={row.label}>
        <th>{row.label}</th><td>{row.value(baseline)}</td><td>{row.value(candidate)}</td>
      </tr>)}</tbody>
    </table>
    <table className="agent-evolution-table">
      <caption>{t('agent.evolution.workComparison.conditions')}</caption>
      <tbody>{comparison.conditions.map(row => <tr key={row.key}>
        <th>{labels[row.key] || row.key}</th><td>{conditionStatus[row.status]}</td>
        <td><details><summary>{t('agent.evolution.workComparison.record')}</summary>
          <pre>{JSON.stringify({ baseline: row.baseline, candidate: row.candidate }, null, 2)}</pre>
        </details></td>
      </tr>)}</tbody>
    </table>
    <div className="agent-growth-work-results">{[baseline, candidate].map((work, index) => <section key={work.run_id}>
      <h5>{index === 0 ? t('agent.evolution.workComparison.baseline') : t('agent.evolution.workComparison.candidate')}</h5>
      <Link to={work.work_url}>{t('agent.evolution.workComparison.openWork')}</Link>
      <details><summary>{t('agent.evolution.workComparison.result')}</summary><pre>{work.result}</pre></details>
      <details><summary>{t('agent.evolution.workComparison.learningVersions')}</summary><pre>{JSON.stringify(work.learning_sources, null, 2)}</pre></details>
      <details><summary>{t('agent.evolution.workComparison.requestRecords')}</summary><pre>{work.request_hashes.join('\n')}</pre></details>
    </section>)}</div>
  </div>;
}

export default function GrowthWorkComparisonCard({ agentId, active }: { agentId: string; active: boolean }) {
  const { t } = useTranslation();
  const [baseline, setBaseline] = useState('');
  const [candidate, setCandidate] = useState('');
  const samples = useInfiniteQuery({
    queryKey: ['growth-work-samples', agentId],
    initialPageParam: undefined as string | undefined,
    queryFn: ({ pageParam }) => knowledgeApi.observability(agentId, { includeWorkSamples: true, beforeRunId: pageParam }),
    getNextPageParam: page => page.growth.work_samples?.next_cursor || undefined,
    enabled: active && !!agentId,
    staleTime: 0,
  });
  const pairSelected = !!baseline && !!candidate && baseline !== candidate;
  const pair = useQuery({
    queryKey: ['growth-work-comparison', agentId, baseline, candidate],
    queryFn: () => knowledgeApi.observability(agentId, { baselineRunId: baseline, candidateRunId: candidate }),
    enabled: active && !!agentId && pairSelected,
    staleTime: 0,
    retry: false,
  });
  const items = samples.isError ? [] : samples.data?.pages.flatMap(p => p.growth.work_samples?.items || []) || [];
  const comparison = !samples.isError && !pair.isError && !pair.isFetching ? pair.data?.growth.work_comparison : undefined;
  return <div className="agent-evolution-card">
    <h4 className="agent-evolution-heading">{t('agent.evolution.workComparison.heading')}</h4>
    <p className="u-meta u-tertiary">{t('agent.evolution.workComparison.description')}</p>
    <div className="agent-growth-work-selectors">
      {([{ key: 'baseline', value: baseline, set: setBaseline, label: t('agent.evolution.workComparison.baseline') },
        { key: 'candidate', value: candidate, set: setCandidate, label: t('agent.evolution.workComparison.candidate') }]).map(field => <label key={field.key}>
        {field.label}<select value={field.value} onChange={event => field.set(event.target.value)}>
          <option value="">{t('agent.evolution.workComparison.choose')}</option>
          {items.map(work => <option key={work.run_id} value={work.run_id}>
            {work.completed_at || t('agent.evolution.workComparison.unknown')} · {work.source_channel} · {work.run_id.slice(0, 8)}
          </option>)}
        </select>
      </label>)}
    </div>
    {samples.isPending && <p role="status">{t('agent.evolution.workComparison.loading')}</p>}
    {!samples.isPending && !samples.isError && items.length === 0 && <p>{t('agent.evolution.workComparison.empty')}</p>}
    {samples.isError && <p role="alert">{t('agent.evolution.workComparison.loadFailed')} <button onClick={() => void samples.refetch()}>{t('agent.evolution.workComparison.retry')}</button></p>}
    {samples.hasNextPage && <button className="btn btn-ghost" disabled={samples.isFetchingNextPage} onClick={() => void samples.fetchNextPage()}>{t('agent.evolution.workComparison.more')}</button>}
    {baseline && baseline === candidate && <p role="status">{t('agent.evolution.workComparison.chooseDifferent')}</p>}
    {pairSelected && pair.isFetching && <p role="status">{t('agent.evolution.workComparison.loading')}</p>}
    {pairSelected && pair.isError && <p role="alert">{t('agent.evolution.workComparison.loadFailed')} <button onClick={() => void pair.refetch()}>{t('agent.evolution.workComparison.retry')}</button></p>}
    {pairSelected && comparison && <GrowthWorkComparisonReport comparison={comparison} />}
  </div>;
}
