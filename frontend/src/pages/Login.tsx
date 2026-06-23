import { useState, useRef, useCallback } from 'react';
import { useNavigate, useSearchParams } from 'react-router-dom';
import { useTranslation } from 'react-i18next';
import { useAuthStore } from '../stores';
import { authApi } from '../api/domains/auth';
import { ApiError } from '../api/core/errors';
import { showAppToast } from '../components/AppDialogs';
import { safePostLoginRedirect } from '../routing/authRedirect';
import { AuthShell } from './auth/AuthShell';

type RegisterConflict = {
    field?: string;
    code?: string;
    suggest_login?: boolean;
    default_password_hint?: boolean;
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

    const [form, setForm] = useState({
        username: '',
        password: '',
        email: '',
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
                        setError(res.detail || t('auth.feishu.ssoFailed', 'Feishu login failed'));
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
                    ...form,
                    display_name: form.username,
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
                if (detail.code === 'email_linked_to_feishu') {
                    setError(t(
                        'auth.registerEmailFromFeishu',
                        'This email was imported from Feishu. Log in with default password 123456 and change it right after.',
                    ));
                    setSuggestLogin(!!detail.suggest_login);
                } else if (detail.code === 'email_taken') {
                    setError(t('auth.registerEmailTaken', 'This email is already registered.'));
                    setSuggestLogin(!!detail.suggest_login);
                } else if (detail.code === 'username_taken') {
                    setError(t('auth.registerUsernameTaken', 'This username is already taken. Please choose another.'));
                } else {
                    setError(err.message);
                }
                return;
            }
            const msg = err.message || '';
            // Server-returned error messages (e.g. disabled company, invalid credentials)
            if (msg && msg !== 'Failed to fetch' && !msg.includes('NetworkError') && !msg.includes('ERR_CONNECTION')) {
                // Translate known error messages
                if (msg.includes('company has been disabled')) {
                    setError(t('auth.companyDisabled', 'Your company has been disabled. Please contact the platform administrator.'));
                } else if (msg.includes('Invalid credentials')) {
                    setError(t('auth.invalidCredentials', 'Invalid username or password.'));
                } else if (msg.includes('Account is disabled')) {
                    setError(t('auth.accountDisabled', 'Your account has been disabled.'));
                } else if (msg.includes('500') || msg.includes('Internal Server Error')) {
                    setError(t('auth.serverStarting', 'Service is starting up or experiencing issues. Please try again in a few seconds.'));
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
                        <button
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
                        </button>
                    </div>

                    <div className="login-form-header">
                        <h2 className="login-form-title">{isRegister ? t('auth.createAccountTitle', '创建你的账户') : t('auth.welcomeBack', '欢迎回来')}</h2>
                        <p className="login-form-subtitle">
                            {isRegister
                                ? t('auth.subtitleRegister', '注册后即可加入或创建一个 workspace。')
                                : t('auth.subtitleLogin', '登录以继续你的数字员工工作区。')}
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
                            <label>{isRegister ? t('auth.username') : t('auth.identifierLabel', '用户名或邮箱')}</label>
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

                    <div className="login-switch">
                        {isRegister ? t('auth.hasAccount') : t('auth.noAccount')}{' '}
                        <a href="#" onClick={(e) => { e.preventDefault(); setIsRegister(!isRegister); setError(''); }}>
                            {isRegister ? t('auth.goLogin') : t('auth.goRegister')}
                        </a>
                    </div>

                    {isRegister && (
                        <p className="login-legal">
                            {t('auth.legalPrefix', '注册即代表同意 Hive 的企业使用条款与隐私政策。')}
                        </p>
                    )}
                </section>
        </AuthShell>
    );
}
