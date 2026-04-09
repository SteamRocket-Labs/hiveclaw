import { useEffect, useState } from 'react';
import { useNavigate, useSearchParams } from 'react-router-dom';
import { useTranslation } from 'react-i18next';
import { useAuthStore } from '../stores';
import { authApi } from '../api/domains/auth';

export default function SsoEntry() {
    const { t } = useTranslation();
    const navigate = useNavigate();
    const setAuth = useAuthStore((s) => s.setAuth);
    const [searchParams] = useSearchParams();
    const [status, setStatus] = useState<'polling' | 'success' | 'error'>('polling');
    const [error, setError] = useState('');

    const sessionId = searchParams.get('sid');
    const complete = searchParams.get('complete');

    useEffect(() => {
        if (!sessionId) {
            setStatus('error');
            setError('Missing session ID');
            return;
        }

        let cancelled = false;
        let attempts = 0;
        const maxAttempts = 60;

        const poll = async () => {
            while (!cancelled && attempts < maxAttempts) {
                try {
                    const res = await authApi.feishuSsoPoll(sessionId);
                    if (cancelled) break;

                    if (res.status === 'completed' && res.access_token && res.user) {
                        setAuth(res.user, res.access_token);
                        setStatus('success');
                        setTimeout(() => navigate('/', { replace: true }), 500);
                        return;
                    }
                    if (res.status === 'expired' || res.status === 'error') {
                        setStatus('error');
                        setError(res.detail || t('auth.feishu.ssoFailed', 'Login failed or expired'));
                        return;
                    }
                } catch {
                    if (cancelled) break;
                }
                attempts++;
                await new Promise((r) => setTimeout(r, complete ? 500 : 2000));
            }
            if (!cancelled) {
                setStatus('error');
                setError(t('auth.feishu.ssoTimeout', 'Login timed out'));
            }
        };

        poll();
        return () => { cancelled = true; };
    }, [sessionId, complete, setAuth, navigate, t]);

    return (
        <div style={{
            display: 'flex', alignItems: 'center', justifyContent: 'center',
            minHeight: '100vh', background: 'var(--bg-primary)',
            flexDirection: 'column', gap: '16px',
        }}>
            {status === 'polling' && (
                <>
                    <div className="login-spinner" style={{ width: 32, height: 32 }} />
                    <p style={{ color: 'var(--text-secondary)', fontSize: '14px' }}>
                        {t('auth.feishu.completing', 'Completing login...')}
                    </p>
                </>
            )}
            {status === 'success' && (
                <p style={{ color: 'var(--success)', fontSize: '14px' }}>
                    {t('auth.feishu.success', 'Login successful, redirecting...')}
                </p>
            )}
            {status === 'error' && (
                <div style={{ textAlign: 'center' }}>
                    <p style={{ color: 'var(--error)', fontSize: '14px', marginBottom: '12px' }}>{error}</p>
                    <button className="btn btn-primary" onClick={() => navigate('/login', { replace: true })}>
                        {t('auth.feishu.backToLogin', 'Back to login')}
                    </button>
                </div>
            )}
        </div>
    );
}
