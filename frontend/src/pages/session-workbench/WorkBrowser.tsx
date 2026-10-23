import React from 'react';
import { useTranslation } from 'react-i18next';
import Modal from '../../components/ui/Modal';
import { useAuthStore } from '../../stores';
import { workBrowserApi, type BrowserAction, type BrowserApproval, type BrowserObservation, type BrowserRead, type WorkBrowser } from '../../api/domains/browser';
import type { EnvironmentScope } from '../../api/domains/environments';
import type { ArtifactContinuationContext } from '../agent-detail/ArtifactContinuation';
import { browserFailureKnownNotStarted, browserFrameCurrent, browserPoint, browserReplyMatches, canApplyBrowserApproval, resumeBrowserInOriginalWork } from './workBrowserState';
import InlineApprovalCard from '../agent-detail/InlineApprovalCard';
import './WorkBrowser.css';

function tenantId(fallback: string) {
  try { return localStorage.getItem('current_tenant_id') || fallback; } catch { return fallback; }
}

export default function WorkBrowser({ agentId, sessionId, continuation }: {
  agentId: string; sessionId: string; continuation?: ArtifactContinuationContext;
}) {
  const user = useAuthStore((state) => state.user);
  const [generation, changed] = React.useState(0);
  React.useEffect(() => {
    const listener = (event: StorageEvent) => { if (!event.key || event.key === 'current_tenant_id') changed((value) => value + 1); };
    window.addEventListener('storage', listener);
    return () => window.removeEventListener('storage', listener);
  }, []);
  const tenant = tenantId(user?.tenant_id || '');
  if (!user || !tenant) return null;
  const subject = `${user.id}:${user.role}:${tenant}`;
  return <BrowserWindow key={`${subject}:${agentId}:${sessionId}:${generation}`} agentId={agentId} sessionId={sessionId}
    subject={subject} scope={{ expected_user_id: user.id, expected_tenant_id: tenant }} continuation={continuation} />;
}

