import { del, get, patch, post } from '../core/request';

export interface BusinessTeamCaller {
  id: string;
  display_name: string;
  role: string;
  tenant_id: string | null;
  department_id: string | null;
  department_name: string | null;
  can_create: boolean;
  can_install: boolean;
}

export interface BusinessTeamSummary {
  id: string;
  tenant_id: string;
  name: string;
  status: string;
  department_name: string | null;
  created_by_display_name: string;
  my_role: string | null;
  can_manage: boolean;
  member_count: number;
  active_registered_count: number;
}

export interface BusinessTeamMember {
  id: string;
  role: string;
  status: string;
  registered: boolean;
  authority: boolean;
  display_name: string;
  email: string | null;
  user_id: string | null;
  org_member_id: string | null;
  joined_at: string | null;
  left_at: string | null;
}

export interface BusinessTeamAffiliation {
  agent_id: string;
  agent_name: string;
  owner_display_name: string;
  actions: string[];
  effect: string;
  use_access: string;
  execution: string;
  caller_can_use?: boolean;
  manage_granted: boolean;
  home_team: boolean;
  owner_unchanged: boolean;
}

export interface BusinessTeamDetail extends BusinessTeamSummary {
  members: BusinessTeamMember[];
  affiliations: BusinessTeamAffiliation[];
  private_sessions: boolean;
  visible?: boolean;
  access?: string;
  membership_id?: string;
  shared_agent_id?: string;
  removed_agent_id?: string;
}

export interface BusinessTeamList {
  tenant_id: string;
  caller: BusinessTeamCaller;
  teams: BusinessTeamSummary[];
}

export interface BusinessTeamPerson {
  id: string;
  display_name: string;
  email: string | null;
  department_name: string | null;
}

export interface BusinessTeamPeople {
  tenant_id: string;
  people: BusinessTeamPerson[];
}

export const businessTeamsApi = {
  list: () => get<BusinessTeamList>('/business-teams'),
  create: (body: { name: string; department_id?: string | null }) =>
    post<BusinessTeamDetail>('/business-teams', body),
  install: (body: { name: string; first_lead_user_id: string }) =>
    post<BusinessTeamDetail>('/enterprise/business-teams', body),
  read: (teamId: string) => get<BusinessTeamDetail>(`/business-teams/${teamId}`),
  update: (teamId: string, body: { name?: string; status?: string }) =>
    patch<BusinessTeamDetail>(`/business-teams/${teamId}`, body),
  memberCandidates: (teamId: string) =>
    get<BusinessTeamPeople>(`/business-teams/${teamId}/member-candidates`),
  leadCandidates: () => get<BusinessTeamPeople>('/enterprise/business-teams/lead-candidates'),
  addMember: (teamId: string, body: { user_id?: string; org_member_id?: string; role?: string }) =>
    post<BusinessTeamDetail>(`/business-teams/${teamId}/members`, body),
  updateMember: (teamId: string, membershipId: string, body: { role?: string; status?: string }) =>
    patch<BusinessTeamDetail>(`/business-teams/${teamId}/members/${membershipId}`, body),
  shareAgent: (teamId: string, agentId: string) =>
    post<BusinessTeamDetail>(`/business-teams/${teamId}/agents`, { agent_id: agentId }),
  unshareAgent: (teamId: string, agentId: string) =>
    del<BusinessTeamDetail>(`/business-teams/${teamId}/agents/${agentId}`),
};
