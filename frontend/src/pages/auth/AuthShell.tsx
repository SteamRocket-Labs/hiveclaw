import { ReactNode, useEffect } from 'react';
import { useTranslation } from 'react-i18next';

function AuthHoneycomb({ placement }: { placement: 'top' | 'bottom' }) {
    return (
        <div className={`login-honeycomb login-honeycomb-${placement}`} aria-hidden="true">
            {Array.from({ length: 30 }).map((_, index) => (
                <span
                    key={index}
                    className={`login-honeycomb-cell ${index % 4 === 0 ? 'is-lit' : ''}`}
                    style={{
                        left: `${(index % 5) * 39.5}px`,
                        top: `${Math.floor(index / 5) * 52 + ((index % 5) % 2 ? 26 : 0)}px`,
                    }}
                />
            ))}
        </div>
    );
}

type AuthShellProps = {
    children: ReactNode;
    languageLabel: string;
    onToggleLanguage: () => void;
};

export function AuthShell({ children, languageLabel, onToggleLanguage }: AuthShellProps) {
    const { t } = useTranslation();

    useEffect(() => {
        document.documentElement.setAttribute('data-theme', 'light');
    }, []);

    return (
        <div className="login-page">
            <aside className="login-brand-panel">
                <AuthHoneycomb placement="top" />
                <AuthHoneycomb placement="bottom" />

                <div className="login-brand-lockup">
                    <span className="login-hex-mark">H</span>
                    <span>Hive</span>
                </div>

                <div className="login-brand-copy">
                    <h1>
                        {t('auth.brandTitleLine1')}
                        <br />
                        {t('auth.brandTitleLine2Prefix')}
                        <span>{t('auth.brandTitleHighlight')}</span>
                        {t('auth.brandTitleLine2Suffix')}
                    </h1>
                    <p>
                        {t(
                            'auth.brandDescription',
                        )}
                    </p>
                    <div className="login-brand-chips" aria-label={t('auth.brandPillGroup', 'Platform principles')}>
                        <span>{t('auth.brandPillPlan')}</span>
                        <span>{t('auth.brandPillA2A')}</span>
                        <span>{t('auth.brandPillGovernance')}</span>
                    </div>
                </div>

                <div className="login-brand-footer">{t('auth.brandFooter')}</div>
            </aside>

            <main className="login-auth-surface">
                <button className="login-lang-button" type="button" onClick={onToggleLanguage}>
                    <span aria-hidden="true">Aa</span>
                    {languageLabel}
                </button>
                {children}
            </main>
        </div>
    );
}
