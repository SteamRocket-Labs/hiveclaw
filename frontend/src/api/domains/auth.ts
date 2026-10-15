/**
 * Auth domain adapter — login, register, profile, password.
 */

import { get, post, put, patch } from '../core';
import type { User, TokenResponse } from '../../types';

export interface LoginParams {
  username: string;
  password: string;
}

export interface RegisterParams {
  username: string;
  email: string;
  password: string;
  display_name?: string;
  invitation_code?: string;
}

export interface PasswordChangeParams {
  old_password: string;
  new_password: string;
}

export interface ProfileUpdateParams {
  username?: string;
  display_name?: string;
  avatar_url?: string;
  title?: string;
  department?: string;
}

export interface RegistrationConfig {
  invitation_code_required: boolean;
}

export interface FeishuSsoInitResponse {
  session_id: string;
  authorize_url: string;
}

export interface FeishuSsoPollResponse {
  status: 'pending' | 'completed' | 'expired' | 'error';
  access_token?: string;
  user?: User;
  detail?: string;
  code?: string;
}

type FeishuTranslator = (key: string, defaultValue: string) => string;

export function feishuConflictMessage(
  code: string | undefined,
  translate: FeishuTranslator,
  scope: 'auth' | 'account' = 'auth',
): string | null {
  if (code === 'email_only_candidate') {
    return scope === 'account'
      ? translate('account.feishuEmailOnlyCandidate', 'This sign-in matches an email only. Nothing was linked. An administrator can repair the directory link.')
      : translate('auth.feishu.emailOnlyCandidate', 'This sign-in matches an email only. Nothing was linked. An administrator can repair the directory link.');
  }
  if (code === 'contradictory_subjects') {
    return scope === 'account'
      ? translate('account.feishuContradictorySubjects', 'The Feishu subjects belong to different accounts. Nothing was linked.')
      : translate('auth.feishu.contradictorySubjects', 'The Feishu subjects belong to different accounts. Nothing was linked.');
  }
  if (code === 'external_identity_conflict') {
    return scope === 'account'
      ? translate('account.feishuExternalIdentityConflict', 'This Feishu subject is already linked to another account. Nothing was changed.')
      : translate('auth.feishu.externalIdentityConflict', 'This Feishu subject is already linked to another account. Nothing was changed.');
  }
  if (code === 'provider_disabled') {
    return scope === 'account'
      ? translate('account.feishuProviderDisabled', 'Feishu sign-in is disabled for this company.')
      : translate('auth.feishu.providerDisabled', 'Feishu sign-in is disabled for this company.');
  }
  if (code === 'inactive_user') {
    return scope === 'account'
      ? translate('account.feishuInactiveUser', 'This account is inactive. Nothing was changed.')
      : translate('auth.feishu.inactiveUser', 'This account is inactive. Nothing was changed.');
  }
  return null;
}

function normalizeLoginParams(data: LoginParams): LoginParams {
  return {
    ...data,
    username: data.username.trim(),
  };
}

export const authApi = {
  login: (data: LoginParams) => post<TokenResponse>('/auth/login', normalizeLoginParams(data)),
  register: (data: RegisterParams) => post<TokenResponse>('/auth/register', data),
  getMe: () => get<User>('/auth/me'),
  updateMe: (data: ProfileUpdateParams) => patch<User>('/auth/me', data),
  changePassword: (data: PasswordChangeParams) => put<void>('/auth/me/password', data),
  getRegistrationConfig: () => get<RegistrationConfig>('/auth/registration-config'),
  feishuSsoAvailable: () => get<{ available: boolean }>('/auth/feishu/sso/available'),
  feishuSsoInit: () => post<FeishuSsoInitResponse>('/auth/feishu/sso/init', {}),
  feishuSsoPoll: (sessionId: string) => get<FeishuSsoPollResponse>(`/auth/feishu/sso/poll?session_id=${sessionId}`),
  feishuBindInit: () => post<FeishuSsoInitResponse>('/auth/feishu/bind/init', {}),
};
