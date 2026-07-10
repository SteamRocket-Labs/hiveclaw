import { get, post } from '../core';

export type AIAssetType = 'agent' | 'skill' | 'workflow' | 'subagent' | 'external_capability';

export interface AIAssetRecord {
  id: string;
  tenant_id: string;
  asset_type: AIAssetType | string;
  native_entity_id: string | null;
  native_key: string;
  display_name: string;
  owner: { type: string; id: string | null };
  visibility_scope: string;
  lifecycle_status: string;
  active_revision_id: string | null;
  content_hash: string;
  source: { type: string; ref: string | null };
  trust_state: string;
  dependencies: string[];
  compatibility: Record<string, unknown>;
  admission_state: string;
  quarantine_reason: string | null;
  usage: { count: number; last_used_at: string | null; evidence: Array<Record<string, unknown>> };
  projection: { status: string; error: string | null };
  created_at: string | null;
  updated_at: string | null;
}

export interface AIAssetRevision {
  version: number;
  id: string;
  content_hash: string;
  diff_from_prev: { set?: Record<string, unknown>; removed?: string[] } | null;
  change_source: string;
  changed_by_user_id: string | null;
  changed_by_agent_id: string | null;
  change_message: string;
  is_active: boolean;
  parent_revision_id: string | null;
  rollback_of_revision_id: string | null;
  created_at: string | null;
  content?: Record<string, unknown>;
}

export interface AIAssetDetail {
  asset: AIAssetRecord;
  active_revision: AIAssetRevision | null;
  history: AIAssetRevision[];
}

export interface AIAssetListFilters {
  assetType?: string;
  lifecycleStatus?: string;
}

function queryString(filters: AIAssetListFilters): string {
  const params = new URLSearchParams();
  if (filters.assetType) params.set('asset_type', filters.assetType);
  if (filters.lifecycleStatus) params.set('lifecycle_status', filters.lifecycleStatus);
  const value = params.toString();
  return value ? `?${value}` : '';
}

export const aiAssetsApi = {
  list: (filters: AIAssetListFilters = {}) =>
    get<AIAssetRecord[]>(`/enterprise/ai-assets${queryString(filters)}`),
  detail: (assetId: string) => get<AIAssetDetail>(`/enterprise/ai-assets/${assetId}`),
  revisions: (assetId: string, limit = 50) =>
    get<AIAssetRevision[]>(`/enterprise/ai-assets/${assetId}/revisions?limit=${limit}`),
  revision: (assetId: string, version: number) =>
    get<AIAssetRevision>(`/enterprise/ai-assets/${assetId}/revisions/${version}`),
  rollback: (assetId: string, targetVersion: number) =>
    post<{ asset_id: string; revision_id: string; version: number }>(
      `/enterprise/ai-assets/${assetId}/rollback`,
      { target_version: targetVersion },
    ),
  reconcile: (assetId: string) =>
    post<{ status: string; expected_hash?: string; actual_hash?: string }>(
      `/enterprise/ai-assets/${assetId}/reconcile`,
    ),
};
