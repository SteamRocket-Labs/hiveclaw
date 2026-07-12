import { getBlob } from '../api/core';

const CREDENTIAL_QUERY_KEYS = new Set([
  'access_token',
  'api_key',
  'apikey',
  'auth',
  'authorization',
  'bearer',
  'token',
]);

function browserOrigin(): string {
  return typeof window !== 'undefined' && window.location?.origin
    ? window.location.origin
    : 'http://localhost';
}

/** Convert a browser-facing, same-origin `/api/*` URL into the core request path. */
export function apiPathFromBrowserUrl(value: string): string | null {
  try {
    const url = new URL(value, browserOrigin());
    if (url.origin !== browserOrigin() || !url.pathname.startsWith('/api/')) return null;
    for (const key of url.searchParams.keys()) {
      if (CREDENTIAL_QUERY_KEYS.has(key.toLowerCase())) return null;
    }
    return `${url.pathname.slice('/api'.length)}${url.search}${url.hash}`;
  } catch {
    return null;
  }
}

export async function fetchAuthenticatedBrowserResource(value: string): Promise<Blob> {
  const apiPath = apiPathFromBrowserUrl(value);
  if (!apiPath) throw new Error('Only credential-free same-origin API resources may be fetched');
  return getBlob(apiPath);
}

export function saveBlob(blob: Blob, filename: string): void {
  const url = URL.createObjectURL(blob);
  const anchor = document.createElement('a');
  anchor.href = url;
  anchor.download = filename;
  anchor.rel = 'noopener noreferrer';
  anchor.click();
  setTimeout(() => URL.revokeObjectURL(url), 0);
}

export async function downloadAuthenticatedBrowserResource(value: string, filename: string): Promise<void> {
  saveBlob(await fetchAuthenticatedBrowserResource(value), filename);
}
