/**
 * Unified HTTP request layer for the HiveClaw frontend.
 *
 * All API calls go through this module. Pages must NOT use raw fetch().
 * Token is read from localStorage (same source as zustand AuthStore).
 */

import { ApiError } from './errors';

const API_BASE = '/api';
export interface RequestOptions {
  signal?: AbortSignal;
}

function getToken(): string | null {
  return localStorage.getItem('token');
}

function authHeaders(): Record<string, string> {
  const headers: Record<string, string> = {};
  const token = getToken();
  if (token) headers['Authorization'] = `Bearer ${token}`;
  const tenantId = localStorage.getItem('current_tenant_id');
  if (tenantId) headers['X-Tenant-Id'] = tenantId;
  return headers;
}

function isAuthEndpoint(url: string): boolean {
  return url.startsWith('/auth/login') || url.startsWith('/auth/register');
}

function handleUnauthorized(): never {
  localStorage.removeItem('token');
  localStorage.removeItem('user');
  window.location.href = '/login';
  throw new ApiError(401, 'Session expired');
}

async function parseErrorDetail(res: Response): Promise<{ message: string; data: unknown }> {
  try {
    const body = await res.json();
    const raw = body.detail;
    if (Array.isArray(raw)) {
      const message = raw.map((e: { loc?: string[]; msg: string }) => {
        const field = e.loc?.slice(-1)[0] || '';
        return field ? `${field}: ${e.msg}` : e.msg;
      }).join('; ');
      return { message, data: raw };
    }
    if (raw && typeof raw === 'object') {
      const message = typeof raw.message === 'string' ? raw.message : `HTTP ${res.status}`;
      return { message, data: raw };
    }
    return { message: raw || `HTTP ${res.status}`, data: raw };
  } catch (parseErr) {
    return { message: `HTTP ${res.status}`, data: { parseError: String(parseErr) } };
  }
}

/**
 * Core JSON request. All domain adapters delegate to this.
 */
export async function request<T>(
  method: string,
  path: string,
  body?: unknown,
  options?: RequestOptions,
): Promise<T> {
  const headers: Record<string, string> = {
    'Content-Type': 'application/json',
    ...authHeaders(),
  };

  const res = await fetch(`${API_BASE}${path}`, {
    method,
    headers,
    body: body !== undefined ? JSON.stringify(body) : undefined,
    signal: options?.signal,
  });

  if (!res.ok) {
    if (res.status === 401 && !isAuthEndpoint(path)) handleUnauthorized();
    const { message, data } = await parseErrorDetail(res);
    throw new ApiError(res.status, message, data);
  }

  if (res.status === 204) return undefined as T;
  return res.json();
}

/** Convenience shortcuts */
export const get = <T>(path: string, options?: RequestOptions) => request<T>('GET', path, undefined, options);
export const post = <T>(path: string, body?: unknown, options?: RequestOptions) => request<T>('POST', path, body, options);
export const put = <T>(path: string, body?: unknown, options?: RequestOptions) => request<T>('PUT', path, body, options);
export const patch = <T>(path: string, body?: unknown, options?: RequestOptions) => request<T>('PATCH', path, body, options);
export const del = <T = void>(path: string, options?: RequestOptions) => request<T>('DELETE', path, undefined, options);

/**
 * Blob download helper for CSV/file exports that still need auth + unified errors.
 */
export async function getBlob(path: string, options?: RequestOptions): Promise<Blob> {
  const res = await fetch(`${API_BASE}${path}`, {
    method: 'GET',
    headers: authHeaders(),
    signal: options?.signal,
  });

  if (!res.ok) {
    if (res.status === 401 && !isAuthEndpoint(path)) handleUnauthorized();
    const { message, data } = await parseErrorDetail(res);
    throw new ApiError(res.status, message, data);
  }

  return res.blob();
}

/**
 * Multipart file upload — the one case where we skip JSON content-type.
 */
export async function upload<T = unknown>(
  path: string,
  file: File,
  extraFields?: Record<string, string>,
): Promise<T> {
  const formData = new FormData();
  formData.append('file', file);
  if (extraFields) {
    for (const [k, v] of Object.entries(extraFields)) {
      formData.append(k, v);
    }
  }

  const res = await fetch(`${API_BASE}${path}`, {
    method: 'POST',
    headers: authHeaders(),
    body: formData,
  });

  if (!res.ok) {
    if (res.status === 401 && !isAuthEndpoint(path)) handleUnauthorized();
    const { message, data } = await parseErrorDetail(res);
    throw new ApiError(res.status, message, data);
  }

  return res.json();
}
