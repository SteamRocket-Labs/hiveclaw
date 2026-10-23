import { get, post } from '../core';
import type { EnvironmentScope } from './environments';

export interface WorkBrowser {
  id: string; status: 'starting' | 'ready' | 'lost' | 'stopped' | 'revoked'; desired_state: 'running' | 'stopped';
  revision: number; control_epoch: number; control_mode: 'agent' | 'human' | 'requested' | 'paused';
  control_expires_at: string | null; viewer_expires_at: string | null; fresh_observation_required: boolean;
  reconciliation_pending: boolean; next_action: string; resource_error: string | null;
}
export interface BrowserObservation {
  screenshot: string; media_type: string; target_id: string;
  pages: Array<{ targetId: string; title?: string; url?: string }>;
  viewport: { width: number; height: number; offsetX: number; offsetY: number } | null;
}
export interface BrowserReply {
  tenant_id: string; agent_id: string; session_id: string; browser: WorkBrowser | null; observation?: BrowserObservation;
}
export interface BrowserRead extends BrowserReply {
  configured: boolean; limits: { human_control_seconds: number; viewer_timeout_seconds: number; idle_seconds: number };
  approvals?: { items: BrowserApproval[]; has_more: boolean };
}
export interface BrowserApproval {
  id: string; status: string; execution_status: string; unavailable?: boolean;
  action?: BrowserAction; request_id?: string; expected_revision?: number; expected_epoch?: number;
}
export type BrowserAction = 'start' | 'observe' | 'take_control' | 'return_control' | 'heartbeat' | 'disconnect'
  | 'stop' | 'reconcile' | 'navigate' | 'click' | 'type' | 'key' | 'scroll' | 'select_page' | 'request_control';
export const workBrowserApi = {
  read: (agentId: string, sessionId: string, scope: EnvironmentScope, signal?: AbortSignal) =>
    get<BrowserRead>(`/agents/${agentId}/sessions/${sessionId}/browser?${new URLSearchParams({ ...scope })}`, { signal }),
  control: (agentId: string, sessionId: string, scope: EnvironmentScope, action: BrowserAction,
    view: WorkBrowser | null, requestId: string, args: Record<string, unknown> = {}, signal?: AbortSignal) =>
    post<BrowserReply>(`/agents/${agentId}/sessions/${sessionId}/browser/${action}`, { ...scope,
      request_id: requestId, expected_revision: view?.revision ?? 0, expected_epoch: view?.control_epoch ?? 0,
      arguments: args }, { signal }),
  applyApproval: (agentId: string, sessionId: string, scope: EnvironmentScope, approval: BrowserApproval, signal?: AbortSignal) =>
    post<BrowserReply>(`/agents/${agentId}/sessions/${sessionId}/browser/${approval.action}`, { ...scope,
      request_id: approval.request_id, expected_revision: approval.expected_revision,
      expected_epoch: approval.expected_epoch, approval_id: approval.id }, { signal }),
};
