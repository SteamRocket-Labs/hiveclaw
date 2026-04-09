import { useEffect, useState } from 'react';
import { useNavigate } from 'react-router-dom';
import { useTranslation } from 'react-i18next';
import { useQuery } from '@tanstack/react-query';
import { agentApi } from '../api/domains/agents';

/**
 * /agents/new — thin redirect to HR Agent's AgentDetail page (chat tab).
 * The HR Agent is fetched (or lazily created) via GET /agents/system/hr,
 * then the user is redirected to /agents/{hrAgentId}#chat where the full
 * AgentDetail UI handles chat, settings, skills, etc.
 *
 * Query key includes current_tenant_id so switching companies re-fetches.
 */
export default function AgentCreate() {
    const { t } = useTranslation();
    const navigate = useNavigate();
    const [tenantId, setTenantId] = useState(localStorage.getItem('current_tenant_id') || '');

    // Listen for tenant switches
    useEffect(() => {
        const handler = (e: StorageEvent) => {
            if (e.key === 'current_tenant_id') setTenantId(e.newValue || '');
        };
        window.addEventListener('storage', handler);
        return () => window.removeEventListener('storage', handler);
    }, []);

    const { data: hrAgent, isLoading, error } = useQuery({
        queryKey: ['hr-agent', tenantId],
        queryFn: () => agentApi.getHrAgent(),
        retry: 2,
    });

    useEffect(() => {
        if (hrAgent?.id) {
            navigate(`/agents/${hrAgent.id}#chat`, { replace: true });
        }
    }, [hrAgent, navigate]);

    if (isLoading) {
        return (
            <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'center', height: '60vh' }}>
                <div style={{ textAlign: 'center', maxWidth: '320px' }}>
                    <div style={{ fontSize: '32px', marginBottom: '16px' }}>🤖</div>
                    <div className="spinner" style={{ margin: '0 auto 12px' }} />
                    <p style={{ color: 'var(--text-secondary)', fontSize: 'var(--text-sm)' }}>{t('hrChat.loading')}</p>
                    <p style={{ color: 'var(--text-tertiary)', fontSize: 'var(--text-xs)', marginTop: '6px', lineHeight: 1.5 }}>
                        {t('hrChat.welcomeDesc')}
                    </p>
                </div>
            </div>
        );
    }

    if (error) {
        return (
            <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'center', height: '60vh' }}>
                <div style={{ textAlign: 'center', maxWidth: '400px' }}>
                    <div style={{ fontSize: '32px', marginBottom: '12px' }}>⚠️</div>
                    <p style={{ color: 'var(--text-primary)', fontSize: 'var(--text-sm)', fontWeight: 500, marginBottom: '8px' }}>
                        {t('hrChat.loadError')}
                    </p>
                    <div style={{ display: 'flex', gap: '8px', justifyContent: 'center', marginTop: '16px' }}>
                        <button className="btn btn-primary" onClick={() => window.location.reload()}>
                            {t('common.retry')}
                        </button>
                        <button className="btn btn-secondary" onClick={() => navigate('/enterprise#llm')}>
                            {t('enterprise.tabs.llm')}
                        </button>
                    </div>
                </div>
            </div>
        );
    }

    return null;
}
