import { get, post } from '../core';

export interface HrCreationDraft {
  blueprint_id: string;
  blueprint_version: number;
  blueprint_hash: string;
  draft_status: string;
  blueprint: Record<string, unknown>;
  confirmed_by_user_id?: string | null;
  confirmed_at?: string | null;
  created_agent_id?: string | null;
  provisioning_task_id?: string | null;
  provisioning?: Record<string, unknown>;
  provisioning_steps?: Array<{
    step_key: string;
    step_kind: string;
    required: boolean;
    status: string;
    attempt_count: number;
    source_key?: string | null;
    error_code?: string | null;
    error_message?: string | null;
  }>;
  creation_state?: string | null;
  failure?: Record<string, unknown> | null;
}

export interface HrCreationConfirmInput {
  blueprint_version: number;
  blueprint_hash: string;
}

function draftPath(agentId: string, draftId: string): string {
  return `/agents/${encodeURIComponent(agentId)}/hr-creation-drafts/${encodeURIComponent(draftId)}`;
}

export const hrCreationApi = {
  get: (agentId: string, draftId: string) => get<HrCreationDraft>(draftPath(agentId, draftId)),
  confirm: (agentId: string, draftId: string, input: HrCreationConfirmInput) => (
    post<HrCreationDraft>(`${draftPath(agentId, draftId)}/confirm`, input)
  ),
  reject: (agentId: string, draftId: string) => (
    post<HrCreationDraft>(`${draftPath(agentId, draftId)}/reject`, {})
  ),
  retry: (agentId: string, draftId: string) => (
    post<HrCreationDraft>(`${draftPath(agentId, draftId)}/retry`, {})
  ),
  cancel: (agentId: string, draftId: string) => (
    post<HrCreationDraft>(`${draftPath(agentId, draftId)}/cancel`, {})
  ),
};
