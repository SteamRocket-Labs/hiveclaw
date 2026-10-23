import type { BrowserApproval, BrowserObservation, BrowserReply, WorkBrowser } from '../../api/domains/browser';
import type { ArtifactContinuationContext } from '../agent-detail/ArtifactContinuation';

export function canOpenWorkBrowser(session: Record<string, unknown> | null | undefined,
  readOnly: boolean, userId: string | undefined): boolean {
  if (session?.audience_kind && session.audience_kind !== 'private') return false;
  return !readOnly || Boolean(userId && session?.user_id === userId && session.session_kind === 'delegation_run'
    && session.runtime_source === 'delegation' && session.source_channel === 'agent');
}

export function browserReplyMatches(value: BrowserReply, tenant: string, agent: string, session: string): boolean {
  return value.tenant_id === tenant && value.agent_id === agent && value.session_id === session;
}

export function browserFrameCurrent(frame: WorkBrowser | null, view: WorkBrowser | null): boolean {
  return Boolean(frame && view && frame.id === view.id && frame.revision === view.revision
    && frame.control_epoch === view.control_epoch && view.status === 'ready' && view.desired_state === 'running');
}

export function canApplyBrowserApproval(value: BrowserApproval): boolean {
  return !value.unavailable && value.status === 'approved' && value.execution_status === 'approved'
    && typeof value.action === 'string' && ['start', 'observe', 'request_control', 'take_control', 'navigate', 'click', 'type', 'key', 'scroll', 'select_page'].includes(value.action)
    && typeof value.request_id === 'string' && Boolean(value.request_id)
    && Number.isSafeInteger(value.expected_revision) && Number(value.expected_revision) >= 0
    && Number.isSafeInteger(value.expected_epoch) && Number(value.expected_epoch) >= 0;
}

export function browserFailureKnownNotStarted(failure: unknown): boolean {
  return (failure as { data?: { effect?: string } } | null)?.data?.effect === 'not_started';
}

export function browserPoint(viewport: BrowserObservation['viewport'], rect: { left: number; top: number; width: number; height: number },
  clientX: number, clientY: number): { x: number; y: number } | null {
  if (!viewport || !Object.values(viewport).every(Number.isFinite) || viewport.width <= 0 || viewport.height <= 0
    || ![rect.left, rect.top, rect.width, rect.height].every(Number.isFinite) || rect.width <= 0 || rect.height <= 0
    || !Number.isFinite(clientX) || !Number.isFinite(clientY)) return null;
  const x = (clientX - rect.left) / rect.width, y = (clientY - rect.top) / rect.height;
  if (x < 0 || x >= 1 || y < 0 || y >= 1) return null;
  return { x: viewport.offsetX + x * viewport.width, y: viewport.offsetY + y * viewport.height };
}

export const BROWSER_CONTINUE_INPUT = 'Please continue this work from the current browser state. Observe the current page and work state before making any browser changes. Human control being returned does not confirm a business outcome.';

export async function resumeBrowserInOriginalWork(context: ArtifactContinuationContext,
  scope: { tenant: string; agent: string; session: string }, attempt: { id: string; display: string },
  isCurrent: () => boolean, read: () => Promise<BrowserReply>): Promise<void> {
  if (!isCurrent() || context.sessionId !== scope.session) throw new Error('browser_scope_changed');
  const latest = await read();
  if (!isCurrent() || !browserReplyMatches(latest, scope.tenant, scope.agent, scope.session)
    || latest.browser?.control_mode !== 'agent' || latest.browser.status !== 'ready'
    || latest.browser.desired_state !== 'running') throw new Error('browser_control_changed');
  await context.send(BROWSER_CONTINUE_INPUT, { inputId: attempt.id, expectedSessionId: scope.session, displayContent: attempt.display });
}
