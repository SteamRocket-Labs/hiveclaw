import React, { useState } from 'react';
import { useQueryClient } from '@tanstack/react-query';
import { useTranslation } from 'react-i18next';
import { agentApi } from '../api/domains/agents';

interface OpenClawSettingsProps {
    agent: any;
    agentId: string;
    isAdmin?: boolean;
}

export default function OpenClawSettings({ agent, agentId }: OpenClawSettingsProps) {
    const { t } = useTranslation();
    const queryClient = useQueryClient();
    // ─── API Key state ──────────────────────────────────
    const [apiKey, setApiKey] = useState<string | null>(null);
    const [regenerating, setRegenerating] = useState(false);
    const [showConfirm, setShowConfirm] = useState(false);
    const [copied, setCopied] = useState(false);

    const hasKey = agent?.has_api_key || false;

    const handleRegenerate = async (autoCopy = false) => {
        setRegenerating(true);
        try {
            const result = await agentApi.generateApiKey(agentId);
            setApiKey(result.api_key);
            setShowConfirm(false);
            // Refresh agent data so has_api_key updates
            queryClient.invalidateQueries({ queryKey: ['agent', agentId] });
            if (autoCopy) {
                handleCopy(result.api_key);
            }
        } catch (e) {
            console.error('Failed to regenerate API key', e);
        } finally {
            setRegenerating(false);
        }
    };

    const handleCopy = async (text: string) => {
        try {
            await navigator.clipboard.writeText(text);
            setCopied(true);
            setTimeout(() => setCopied(false), 2000);
        } catch { }
    };

    return (
        <div>
            <h3 style={{ marginBottom: '16px' }}>{t('agent.settings.title')}</h3>

            {/* ── API Key Management ── */}
            <div className="card" style={{ marginBottom: '12px' }}>
                <h4 style={{ marginBottom: '4px' }}>
                    API Key
                </h4>
                <p style={{ fontSize: '12px', color: 'var(--text-tertiary)', marginBottom: '12px' }}>
                    {t('openclaw.apiKeyDesc')}
                </p>

                {/* API Key Display Logic */}
                {(() => {
                    const activeKey = apiKey || (agent?.api_key_hash?.startsWith('oc-') ? agent.api_key_hash : null);
                    const isLegacyHash = hasKey && !activeKey;

                    if (activeKey) {
                        return (
                            <div style={{
                                display: 'flex', alignItems: 'center', gap: '8px',
                                padding: '10px 14px', background: 'rgba(99,102,241,0.06)',
                                borderRadius: '8px', border: '1px solid var(--accent-primary)',
                            }}>
                                <code style={{
                                    flex: 1, fontSize: '13px', fontFamily: 'monospace',
                                    wordBreak: 'break-all', color: 'var(--text-primary)',
                                }}>
                                    {activeKey}
                                </code>
                                <button
                                    className="btn btn-secondary"
                                    onClick={() => handleCopy(activeKey)}
                                    style={{ padding: '4px 12px', fontSize: '12px', whiteSpace: 'nowrap' }}
                                >
                                    {copied ? t('openclaw.copied') : t('openclaw.copy')}
                                </button>
                                <button
                                    className="btn btn-secondary"
                                    onClick={() => setShowConfirm(true)}
                                    style={{ padding: '4px 12px', fontSize: '12px', whiteSpace: 'nowrap' }}
                                >
                                    {t('openclaw.regenerate')}
                                </button>
                            </div>
                        );
                    }

                    return (
                        <div style={{ display: 'flex', alignItems: 'center', gap: '10px' }}>
                            <div style={{
                                flex: 1, padding: '8px 14px', borderRadius: '8px',
                                background: 'var(--bg-elevated)', border: '1px solid var(--border-subtle)',
                                fontFamily: 'monospace', fontSize: '13px', color: 'var(--text-secondary)',
                                letterSpacing: '0.5px',
                            }}>
                                {isLegacyHash ? t('openclaw.legacyKey') : t('openclaw.notGenerated')}
                            </div>
                            <button
                                className="btn btn-secondary"
                                onClick={() => setShowConfirm(true)}
                                style={{ padding: '6px 16px', fontSize: '12px', whiteSpace: 'nowrap' }}
                            >
                                {isLegacyHash ? t('openclaw.regenerate') : t('openclaw.generate')}
                            </button>
                        </div>
                    );
                })()}

                {/* Confirmation dialog */}
                {showConfirm && (
                    <div style={{
                        marginTop: '12px', padding: '14px', borderRadius: '8px',
                        background: hasKey ? 'rgba(255,80,80,0.06)' : 'rgba(99,102,241,0.04)',
                        border: hasKey ? '1px solid rgba(255,80,80,0.2)' : '1px solid var(--border-subtle)',
                    }}>
                        <div style={{ fontSize: '13px', fontWeight: 500, marginBottom: '8px', color: 'var(--text-primary)' }}>
                            {hasKey ? t('openclaw.confirmRegenerate') : t('openclaw.confirmGenerate')}
                        </div>
                        <div style={{ fontSize: '12px', color: 'var(--text-secondary)', marginBottom: '12px' }}>
                            {hasKey ? t('openclaw.regenerateWarning') : t('openclaw.generateDesc')}
                        </div>
                        <div style={{ display: 'flex', gap: '8px', justifyContent: 'flex-end' }}>
                            <button
                                className="btn btn-secondary"
                                onClick={() => setShowConfirm(false)}
                                style={{ padding: '5px 14px', fontSize: '12px' }}
                            >
                                {t('common.cancel')}
                            </button>
                            <button
                                className="btn btn-primary"
                                onClick={() => handleRegenerate(false)}
                                disabled={regenerating}
                                style={{ padding: '5px 14px', fontSize: '12px' }}
                            >
                                {regenerating ? t('openclaw.generating') : t('common.confirm')}
                            </button>
                        </div>
                    </div>
                )}
            </div>
        </div>
    );
}
