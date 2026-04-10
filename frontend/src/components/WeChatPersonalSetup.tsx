import { useState, useEffect, useCallback, useRef } from 'react';
import { useTranslation } from 'react-i18next';
import { QRCodeSVG } from 'qrcode.react';
import { channelApi } from '../api/domains/channels';

interface WeChatPersonalSetupProps {
    agentId: string;
    onConnected?: () => void;
}

type Phase = 'idle' | 'loading-qr' | 'scanning' | 'connecting' | 'done' | 'error';

export default function WeChatPersonalSetup({ agentId, onConnected }: WeChatPersonalSetupProps) {
    const { t } = useTranslation();
    const [phase, setPhase] = useState<Phase>('idle');
    const [qrUrl, setQrUrl] = useState<string | null>(null);
    const [sessionKey, setSessionKey] = useState<string>('');
    const [errorMsg, setErrorMsg] = useState<string>('');
    const [statusMsg, setStatusMsg] = useState<string>('');
    const pollRef = useRef(false);

    const startQrFlow = useCallback(async () => {
        setPhase('loading-qr');
        setErrorMsg('');
        setStatusMsg('');
        try {
            const res = await channelApi.wechatPersonalQrStart(agentId);
            if (!res.qr_image_url) {
                setPhase('error');
                setErrorMsg(res.message || t('wechatPersonal.qr.fetchFailed', 'Failed to fetch QR code'));
                return;
            }
            setQrUrl(res.qr_image_url);
            setSessionKey(res.session_key);
            setPhase('scanning');
        } catch (e: unknown) {
            setPhase('error');
            setErrorMsg(String(e));
        }
    }, [agentId, t]);

    // Poll for scan status once we enter 'scanning' phase
    useEffect(() => {
        if (phase !== 'scanning' || !sessionKey) return;
        pollRef.current = true;

        let cancelled = false;
        const poll = async () => {
            while (pollRef.current && !cancelled) {
                try {
                    const res = await channelApi.wechatPersonalQrWait(agentId, sessionKey);

                    if (cancelled) break;

                    // QR refreshed (expired + auto-refresh)
                    if (res.qr_image_url) {
                        setQrUrl(res.qr_image_url);
                    }

                    if (res.connected && res.session_key) {
                        // Scan confirmed — finalize connection (credentials stay server-side)
                        setPhase('connecting');
                        setStatusMsg(res.message);
                        try {
                            await channelApi.wechatPersonalConnect(agentId, res.session_key);
                            setPhase('done');
                            onConnected?.();
                        } catch (e: unknown) {
                            setPhase('error');
                            setErrorMsg(String(e));
                        }
                        return;
                    }

                    setStatusMsg(res.message);

                    // If not connected and no QR refresh, it's a terminal failure
                    if (!res.connected && !res.qr_image_url && res.message !== '等待扫码中...' && res.message !== '已扫码，请在微信上确认') {
                        setPhase('error');
                        setErrorMsg(res.message);
                        return;
                    }
                } catch (e: unknown) {
                    if (!cancelled) {
                        setPhase('error');
                        setErrorMsg(String(e));
                    }
                    return;
                }
            }
        };

        poll();
        return () => {
            cancelled = true;
            pollRef.current = false;
        };
    }, [phase, sessionKey, agentId, onConnected]);

    const handleDisconnect = async () => {
        try {
            await channelApi.wechatPersonalDisconnect(agentId);
            setPhase('idle');
            setQrUrl(null);
            setSessionKey('');
            onConnected?.();
        } catch (e: unknown) {
            setErrorMsg(String(e));
        }
    };

    // ── Styles ──
    const containerStyle: React.CSSProperties = {
        padding: '16px',
        textAlign: 'center',
    };
    const qrContainerStyle: React.CSSProperties = {
        display: 'inline-block',
        padding: '12px',
        background: '#fff',
        borderRadius: '12px',
        boxShadow: '0 2px 8px rgba(0,0,0,0.08)',
        margin: '12px 0',
    };
    const btnStyle: React.CSSProperties = {
        padding: '8px 20px',
        fontSize: '13px',
        fontWeight: 500,
        borderRadius: '8px',
        border: '1px solid var(--border-color)',
        background: 'var(--accent-primary)',
        color: '#fff',
        cursor: 'pointer',
        marginTop: '8px',
    };
    const btnOutlineStyle: React.CSSProperties = {
        ...btnStyle,
        background: 'transparent',
        color: 'var(--text-primary)',
        border: '1px solid var(--border-color)',
    };
    const statusStyle: React.CSSProperties = {
        fontSize: '12px',
        color: 'var(--text-secondary)',
        marginTop: '8px',
    };
    const errorStyle: React.CSSProperties = {
        fontSize: '12px',
        color: 'var(--error)',
        marginTop: '8px',
    };
    const successStyle: React.CSSProperties = {
        fontSize: '13px',
        color: 'rgb(16, 185, 129)',
        fontWeight: 500,
        marginTop: '8px',
    };

    // ── Render ──
    return (
        <div style={containerStyle}>
            {phase === 'idle' && (
                <>
                    <div style={{ fontSize: '13px', color: 'var(--text-secondary)', marginBottom: '12px' }}>
                        {t('wechatPersonal.qr.description', 'Scan QR code with WeChat to connect your personal account to this agent.')}
                    </div>
                    <button style={btnStyle} onClick={startQrFlow}>
                        {t('wechatPersonal.qr.startBtn', 'Connect WeChat')}
                    </button>
                </>
            )}

            {phase === 'loading-qr' && (
                <div style={statusStyle}>
                    {t('wechatPersonal.qr.loading', 'Generating QR code...')}
                </div>
            )}

            {phase === 'scanning' && qrUrl && (
                <>
                    <div style={{ fontSize: '13px', color: 'var(--text-secondary)', marginBottom: '8px' }}>
                        {t('wechatPersonal.qr.scanPrompt', 'Scan with WeChat')}
                    </div>
                    <div style={qrContainerStyle}>
                        <QRCodeSVG
                            value={qrUrl}
                            size={200}
                            level="M"
                            bgColor="#ffffff"
                            fgColor="#000000"
                        />
                    </div>
                    <div style={statusStyle}>{statusMsg}</div>
                </>
            )}

            {phase === 'connecting' && (
                <div style={statusStyle}>
                    {statusMsg || t('wechatPersonal.qr.connecting', 'Connecting...')}
                </div>
            )}

            {phase === 'done' && (
                <>
                    <div style={successStyle}>
                        {t('wechatPersonal.qr.success', 'WeChat connected successfully!')}
                    </div>
                    <button style={btnOutlineStyle} onClick={handleDisconnect}>
                        {t('wechatPersonal.disconnect', 'Disconnect')}
                    </button>
                </>
            )}

            {phase === 'error' && (
                <>
                    <div style={errorStyle}>{errorMsg}</div>
                    <button style={btnOutlineStyle} onClick={startQrFlow}>
                        {t('wechatPersonal.qr.retry', 'Retry')}
                    </button>
                </>
            )}
        </div>
    );
}
