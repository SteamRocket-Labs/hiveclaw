import { describe, expect, it, vi } from 'vitest';
import { browserFailureKnownNotStarted, browserFrameCurrent, browserPoint, browserReplyMatches, canApplyBrowserApproval, canOpenWorkBrowser, resumeBrowserInOriginalWork } from './workBrowserState';
import type { WorkBrowser } from '../../api/domains/browser';
import { approvalExecutionPresentation } from '../../utils/approvalExecution';

const view: WorkBrowser = { id: 'resource', status: 'ready', desired_state: 'running', revision: 4, control_epoch: 3,
  control_mode: 'human', control_expires_at: null, viewer_expires_at: null, fresh_observation_required: true,
  reconciliation_pending: false, next_action: 'human_control', resource_error: null };

describe('private work browser state', () => {
  it('exposes the requester browser without making a delegated conversation writable', () => {
    const child = { user_id: 'requester', session_kind: 'delegation_run', runtime_source: 'delegation', source_channel: 'agent' };
    expect(canOpenWorkBrowser(child, true, 'requester')).toBe(true);
    expect(canOpenWorkBrowser(child, true, 'other-reader')).toBe(false);
    for (const changed of [{ audience_kind: 'group' }, { runtime_source: 'web_chat' },
      { session_kind: 'human_chat' }, { source_channel: 'web' }]) {
      expect(canOpenWorkBrowser({ ...child, ...changed }, true, 'requester')).toBe(false);
    }
    expect(canOpenWorkBrowser({}, false, 'requester')).toBe(true);
    expect(canOpenWorkBrowser({ audience_kind: 'group' }, false, 'requester')).toBe(false);
  });
  it('shows the consumed human effect outcome independently of the approval decision', () => {
    for (const state of ['succeeded', 'failed', 'needs_reconciliation']) {
      expect(approvalExecutionPresentation({ status: 'approved', tool_name: null,
        execution_kind: 'browser_human', execution_status: state }).key).toBe(state);
    }
    expect(approvalExecutionPresentation({ status: 'approved', tool_name: null, execution_status: 'queued' }).key).toBe('approved');
  });
  it('only offers explicit application of an intact, approved, unconsumed human request', () => {
    const pending = { id: 'ticket', status: 'approved', execution_status: 'approved', action: 'type' as const,
      request_id: 'original-request', expected_revision: 2, expected_epoch: 1 };
    expect(canApplyBrowserApproval(pending)).toBe(true);
    for (const changed of [{ status: 'pending' }, { execution_status: 'executing' }, { execution_status: 'succeeded' },
      { execution_status: 'needs_reconciliation' }, { unavailable: true }, { request_id: '' },
      { expected_revision: NaN }, { expected_epoch: -1 }]) expect(canApplyBrowserApproval({ ...pending, ...changed })).toBe(false);
  });
  it('keeps a typed not-started rejection separate from unknown effects or lost replies', () => {
    expect(browserFailureKnownNotStarted({ data: { code: 'browser_approval_required', effect: 'not_started' } })).toBe(true);
    for (const error of [null, new Error('response lost'), { data: { effect: 'unknown' } }, { message: 'not_started' }]) {
      expect(browserFailureKnownNotStarted(error)).toBe(false);
    }
  });
  it('rejects replies from another company, employee or original work', () => {
    const reply = { tenant_id: 'company', agent_id: 'employee', session_id: 'work', browser: view };
    expect(browserReplyMatches(reply, 'company', 'employee', 'work')).toBe(true);
    for (const field of ['tenant_id', 'agent_id', 'session_id']) {
      expect(browserReplyMatches({ ...reply, [field]: 'foreign' }, 'company', 'employee', 'work')).toBe(false);
    }
  });
  it('invalidates the screen after control changes, restart, loss or stop', () => {
    expect(browserFrameCurrent(view, view)).toBe(true);
    for (const change of [{ id: 'other' }, { revision: 5 }, { control_epoch: 4 }, { status: 'lost' as const },
      { desired_state: 'stopped' as const }]) expect(browserFrameCurrent(view, { ...view, ...change })).toBe(false);
    expect(browserFrameCurrent(view, null)).toBe(false);
  });
  it('uses CSS viewport units, visual offset and the actual scaled image rectangle', () => {
    const viewport = { width: 1200, height: 800, offsetX: 10, offsetY: 20 };
    const rect = { left: 100, top: 50, width: 600, height: 400 };
    expect(browserPoint(viewport, rect, 400, 250)).toEqual({ x: 610, y: 420 });
    expect(browserPoint(viewport, rect, 99, 250)).toBeNull();
    expect(browserPoint(viewport, rect, 700, 250)).toBeNull();
    expect(browserPoint(null, rect, 400, 250)).toBeNull();
    expect(browserPoint(viewport, { ...rect, width: NaN }, 400, 250)).toBeNull();
    expect(browserPoint(viewport, rect, Infinity, 250)).toBeNull();
    const prototypeRect = Object.create({ ...rect, width: NaN });
    expect(browserPoint(viewport, prototypeRect, 400, 250)).toBeNull();
  });
  it('reuses the same canonical input after an unconfirmed continuation', async () => {
    const send = vi.fn().mockRejectedValueOnce(new Error('response lost')).mockResolvedValue(undefined);
    const scope = { tenant: 'company', agent: 'employee', session: 'work' };
    const attempt = { id: 'stable-input-id', display: 'Continue from the current browser state' };
    const read = async () => ({ tenant_id: scope.tenant, agent_id: scope.agent, session_id: scope.session,
      browser: { ...view, control_mode: 'agent' as const } });
    await expect(resumeBrowserInOriginalWork({ sessionId: 'work', send }, scope, attempt, () => true, read)).rejects.toThrow('response lost');
    await resumeBrowserInOriginalWork({ sessionId: 'work', send }, scope, attempt, () => true, read);
    expect(send).toHaveBeenCalledTimes(2);
    expect(send.mock.calls[0]).toEqual(send.mock.calls[1]);
    expect(send.mock.calls[0][1]).toEqual({ inputId: attempt.id, expectedSessionId: 'work', displayContent: attempt.display });
  });
  it('does not send to a stale account or another work after asynchronous readback', async () => {
    const send = vi.fn();
    const scope = { tenant: 'company', agent: 'employee', session: 'work' };
    let current = true;
    const read = async () => { current = false; return { tenant_id: scope.tenant, agent_id: scope.agent,
      session_id: scope.session, browser: { ...view, control_mode: 'agent' as const } }; };
    await expect(resumeBrowserInOriginalWork({ sessionId: 'work', send }, scope, { id: 'input', display: 'Continue' },
      () => current, read)).rejects.toThrow('browser_control_changed');
    await expect(resumeBrowserInOriginalWork({ sessionId: 'other-work', send }, scope, { id: 'input', display: 'Continue' },
      () => true, read)).rejects.toThrow('browser_scope_changed');
    expect(send).not.toHaveBeenCalled();
  });
  it('cannot resume the model while human control or stopping remains authoritative', async () => {
    const send = vi.fn();
    for (const browser of [view, { ...view, control_mode: 'agent' as const, desired_state: 'stopped' as const }]) {
      await expect(resumeBrowserInOriginalWork({ sessionId: 'work', send }, { tenant: 'company', agent: 'employee', session: 'work' },
        { id: 'input', display: 'Continue' }, () => true,
        async () => ({ tenant_id: 'company', agent_id: 'employee', session_id: 'work', browser }))).rejects.toThrow('browser_control_changed');
    }
    expect(send).not.toHaveBeenCalled();
  });
});
