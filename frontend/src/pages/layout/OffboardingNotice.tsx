import { useEffect, useState } from 'react';
import { Link } from 'react-router-dom';
import { useTranslation } from 'react-i18next';
import { taskApi } from '../../api/domains/tasks';
import { triggerApi } from '../../api/domains/triggers';

interface HandoverWork {
  id: string; agent_id: string; task_id: string | null; title: string;
  known_results: string[]; requires_confirmation: boolean;
}
interface HandoverTrigger { id: string; agent_id: string; name: string }
interface Handover {
  agents: Array<{ id: string; name: string }>;
  knowledge: Array<{ id: string; title: string }>;
  work: HandoverWork[]; triggers: HandoverTrigger[];
}

function WorkDecision({ work }: { work: HandoverWork }) {
  const { t } = useTranslation();
  const [reason, setReason] = useState('');
  const [status, setStatus] = useState('');
  const [busy, setBusy] = useState(false);
  const [resolved, setResolved] = useState(true);
  useEffect(() => {
    if (!work.requires_confirmation || !work.task_id) return;
    let active = true;
    taskApi.get(work.agent_id, work.task_id).then(({ task }) => {
      if (!active) return;
      setResolved(!task.actions?.can_reconcile);
      if (!task.actions?.can_reconcile) setStatus(t(task.status === 'cancelled' ? 'handover.closed' : 'handover.reviewed'));
    }).catch(error => { if (active) setStatus(String(error)); });
    return () => { active = false; };
  }, [work.agent_id, work.task_id, work.requires_confirmation, t]);
  const confirm = async (decision: 'retry_safe' | 'close_without_retry') => {
    setBusy(true);
    try {
      await taskApi.reconcile(work.agent_id, work.task_id!, { decision, reason });
      setResolved(true);
      setStatus(t(decision === 'retry_safe' ? 'handover.reviewed' : 'handover.closed'));
    } catch (error) { setStatus(String(error)); }
    finally { setBusy(false); }
  };
  return <li>
    <strong>{work.title}</strong>
    {work.known_results.length > 0
      ? work.known_results.map((result, index) => <p key={index}>{result}</p>)
      : <p>{t('handover.noResult')}</p>}
    {work.requires_confirmation && !resolved && <p>{t('handover.reviewFirst')}</p>}
    {work.requires_confirmation && work.task_id && !resolved && <>
      <label>{t('handover.reason')}<textarea className="form-input" value={reason}
        onChange={event => setReason(event.target.value)} /></label>
      <div className="offboarding-notice-actions">
        <button className="btn btn-secondary" disabled={busy || !reason.trim()}
          onClick={() => confirm('retry_safe')}>{t('handover.confirmSafe')}</button>
        <button className="btn btn-ghost" disabled={busy || !reason.trim()}
          onClick={() => confirm('close_without_retry')}>{t('handover.closeWork')}</button>
      </div>
    </>}
    {status && <p role="status">{status}</p>}
  </li>;
}

function TriggerDecision({ trigger, onNavigate }: { trigger: HandoverTrigger; onNavigate: () => void }) {
  const { t } = useTranslation();
  const [status, setStatus] = useState('');
  const [busy, setBusy] = useState(false);
  const [reclaimed, setReclaimed] = useState(false);
  const reclaim = async (configure: boolean) => {
    setBusy(true);
    try {
      await triggerApi.reclaim(trigger.agent_id, trigger.id);
      setReclaimed(true);
      setStatus(t(configure ? 'handover.configureNext' : 'handover.keptOff'));
    } catch (error) { setStatus(String(error)); }
    finally { setBusy(false); }
  };
  return <li>{trigger.name}
    {!reclaimed && <div className="offboarding-notice-actions">
      <button className="btn btn-secondary" disabled={busy} onClick={() => reclaim(true)}>{t('handover.reclaim')}</button>
      <button className="btn btn-ghost" disabled={busy} onClick={() => reclaim(false)}>{t('handover.keepOff')}</button>
    </div>}
    {status && <p role="status">{status}</p>}
    {reclaimed && <Link to={`/agents/${trigger.agent_id}#aware`} onClick={onNavigate}>{t('handover.settings')}</Link>}
  </li>;
}

export default function OffboardingNotice({ body, onNavigate }: { body: string; onNavigate: () => void }) {
  const { t } = useTranslation();
  let handover: Handover;
  try { handover = JSON.parse(body); }
  catch { return <p role="alert">{t('handover.unavailable')}</p>; }
  return <div className="offboarding-notice">
    <h4>{t('handover.bots')}</h4>
    <ul>{handover.agents.map(agent => <li key={agent.id}><Link to={`/agents/${agent.id}#chat`} onClick={onNavigate}>{agent.name}</Link></li>)}</ul>
    <h4>{t('handover.knowledge')}</h4>
    <ul>{handover.knowledge.map(document => <li key={document.id}>{document.title}</li>)}</ul>
    <h4>{t('handover.work')}</h4>
    <ul>{handover.work.map(work => <WorkDecision key={work.id} work={work} />)}</ul>
    <h4>{t('handover.triggers')}</h4>
    <p>{t('handover.triggersPaused')}</p>
    <ul>{handover.triggers.map(trigger => <TriggerDecision key={trigger.id} trigger={trigger} onNavigate={onNavigate} />)}</ul>
  </div>;
}
