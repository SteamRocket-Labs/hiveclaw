import { get, post } from '../core';

export interface EnvironmentScope { expected_tenant_id: string; expected_user_id: string }
export interface CommandEnvironment {
  id: string; provider_key: string; generation: number; revision: number;
  desired_state: 'ready' | 'stopped'; observed_state: 'ready' | 'stopping' | 'stopped' | 'lost' | 'failed';
  active_commands: number; next_action: string; last_used_at: string | null;
  capabilities: { persistent_compute: boolean; detached_process_control: boolean; browser: boolean };
}
export interface EnvironmentView { tenant_id: string; agent_id: string; can_manage: boolean; environment: CommandEnvironment | null }
export const environmentApi = {
  read: (agentId: string, scope: EnvironmentScope, signal?: AbortSignal) =>
    get<EnvironmentView>(`/agents/${agentId}/environment?${new URLSearchParams({ ...scope })}`, { signal }),
  control: (agentId: string, scope: EnvironmentScope, action: 'stop' | 'resume', expectedRevision: number) =>
    post<EnvironmentView>(`/agents/${agentId}/environment/${action}`, { ...scope, expected_revision: expectedRevision }),
};
