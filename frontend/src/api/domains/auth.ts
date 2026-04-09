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

export const authApi = {
  login: (data: LoginParams) => post<TokenResponse>('/auth/login', data),
  register: (data: RegisterParams) => post<TokenResponse>('/auth/register', data),
  getMe: () => get<User>('/auth/me'),
  updateMe: (data: ProfileUpdateParams) => patch<User>('/auth/me', data),
  changePassword: (data: PasswordChangeParams) => put<void>('/auth/me/password', data),
  getRegistrationConfig: () => get<RegistrationConfig>('/auth/registration-config'),
};
