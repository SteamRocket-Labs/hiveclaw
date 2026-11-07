import { useEffect, useRef, useState } from 'react';
import { Link, useNavigate, useSearchParams } from 'react-router-dom';
import { useTranslation } from 'react-i18next';
import { authApi } from '../api/domains/auth';
import { useAuthStore } from '../stores';
import { OIDC_ATTEMPT_KEY, type OidcAttempt } from '../routing/oidcLogin';
import { safePostLoginRedirect } from '../routing/authRedirect';
import { AuthShell } from './auth/AuthShell';
import { ApiError } from '../api/core/errors';

export default function OidcCallback() {
  const { t, i18n } = useTranslation();
  const navigate = useNavigate();
  const [params] = useSearchParams();
  const setAuth = useAuthStore(s => s.setAuth);
  const started = useRef(false);
  const [error, setError] = useState('');
  const [loginPath, setLoginPath] = useState('/login');
  const chinese = i18n.language?.startsWith('zh');

  useEffect(() => {
    if (started.current) return;
    started.current = true;
    const raw = sessionStorage.getItem(OIDC_ATTEMPT_KEY);
    sessionStorage.removeItem(OIDC_ATTEMPT_KEY);
    const complete = async () => {
      let attempt: OidcAttempt | null = null;
      try { attempt = raw ? JSON.parse(raw) : null; } catch { /* Invalid attempts cannot sign in. */ }
      if (attempt?.loginPath) setLoginPath(safePostLoginRedirect(attempt.loginPath));
      if (!params.get('state') || !attempt?.state || params.get('state') !== attempt.state) {
        setError(t('auth.oidc.stateMismatch'));
        return;
      }
      if (params.get('error') || !params.get('code') || !attempt.nonce) {
        setError(t('auth.oidc.failed'));
        return;
      }
      try {
        const result = await authApi.oidcCallback({
          code: params.get('code')!, nonce: attempt.nonce,
          redirect_uri: attempt.redirect_uri, tenant_id: attempt.tenant_id,
        });
        setAuth(result.user, result.access_token);
        navigate(result.needs_company_setup ? '/setup-company' : safePostLoginRedirect(attempt.next), { replace: true });
      } catch (err) {
        const key = err instanceof ApiError
          ? err.status === 409 ? 'accountConflict' : err.status === 403 ? 'accessDenied' : err.status >= 500 ? 'unavailable' : 'failed'
          : 'failed';
        setError(t(`auth.oidc.${key}`));
      }
    };
    void complete();
  }, [navigate, params, setAuth, t]);

  return <AuthShell languageLabel={chinese ? 'EN' : '中文'} onToggleLanguage={() => i18n.changeLanguage(chinese ? 'en' : 'zh')}>
    <section className="login-auth-card" aria-label={t('auth.oidc.title')}>
      <h2 className="login-form-title">{t('auth.oidc.title')}</h2>
      {error ? <>
        <p className="login-error" role="alert">{error}</p>
        <Link to={loginPath}>{t('auth.goLogin')}</Link>
      </> : <p role="status">{t('common.loading')}</p>}
    </section>
  </AuthShell>;
}
