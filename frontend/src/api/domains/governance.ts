import { del, get, patch, post, put } from '../core';

export type GuardPolicyDecision = 'allow' | 'require_approval' | 'deny';

export interface GuardPolicyRule {
  rule_id?: string;
  tools?: string[];
  decision?: GuardPolicyDecision | string;
  reason?: string;
  argument_equals?: Record<string, unknown>;
  [key: string]: unknown;
}

export interface GuardPolicyLane {
  tool_rules?: GuardPolicyRule[];
  [key: string]: unknown;
}

export interface GuardPolicy {
  id: string;
  tenant_id: string;
  version: number;
  zone_guard: GuardPolicyLane;
  egress_guard: GuardPolicyLane;
}

export interface GuardPolicyUpdate {
  expected_version: number;
  zone_guard?: GuardPolicyLane;
  egress_guard?: GuardPolicyLane;
}

export type FeatureFlagType = 'boolean' | 'percentage' | 'allowlist' | 'tenant_gate';

export interface FeatureFlag {
  id: string;
  key: string;
  description: string;
  flag_type: FeatureFlagType;
  enabled: boolean;
  rollout_percentage: number | null;
  allowed_tenant_ids: string[] | null;
  allowed_user_ids: string[] | null;
  overrides: Record<string, boolean> | null;
  expires_at: string | null;
  created_at: string | null;
  updated_at: string | null;
}

export interface FeatureFlagWrite {
  key?: string;
  description?: string;
  flag_type?: FeatureFlagType;
  enabled?: boolean;
  rollout_percentage?: number | null;
  allowed_tenant_ids?: string[] | null;
  allowed_user_ids?: string[] | null;
  overrides?: Record<string, boolean> | null;
  expires_at?: string | null;
}

export interface FeatureFlagUpdate extends FeatureFlagWrite {
  expected_updated_at: string;
}

export const governanceApi = {
  getGuardPolicy: () => get<GuardPolicy>('/guard-policies'),
  updateGuardPolicy: (payload: GuardPolicyUpdate) =>
    put<GuardPolicy>('/guard-policies', payload),
  listFeatureFlags: () => get<FeatureFlag[]>('/feature-flags/'),
  createFeatureFlag: (payload: FeatureFlagWrite & { key: string }) =>
    post<FeatureFlag>('/feature-flags/', payload),
  updateFeatureFlag: (flagId: string, payload: FeatureFlagUpdate) =>
    patch<FeatureFlag>(`/feature-flags/${flagId}`, payload),
  deleteFeatureFlag: (flagId: string, expectedUpdatedAt: string) =>
    del<void>(
      `/feature-flags/${flagId}?expected_updated_at=${encodeURIComponent(expectedUpdatedAt)}`,
    ),
};
