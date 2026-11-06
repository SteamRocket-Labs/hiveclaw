import { useEffect, useMemo, useState } from 'react';
import { Link } from 'react-router-dom';
import { useTranslation } from 'react-i18next';
import { taskApi } from '../../api/domains/tasks';
import { toolStepLabel } from '../agent-detail/toolStepLabel';

interface HandoverWork {
  id: string; agent_id: string; task_id: string | null; title: string;
  known_results: Array<{ tool_name: string; state: string; time: string }>;
  side_effect_state: string | null; requires_confirmation: boolean; needs_admin?: boolean;
}
interface HandoverTrigger { id: string; agent_id: string; name: string }
interface Handover {
  agents: Array<{ id: string; name: string }>;
  knowledge: Array<{ id: string; title: string; merged?: boolean }>;
  work: HandoverWork[]; triggers: HandoverTrigger[];
  tools_needing_credentials?: Array<{ agent_id: string; agent_name: string; name: string }>;
}

function WorkDecision({ work, onNavigate }: { work: HandoverWork; onNavigate: () => void }) {
  const { t } = useTranslation();
  const [reason, setReason] = useState('');
  const [status, setStatus] = useState('');
  const [busy, setBusy] = useState(false);
  const [resolved, setResolved] = useState(true);
  useEffect(() => {
    if (work.needs_admin || !work.requires_confirmation || !work.task_id) return;
    let active = true;
    taskApi.get(work.agent_id, work.task_id).then(({ task }) => {
      if (!active) return;
      setResolved(!task.actions?.can_reconcile);
      if (!task.actions?.can_reconcile) setStatus(t(task.status === 'cancelled' ? 'handover.closed' : 'handover.reviewed'));
    }).catch(() => { if (active) setStatus(t('assistant.actionFailed')); });
    return () => { active = false; };
  }, [work.agent_id, work.task_id, work.requires_confirmation, work.needs_admin, t]);
  const confirm = async (decision: 'retry_safe' | 'close_without_retry') => {
    setBusy(true);
    try {
      await taskApi.reconcile(work.agent_id, work.task_id!, { decision, reason });
      setResolved(true);
      setStatus(t(decision === 'retry_safe' ? 'handover.reviewed' : 'handover.closed'));
    } catch { setStatus(t('assistant.actionFailed')); }
    finally { setBusy(false); }
  };
  return <li>
    <strong>{work.needs_admin ? work.title : <Link to={`/agents/${work.agent_id}?task_id=${work.task_id}#tasks`} onClick={onNavigate}>{work.title}</Link>}</strong>
    <p>{work.needs_admin ? t('handover.needsAdmin') : t(`handover.effect.${work.side_effect_state || 'unknown'}`)}</p>
    {work.known_results.length > 0
      ? work.known_results.filter(step => typeof step === 'object').map((step, index) => <p key={index}>
        {toolStepLabel(step.tool_name, t)}: <span>{t(`handover.step.${step.state}`)}</span> · <time dateTime={step.time}>{new Date(step.time).toLocaleString()}</time></p>)
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

export default function OffboardingNotice({ body, onNavigate }: { body: string; onNavigate: () => void }) {
  const { t } = useTranslation();
  const agentName = (name: string) => name === '__system_hr__' ? t('agentCreate.hrAgentName') : name;
  const handover = useMemo((): Handover | null => { try { return JSON.parse(body); } catch { return null; } }, [body]);
  if (!handover) return <p role="alert">{t('handover.unavailable')}</p>;
  return <div className="offboarding-notice">
    {handover.agents.length > 0 && <><h4>{t('handover.bots')}</h4>
    <ul>{handover.agents.map(agent => <li key={agent.id}><Link to={`/agents/${agent.id}#chat`} onClick={onNavigate}>{agentName(agent.name)}</Link></li>)}</ul></>}
    {handover.knowledge.length > 0 && <><h4>{t('handover.knowledge')}</h4>
    <ul>{handover.knowledge.map(document => <li key={document.id}>{document.title}{document.merged && ` · ${t('handover.merged')}`}</li>)}</ul></>}
    {!!handover.tools_needing_credentials?.length && <><h4>{t('handover.toolsNeedCredentials')}</h4>
    <p>{t('handover.enterCredentialsAgain')}</p>
    <ul>{handover.tools_needing_credentials.map((tool, index) => <li key={index}>
      <Link to={`/agents/${tool.agent_id}#tools`} onClick={onNavigate}>{agentName(tool.agent_name)} · {tool.name}</Link>
    </li>)}</ul></>}
    {handover.work.length > 0 && <><h4>{t('handover.work')}</h4>
    <ul>{handover.work.map(work => <WorkDecision key={work.id} work={work} onNavigate={onNavigate} />)}</ul></>}
    {handover.triggers.length > 0 && <><h4>{t('handover.triggers')}</h4>
    <p>{t('handover.triggersPaused')}</p>
    <ul>{handover.triggers.map(trigger => <li key={trigger.id}><span className="offboarding-notice-trigger">
      <span>{trigger.name}</span><Link to={`/agents/${trigger.agent_id}#aware`} onClick={onNavigate}>{t('handover.settings')}</Link>
    </span></li>)}</ul></>}
  </div>;
}
