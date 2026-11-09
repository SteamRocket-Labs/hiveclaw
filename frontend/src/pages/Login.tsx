import { useState, useRef, useCallback, useEffect } from 'react';
import { useNavigate, useSearchParams } from 'react-router-dom';
import { useTranslation } from 'react-i18next';
import { useAuthStore } from '../stores';
import { authApi, feishuConflictMessage, type RegistrationConfig, type OidcConfig } from '../api/domains/auth';
import { ApiError } from '../api/core/errors';
import { showAppToast } from '../components/AppDialogs';
import { safePostLoginRedirect } from '../routing/authRedirect';
import { prepareOidcLogin } from '../routing/oidcLogin';
import { AuthShell } from './auth/AuthShell';

type RegisterConflict = {
    field?: string;
    code?: string;
    suggest_login?: boolean;
};

export default function Login() {
    const { t, i18n } = useTranslation();
    const navigate = useNavigate();
    const [searchParams] = useSearchParams();
    const setAuth = useAuthStore((s) => s.setAuth);
    const postLoginRedirect = safePostLoginRedirect(searchParams.get('next'));
    const [isRegister, setIsRegister] = useState(false);
    const [error, setError] = useState('');
    const [suggestLogin, setSuggestLogin] = useState(false);
    const [loading, setLoading] = useState(false);
    const [feishuLoading, setFeishuLoading] = useState(false);
    const feishuPollRef = useRef(false);
    const [registration, setRegistration] = useState<RegistrationConfig | null>(null);
    const [oidc, setOidc] = useState<OidcConfig | null>(null);
    const tenantSlug = searchParams.get('tenant_slug') || undefined;

    useEffect(() => {
        let active = true;
        authApi.getRegistrationConfig().then(config => {
            if (active) setRegistration(config);
        }).catch(() => { if (active) setError(t('auth.serverUnreachable')); });
        authApi.getOidcConfig(tenantSlug).then(config => {
            if (active) setOidc(config);
        }).catch(() => { /* Password sign-in remains available. */ });
        return () => { active = false; };
    }, [tenantSlug, t]);

    const [form, setForm] = useState({
        username: '',
        password: '',
        email: '',
        install_token: '',
    });
    const isChinese = i18n.language?.toLowerCase().startsWith('zh');

    const handleFeishuLogin = useCallback(async () => {
        setError('');
        setFeishuLoading(true);
        try {
            const { session_id, authorize_url } = await authApi.feishuSsoInit();
            // Open feishu auth in a popup
            const popup = window.open(authorize_url, 'feishu_sso', 'width=600,height=700,popup=yes');

            // Poll for completion
            feishuPollRef.current = true;
            let attempts = 0;
            const maxAttempts = 120;
            while (feishuPollRef.current && attempts < maxAttempts) {
                await new Promise((r) => setTimeout(r, 1500));
                if (!feishuPollRef.current) break;
                try {
                    const res = await authApi.feishuSsoPoll(session_id);
                    if (res.status === 'completed' && res.access_token && res.user) {
                        popup?.close();
                        setAuth(res.user, res.access_token);
                        navigate(postLoginRedirect);
                        return;
                    }
                    if (res.status === 'expired' || res.status === 'error') {
                        popup?.close();
                        setError(feishuConflictMessage(res.code, t) || res.detail || t('auth.feishu.ssoFailed', 'Feishu login failed'));
                        break;
                    }
                } catch {
                    // network blip, keep polling
                }
                if (popup?.closed) break;
                attempts++;
            }
        } catch (err: any) {
            const msg = err?.message || '';
            if (msg.includes('503') || msg.includes('not configured')) {
                setError(t('auth.feishu.notConfigured', 'Feishu login is not configured. Contact admin.'));
            } else {
                setError(msg || t('auth.feishu.initFailed', 'Failed to start Feishu login'));
            }
        } finally {
            feishuPollRef.current = false;
            setFeishuLoading(false);
        }
    }, [navigate, postLoginRedirect, setAuth, t]);

    const toggleLang = () => {
        i18n.changeLanguage(isChinese ? 'en' : 'zh');
    };

    const handleSubmit = async (e: React.FormEvent) => {
        e.preventDefault();
        setError('');
        setSuggestLogin(false);
        setLoading(true);

        try {
            let res;
            if (isRegister) {
                res = await authApi.register({
                    username: form.username, email: form.email, password: form.password,
                    display_name: form.username,
                    ...(registration?.install_token_required ? { install_token: form.install_token } : {}),
                });
            } else {
                res = await authApi.login({ username: form.username, password: form.password });
            }
            setAuth(res.user, res.access_token);
            // Feishu-imported users log in with the shared default "123456".
            if (res.needs_password_change) {
                showAppToast(t(
                    'auth.passwordChangeReminder',
                    'You are using the default password. For security, please change it in Settings → Account → Change Password.',
                ), 'info');
            }
            // Redirect to company setup if user has no company assigned
            if (res.needs_company_setup) {
                navigate('/setup-company');
            } else {
                navigate(postLoginRedirect);
            }
        } catch (err: any) {
            // Structured 409 on register — surface the specific clashing field
            // and offer "go log in" when the email is already taken.
            if (isRegister && err instanceof ApiError && err.status === 409) {
                const detail = (err.data ?? {}) as RegisterConflict;
                if (detail.code === 'email_taken') {
                    setError(t('auth.registerEmailTaken', 'This email is already registered.'));
                    setSuggestLogin(!!detail.suggest_login);
                } else if (detail.code === 'username_taken') {
                    setError(t('auth.registerUsernameTaken', 'This username is already taken. Please choose another.'));
                } else {
                    setError(err.message);
                }
                return;
            }
            if (err instanceof TypeError || err instanceof ApiError && err.status === 0) {
                setError(t('auth.serverUnreachable', 'Unable to reach server. Please check if the service is running and try again.'));
                return;
            }
            if (err instanceof ApiError && err.status >= 500 && (
                err.data == null || typeof err.data === 'object' && 'parseError' in err.data
            )) {
                setError(t('auth.serverStarting', 'Service is starting up or experiencing issues. Please try again in a few seconds.'));
                return;
            }
            const msg = err.message || '';
            // Server-returned error messages (e.g. disabled company, invalid credentials)
            if (msg) {
                // Translate known error messages
                if (msg === 'Public registration is closed. Sign in with an existing account or SSO.') {
                    setError(t('auth.registrationClosed'));
                } else if (msg === 'A valid install token is required for the first administrator.') {
                    setError(t('auth.installTokenRequired'));
                } else if (msg === 'Set INSTALL_TOKEN on the server before creating the first administrator.') {
                    setError(t('auth.installNotReady'));
                } else if (msg.includes('company has been disabled')) {
                    setError(t('auth.companyDisabled', 'Your company has been disabled. Please contact the platform administrator.'));
                } else if (msg.includes('Invalid credentials')) {
                    setError(t('auth.invalidCredentials', 'Invalid username or password.'));
                } else if (msg.includes('Account is disabled')) {
                    setError(t('auth.accountDisabled', 'Your account has been disabled.'));
                } else {
                    setError(msg);
                }
            } else {
                setError(t('auth.serverUnreachable', 'Unable to reach server. Please check if the service is running and try again.'));
            }
        } finally {
            setLoading(false);
        }
    };

    return (
        <AuthShell languageLabel={isChinese ? 'EN' : '中文'} onToggleLanguage={toggleLang}>
                <section className="login-auth-card" aria-label={isRegister ? t('auth.register') : t('auth.login')}>
                    <div className="login-mode-switch" role="tablist" aria-label={t('auth.modeSwitch', 'Authentication mode')}>
                        <button
                            type="button"
                            role="tab"
                            aria-selected={!isRegister}
                            className={!isRegister ? 'active' : ''}
                            onClick={() => {
                                setIsRegister(false);
                                setError('');
                                setSuggestLogin(false);
                            }}
                        >
                            {t('auth.login')}
                        </button>
                        {(registration?.registration_enabled || registration?.install_token_required) && <button
                            type="button"
                            role="tab"
                            aria-selected={isRegister}
                            className={isRegister ? 'active' : ''}
                            onClick={() => {
                                setIsRegister(true);
                                setError('');
                                setSuggestLogin(false);
                            }}
                        >
                            {t('auth.register')}
                        </button>}
                    </div>

                    <div className="login-form-header">
                        <h2 className="login-form-title">{isRegister ? t('auth.createAccountTitle') : t('auth.welcomeBack')}</h2>
                        <p className="login-form-subtitle">
                            {isRegister
                                ? t('auth.subtitleRegister')
                                : t('auth.subtitleLogin')}
                        </p>
                    </div>

                    {error && (
                        <div className="login-error">
                            <span aria-hidden="true">!</span>
                            <span>{error}</span>
                            {suggestLogin && (
                                <button
                                    type="button"
                                    className="login-error-action"
                                    onClick={() => {
                                        setIsRegister(false);
                                        setError('');
                                        setSuggestLogin(false);
                                    }}
                                >
                                    {t('auth.goLogin', 'Go to login')}
                                </button>
                            )}
                        </div>
                    )}

                    <form onSubmit={handleSubmit} className="login-form">
                        <div className="login-field">
                            <label>{isRegister ? t('auth.username') : t('auth.identifierLabel')}</label>
                            <input
                                value={form.username}
                                onChange={(e) => setForm({ ...form, username: e.target.value })}
                                required
                                autoFocus
                                placeholder={isRegister ? t('auth.usernamePlaceholder') : t('auth.loginIdentifierPlaceholder', 'Username or email')}
                            />
                        </div>

                        {isRegister && (
                            <div className="login-field">
                                <label>{t('auth.email')}</label>
                                <input
                                    type="email"
                                    value={form.email}
                                    onChange={(e) => setForm({ ...form, email: e.target.value })}
                                    required
                                    placeholder={t('auth.emailPlaceholder')}
                                />
                            </div>
                        )}

                        <div className="login-field">
                            <label>{t('auth.password')}</label>
                            <input
                                type="password"
                                value={form.password}
                                onChange={(e) => setForm({ ...form, password: e.target.value })}
                                required
                                placeholder={t('auth.passwordPlaceholder')}
                            />
                        </div>

                        {isRegister && registration?.install_token_required && (
                            <div className="login-field">
                                <label htmlFor="install-token">{t('auth.installToken')}</label>
                                <input id="install-token" type="password" autoComplete="off"
                                    value={form.install_token}
                                    onChange={e => setForm({ ...form, install_token: e.target.value })} />
                            </div>
                        )}

                        <button className="login-submit" type="submit" disabled={loading}>
                            {loading ? (
                                <span className="login-spinner" />
                            ) : (
                                <>
                                    {isRegister ? t('auth.register') : t('auth.login')}
                                    <span className="login-submit-arrow" aria-hidden="true">→</span>
                                </>
                            )}
                        </button>
                    </form>

                    <div className="login-divider">
                        <span />
                        <em>{t('auth.feishu.or', 'or')}</em>
                        <span />
                    </div>

                    {oidc?.configured && (
                        <button type="button" className="login-submit login-submit-secondary" disabled={loading || feishuLoading}
                            onClick={() => {
                                try {
                                    window.location.assign(prepareOidcLogin(oidc, postLoginRedirect, `/login?${searchParams.toString()}`));
                                } catch {
                                    setError(t('auth.oidc.failed'));
                                }
                            }}>
                            {oidc.display_name}
                        </button>
                    )}

                    <button
                        type="button"
                        className="login-submit login-submit-secondary"
                        disabled={feishuLoading || loading}
                        onClick={handleFeishuLogin}
                    >
                        {feishuLoading ? (
                            <span className="login-spinner" />
                        ) : (
                            <>
                                <span className="login-sso-mark" aria-hidden="true">飞</span>
                                {t('auth.feishu.login', 'Login with Feishu')}
                            </>
                        )}
                    </button>

                    {(isRegister || registration?.registration_enabled || registration?.install_token_required) && <div className="login-switch">
                        {isRegister ? t('auth.hasAccount') : t('auth.noAccount')}{' '}
                        <a href="#" onClick={(e) => { e.preventDefault(); setIsRegister(!isRegister); setError(''); }}>
                            {isRegister ? t('auth.goLogin') : t('auth.goRegister')}
                        </a>
                    </div>}

                    {isRegister && (
                        <p className="login-legal">
                            {t('auth.legalPrefix')}
                        </p>
                    )}
                </section>
        </AuthShell>
    );
}
