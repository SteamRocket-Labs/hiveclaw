import type { OidcConfig } from '../api/domains/auth';

export const OIDC_ATTEMPT_KEY = 'oidc_login_attempt';

export interface OidcAttempt {
  state: string;
  nonce: string;
  tenant_id: string;
  redirect_uri: string;
  next: string;
  loginPath: string;
}

export function prepareOidcLogin(config: OidcConfig, next: string, loginPath: string): string {
  const url = new URL(config.authorization_endpoint);
  if (url.protocol !== 'https:' && !(url.protocol === 'http:' && ['localhost', '127.0.0.1'].includes(url.hostname))) {
    throw new Error('OIDC authorization endpoint must use HTTPS');
  }
  const random = () => Array.from(crypto.getRandomValues(new Uint8Array(32)), byte => byte.toString(16).padStart(2, '0')).join('');
  const attempt: OidcAttempt = {
    state: random(), nonce: random(), tenant_id: config.tenant_id,
    redirect_uri: `${window.location.origin}/auth/oidc/callback`, next, loginPath,
  };
  url.searchParams.set('response_type', 'code');
  url.searchParams.set('client_id', config.client_id);
  url.searchParams.set('scope', config.scopes);
  url.searchParams.set('redirect_uri', attempt.redirect_uri);
  url.searchParams.set('state', attempt.state);
  url.searchParams.set('nonce', attempt.nonce);
  sessionStorage.setItem(OIDC_ATTEMPT_KEY, JSON.stringify(attempt));
  return url.toString();
}
