import { useState, useEffect } from 'react';
import { useNavigate } from 'react-router-dom';
import { useTranslation } from 'react-i18next';
import { useAuthStore } from '../stores';
import { authApi } from '../api/domains/auth';
import { systemApi } from '../api/domains/system';
import type { User } from '../types';
import { AuthShell } from './auth/AuthShell';

export default function CompanySetup() {
    const { t, i18n } = useTranslation();
    const navigate = useNavigate();
    const { user, setAuth } = useAuthStore();
    const [allowCreate, setAllowCreate] = useState(true);
    const [loading, setLoading] = useState(false);
    const [error, setError] = useState('');

    // Join company form
    const [inviteCode, setInviteCode] = useState('');
    // Create company form
    const [companyName, setCompanyName] = useState('');
    const isChinese = i18n.language?.toLowerCase().startsWith('zh');

    useEffect(() => {
        // Check if self-creation is allowed
        systemApi.getRegistrationConfig().then((d: any) => {
            setAllowCreate(d.allow_self_create_company);
        }).catch(() => {});
    }, []);

    // If user already has a company, redirect home
    useEffect(() => {
        if (user?.tenant_id) {
            navigate('/');
        }
    }, [user, navigate]);

    const refreshUser = async (tokenOverride?: string) => {
        try {
            if (tokenOverride) {
                localStorage.setItem('token', tokenOverride);
            }
            const me = await authApi.getMe();
            const token = tokenOverride || useAuthStore.getState().token;
            if (token) setAuth(me, token);
        } catch { /* ignore */ }
    };

    const handleJoin = async (e: React.FormEvent) => {
        e.preventDefault();
        setError('');
        setLoading(true);
        try {
            const normalizedInviteCode = inviteCode.trim().toUpperCase();
            const result = await systemApi.joinTenant({ invitation_code: normalizedInviteCode });
            if (user) {
                setAuth({
                    ...user,
                    tenant_id: result.tenant.id,
                    role: result.role as User['role'],
                }, result.access_token);
            } else {
                await refreshUser(result.access_token);
            }
            navigate('/');
        } catch (err: any) {
            setError(err.message || 'Failed to join company');
        } finally {
            setLoading(false);
        }
    };

    const handleCreate = async (e: React.FormEvent) => {
        e.preventDefault();
        setError('');
        setLoading(true);
        try {
            await systemApi.createTenant({ name: companyName });
            await refreshUser();
            // Navigate to Enterprise Settings to configure LLM models
            navigate('/enterprise');
        } catch (err: any) {
            setError(err.message || 'Failed to create company');
        } finally {
            setLoading(false);
        }
    };

    const toggleLang = () => {
        i18n.changeLanguage(isChinese ? 'en' : 'zh');
    };

    return (
        <AuthShell languageLabel={isChinese ? 'EN' : '中文'} onToggleLanguage={toggleLang}>
            <section className="login-auth-card company-setup-card" aria-label={t('companySetup.title', 'Set Up Your Workspace')}>
                <div className="company-setup-kicker">{t('companySetup.kicker', 'Workspace access')}</div>
                <div className="login-form-header">
                    <h1 className="login-form-title">{t('companySetup.title', 'Set Up Your Workspace')}</h1>
                    <p className="login-form-subtitle">
                        {t('companySetup.subtitle', 'Join an existing company or create your own to get started.')}
                    </p>
                </div>

                {error && (
                    <div className="login-error company-setup-error" role="alert">
                        <span aria-hidden="true">!</span>
                        <span>{error}</span>
                    </div>
                )}

                <div className={`company-choice-list ${!allowCreate ? 'single' : ''}`}>
                    <form className="company-choice-card" onSubmit={handleJoin}>
                        <div className="company-choice-header">
                            <span className="company-choice-icon">→</span>
                            <div>
                                <h3>{t('companySetup.joinTitle', 'Join a Company')}</h3>
                                <p>{t('companySetup.joinDesc', 'Enter the invitation code provided by your company administrator.')}</p>
                            </div>
                        </div>
                        <div className="login-field">
                            <label htmlFor="company-invitation-code">{t('companySetup.inviteCode', 'Invitation Code')}</label>
                            <input
                                id="company-invitation-code"
                                className="company-invite-input"
                                value={inviteCode}
                                onChange={(e) => setInviteCode(e.target.value.toUpperCase())}
                                required
                                placeholder={t('companySetup.inviteCodePlaceholder', 'e.g. ABC12345')}
                            />
                        </div>
                        <button className="login-submit" type="submit" disabled={loading || !inviteCode.trim()}>
                            {loading ? <span className="login-spinner" /> : (
                                <>
                                    {t('companySetup.joinBtn', 'Join Company')}
                                    <span className="login-submit-arrow" aria-hidden="true">→</span>
                                </>
                            )}
                        </button>
                    </form>

                    {allowCreate && (
                        <form className="company-choice-card" onSubmit={handleCreate}>
                            <div className="company-choice-header">
                                <span className="company-choice-icon company-choice-icon-honey">H</span>
                                <div>
                                    <h3>{t('companySetup.createTitle', 'Create a Company')}</h3>
                                    <p>{t('companySetup.createDesc', 'Start a new workspace. You can invite team members later.')}</p>
                                </div>
                            </div>
                            <div className="login-field">
                                <label htmlFor="company-setup-name">{t('companySetup.companyName', 'Company Name')}</label>
                                <input
                                    id="company-setup-name"
                                    value={companyName}
                                    onChange={(e) => setCompanyName(e.target.value)}
                                    required
                                    placeholder={t('companySetup.companyNamePlaceholder', 'e.g. Acme Inc.')}
                                />
                            </div>
                            <button className="login-submit login-submit-secondary" type="submit" disabled={loading || !companyName}>
                                {loading ? <span className="login-spinner" /> : t('companySetup.createBtn', 'Create Company')}
                            </button>
                        </form>
                    )}
                </div>

                {!allowCreate && (
                    <p className="company-setup-hint">
                        {t('companySetup.contactAdmin', 'Contact your platform administrator for an invitation code.')}
                    </p>
                )}
            </section>
        </AuthShell>
    );
}