function BrowserWindow({ agentId, sessionId, scope, subject, continuation }: {
  agentId: string; sessionId: string; scope: EnvironmentScope; subject: string; continuation?: ArtifactContinuationContext;
}) {
  const { t } = useTranslation();
  const [open, setOpen] = React.useState(false);
  const [read, setRead] = React.useState<BrowserRead | null>(null);
  const [view, setView] = React.useState<WorkBrowser | null>(null);
  const [image, setImage] = React.useState<{ observation: BrowserObservation; view: WorkBrowser } | null>(null);
  const [busy, setBusy] = React.useState(false);
  const [error, setError] = React.useState('');
  const [unknown, setUnknown] = React.useState(false);
  const [url, setUrl] = React.useState('');
  const [secret, setSecret] = React.useState('');
  const [recorded, setRecorded] = React.useState(false);
  const active = React.useRef(true), pending = React.useRef(false);
  const windowEpoch = React.useRef(0);
  const current = React.useRef(view); current.current = view;
  const requests = React.useRef(new Set<AbortController>());
  const inputAttempt = React.useRef<{ id: string; display: string } | null>(null);
  function valid() {
    const user = useAuthStore.getState().user;
    return active.current && `${user?.id}:${user?.role}:${tenantId(user?.tenant_id || '')}` === subject;
  }
  React.useEffect(() => {
    active.current = true;
    return () => { release(); active.current = false; requests.current.forEach((request) => request.abort()); };
  }, []);
  async function bounded<T>(run: (signal: AbortSignal) => Promise<T>): Promise<T> {
    const epoch = windowEpoch.current;
    const controller = new AbortController(); requests.current.add(controller);
    try { const result = await run(controller.signal); if (!valid() || epoch !== windowEpoch.current) throw new Error('browser_scope_changed'); return result; }
    finally { requests.current.delete(controller); }
  }
  async function refresh() {
    const epoch = windowEpoch.current;
    try {
      const result = await bounded((signal) => workBrowserApi.read(agentId, sessionId, scope, signal));
      if (!browserReplyMatches(result, scope.expected_tenant_id, agentId, sessionId)) throw new Error('browser_scope_changed');
      setRead(result); setView(result.browser); setUnknown(false); setError('');
      setImage((previous) => previous && browserFrameCurrent(previous.view, result.browser) ? previous : null);
    } catch { if (valid() && epoch === windowEpoch.current) { setUnknown(true); setImage(null); setError(t('workBrowser.unconfirmed')); } }
  }
  async function control(action: BrowserAction, args: Record<string, unknown> = {}, background = false, approval?: BrowserApproval) {
    if (!valid() || !background && pending.current || approval && !canApplyBrowserApproval(approval)) return null;
    if (!background) { pending.current = true; setBusy(true); setError(''); }
    const frame = current.current;
    const epoch = windowEpoch.current;
    try {
      const result = await bounded((signal) => approval
        ? workBrowserApi.applyApproval(agentId, sessionId, scope, approval, signal)
        : workBrowserApi.control(agentId, sessionId, scope, action, frame, crypto.randomUUID(), args, signal));
      if (!browserReplyMatches(result, scope.expected_tenant_id, agentId, sessionId)) throw new Error('browser_scope_changed');
      if (background && !browserFrameCurrent(frame, current.current)) return null;
      current.current = result.browser; setView(result.browser); setUnknown(false);
      if (result.observation && result.browser) setImage({ observation: result.observation, view: result.browser });
      else if (!background) setImage(null);
      if (action === 'take_control' || action === 'start') { setRecorded(false); inputAttempt.current = null; }
      if (approval) await refresh();
      if (!background && !['observe', 'stop', 'reconcile', 'return_control', 'disconnect'].includes(action) && result.browser?.status === 'ready') {
        try {
          const observed = await bounded((signal) => workBrowserApi.control(agentId, sessionId, scope, 'observe', result.browser,
            crypto.randomUUID(), {}, signal));
          if (browserReplyMatches(observed, scope.expected_tenant_id, agentId, sessionId)
            && observed.observation && observed.browser && browserFrameCurrent(result.browser, current.current)) {
            setImage({ observation: observed.observation, view: observed.browser });
          }
        } catch { if (valid() && epoch === windowEpoch.current) setError(t('workBrowser.screenUnavailable')); }
      }
      return result;
    } catch (failure) {
      if (valid() && epoch === windowEpoch.current && (!background || browserFrameCurrent(frame, current.current))) {
        const code = (failure as { data?: { code?: string } }).data?.code;
        const notStarted = browserFailureKnownNotStarted(failure);
        setUnknown(!notStarted); if (!notStarted) setImage(null);
        setError(t(code === 'browser_commands_active' ? 'workBrowser.actionPending'
          : code === 'browser_capability_revoked' ? 'workBrowser.denied'
          : code === 'browser_approval_required' ? 'workBrowser.approvalRequired'
          : code === 'browser_approval_consumed' ? 'workBrowser.approvalUsed'
          : ['browser_approval_expired', 'browser_approval_binding_changed', 'browser_approval_input_changed',
            'browser_approval_not_approved', 'browser_approval_revoked'].includes(code || '')
            ? 'workBrowser.approvalChanged' : 'workBrowser.unconfirmed'));
        if (code === 'browser_approval_required' || approval) {
          await refresh();
          if (code === 'browser_approval_required' && valid() && epoch === windowEpoch.current) setError(t('workBrowser.approvalRequired'));
          else if (valid() && epoch === windowEpoch.current) setError(t(code === 'browser_approval_consumed'
            ? 'workBrowser.approvalUsed' : 'workBrowser.approvalChanged'));
        }
      }
      return null;
    } finally { if (!background && epoch === windowEpoch.current) { pending.current = false; if (valid()) setBusy(false); } }
  }
  React.useEffect(() => { if (open) void refresh(); }, [open]);
  React.useEffect(() => {
    if (!open || unknown || view?.control_mode !== 'human' || !read) return;
    const timer = window.setInterval(() => { void control('heartbeat', {}, true); },
      Math.max(500, Math.min(10000, read.limits.viewer_timeout_seconds * 1000 / 3)));
    return () => window.clearInterval(timer);
  }, [open, unknown, view?.control_mode, read?.limits.viewer_timeout_seconds]);
  React.useEffect(() => { if (view?.control_mode !== 'human') setSecret(''); }, [view?.control_mode]);
  function release() {
    if (valid() && current.current?.control_mode === 'human') {
      void workBrowserApi.control(agentId, sessionId, scope, 'disconnect', current.current, crypto.randomUUID()).catch(() => {});
    }
  }
  function close() {
    release(); requests.current.forEach((request) => request.abort());
    if (!valid()) return;
    windowEpoch.current += 1; pending.current = false; setBusy(false);
    setOpen(false); setSecret(''); setImage(null);
  }
  async function continueWork() {
    if (!continuation || continuation.sessionId !== sessionId || pending.current || !valid()) return;
    pending.current = true; setBusy(true); setError('');
    const epoch = windowEpoch.current;
    try {
      inputAttempt.current ||= { id: crypto.randomUUID(), display: t('workBrowser.continueMessage') };
      await resumeBrowserInOriginalWork(continuation, { tenant: scope.expected_tenant_id, agent: agentId, session: sessionId },
        inputAttempt.current, () => valid() && epoch === windowEpoch.current,
        () => bounded((signal) => workBrowserApi.read(agentId, sessionId, scope, signal)));
      if (valid() && epoch === windowEpoch.current) { setRecorded(true); inputAttempt.current = null; }
    } catch { if (valid() && epoch === windowEpoch.current) setError(t('workBrowser.resumeUnknown')); }
    finally { if (epoch === windowEpoch.current) { pending.current = false; if (valid()) setBusy(false); } }
  }
  const human = view?.control_mode === 'human' && !unknown && view.status === 'ready';
  const shown = image && browserFrameCurrent(image.view, view) ? image.observation : null;
  const enabled = human && !busy && Boolean(shown);
  function stateLabel() {
    if (!read) return t('common.loading');
    if (!view) return t('workBrowser.notStarted');
    switch (view.status) {
      case 'starting': return t('workBrowser.starting');
      case 'lost': return t('workBrowser.lost');
      case 'stopped': case 'revoked': return t('workBrowser.stopped');
      default:
        switch (view.control_mode) {
          case 'human': return t('workBrowser.human');
          case 'paused': return t('workBrowser.paused');
          case 'requested': return t('workBrowser.requested');
          default: return t('workBrowser.agent');
        }
    }
  }
  const observe = () => { void control('observe'); };
  return <section className="work-browser-entry">
    <button className="btn btn-secondary" type="button" onClick={() => { windowEpoch.current += 1; setOpen(true); }}>{t('workBrowser.open')}</button>
    <Modal open={open} onClose={() => { void close(); }} width={1040} title={t('workBrowser.title')}
      footer={<button className="btn btn-secondary" type="button" onClick={() => { void close(); }}>{t('common.close')}</button>}>
      <div className="work-browser">
        <p>{t('workBrowser.private')}</p>
        <p role="status">{stateLabel()}</p>
        {error && <p role="alert">{error}</p>}
        {recorded && <p role="status">{t('workBrowser.recorded')}</p>}
        {Boolean(read?.approvals?.items.length) && <details open={read?.approvals?.items.some((item) => item.status === 'pending'
          || item.execution_status === 'needs_reconciliation')}>
          <summary>{t('workBrowser.approvals')}</summary>
          <p>{t('workBrowser.approvalHelp')}</p>
          {read?.approvals?.items.map((approval) => <div className="card card-pad-sm" key={approval.id}>
            <InlineApprovalCard agentId={agentId} sessionId={sessionId} approvalId={approval.id} authorityKey={subject} />
            {canApplyBrowserApproval(approval) && <button className="btn btn-primary" type="button" disabled={busy}
              onClick={() => { if (approval.action) void control(approval.action, {}, false, approval); }}>{t('workBrowser.applyApproval')}</button>}
          </div>)}
          {read?.approvals?.has_more && <p>{t('workBrowser.moreApprovals')}</p>}
        </details>}
        <div className="work-browser-toolbar">
          <button className="btn btn-secondary" type="button" onClick={() => { void refresh(); }} disabled={busy}>{t('common.refresh')}</button>
          {read && (!view || ['stopped', 'revoked'].includes(view.status)) && read.configured && !unknown &&
            <button className="btn btn-primary" type="button" disabled={busy} onClick={() => { void control('start'); }}>{t('workBrowser.start')}</button>}
          {read && !read.configured && !view && <p>{t('workBrowser.notConfigured')}</p>}
          {view?.status === 'ready' && !unknown && <>
            <button className="btn btn-secondary" type="button" disabled={busy} onClick={observe}>{t('workBrowser.observe')}</button>
            {!human ? <button className="btn btn-primary" type="button" disabled={busy} onClick={() => { void control('take_control'); }}>{t('workBrowser.take')}</button>
              : <button className="btn btn-primary" type="button" disabled={busy} onClick={() => { void control('return_control'); }}>{t('workBrowser.return')}</button>}
          </>}
          {view?.status === 'lost' && <button className="btn btn-secondary" type="button" disabled={busy || view.reconciliation_pending}
            onClick={() => { void control('reconcile'); }}>{t('workBrowser.reconcile')}</button>}
          {view && !['stopped', 'revoked'].includes(view.status) && <button className="btn btn-secondary" type="button" disabled={busy}
            onClick={() => { void control('stop'); }}>{t('workBrowser.stop')}</button>}
          {view?.status === 'ready' && view.control_mode === 'agent' && continuation && !unknown &&
            <button className="btn btn-secondary" type="button" disabled={busy || recorded} onClick={() => { void continueWork(); }}>{t('workBrowser.continue')}</button>}
        </div>
        {view?.control_expires_at && <p>{t('workBrowser.expires', { time: new Date(view.control_expires_at).toLocaleTimeString() })}</p>}
        {shown && <>
          <div className="work-browser-toolbar">
            <label>{t('workBrowser.pages')}<select className="form-select" value={shown.target_id} disabled={!enabled}
              onChange={(event) => { void control('select_page', { target_id: event.target.value }); }}>
              {shown.pages.map((page) => <option key={page.targetId} value={page.targetId}>{page.title || page.url || t('workBrowser.page')}</option>)}
            </select></label>
          </div>
          <img className={`work-browser-image${enabled ? ' is-interactive' : ''}`} alt={t('workBrowser.screen')}
            src={`data:${shown.media_type};base64,${shown.screenshot}`} draggable={false} onClick={(event) => {
              if (!enabled) return;
              const point = browserPoint(shown.viewport, event.currentTarget.getBoundingClientRect(), event.clientX, event.clientY);
              if (point) void control('click', point);
            }} />
          {human && !shown.viewport && <p>{t('workBrowser.coordinatesUnavailable')}</p>}
        </>}
        {human && <>
          {!shown && <p>{t('workBrowser.screenUnavailable')}</p>}
          <form className="work-browser-toolbar" onSubmit={(event) => { event.preventDefault(); void control('navigate', { url }); }}>
            <label>{t('workBrowser.address')}<input className="form-input" type="url" value={url} disabled={!enabled} onChange={(event) => setUrl(event.target.value)} required /></label>
            <button className="btn btn-secondary" type="submit" disabled={!enabled || !url}>{t('workBrowser.navigate')}</button>
          </form>
          <form className="work-browser-toolbar" onSubmit={(event) => { event.preventDefault(); const text = secret; setSecret(''); void control('type', { text }); }}>
            <label>{t('workBrowser.text')}<input className="form-input" type="password" value={secret} disabled={!enabled} autoComplete="off"
              onChange={(event) => setSecret(event.target.value)} /></label>
            <button className="btn btn-secondary" type="submit" disabled={!enabled || !secret}>{t('workBrowser.enterText')}</button>
          </form>
          <div className="work-browser-toolbar">
            {['Tab', 'Enter', 'Backspace', 'Escape'].map((key) => <button key={key} className="btn btn-secondary" type="button"
              disabled={!enabled} onClick={() => { void control('key', { key }); }}>{key}</button>)}
            {[{ direction: -1, label: t('workBrowser.up') }, { direction: 1, label: t('workBrowser.down') }].map(({ direction, label }) => <button key={direction} className="btn btn-secondary" type="button" disabled={!enabled || !shown?.viewport}
              onClick={() => { if (shown?.viewport) void control('scroll', { x: shown.viewport.width / 2, y: shown.viewport.height / 2,
                deltaX: 0, deltaY: direction * shown.viewport.height * 0.7 }); }}>{label}</button>)}
          </div>
        </>}
      </div>
    </Modal>
  </section>;
}
