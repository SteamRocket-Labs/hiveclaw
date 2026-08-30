import React, { Component, type ErrorInfo } from 'react';
import { useTranslation } from 'react-i18next';

export function SessionResolvingSurface() {
  const { t } = useTranslation();
  return (
    <div className="agent-detail-section-fallback" role="status" aria-live="polite">
      {t('agent.chat.resolvingSession', 'Resolving session…')}
    </div>
  );
}

export function SessionAccessErrorSurface({
  status,
  onBack,
}: {
  status: 403 | 404;
  onBack: () => void;
}) {
  const { t } = useTranslation();
  return (
    <div className="agent-detail-error" role="alert" data-testid="session-route-error">
      <div className="agent-detail-error-title">
        {status === 403
          ? t('agent.chat.sessionAccessDenied', 'Session access denied')
          : t('agent.chat.sessionNotFound', 'Session not found')}
      </div>
      <div className="agent-detail-error-message">
        {status === 403
          ? t('agent.chat.sessionAccessDeniedBody', 'You do not have access to this session.')
          : t('agent.chat.sessionNotFoundBody', 'This session does not exist or is not available to your account.')}
      </div>
      <button className="btn btn-primary agent-detail-error-action" onClick={onBack}>
        {t('agent.chat.backToConversations', 'Back to conversations')}
      </button>
    </div>
  );
}

export class AgentDetailErrorBoundary extends Component<
  { children: React.ReactNode },
  { hasError: boolean; error: Error | null }
> {
  state = { hasError: false, error: null as Error | null };

  static getDerivedStateFromError(error: Error) {
    return { hasError: true, error };
  }

  componentDidCatch(error: Error, errorInfo: ErrorInfo) {
    console.error('AgentDetail crash caught by error boundary:', error, errorInfo);
  }

  render() {
    if (!this.state.hasError) return this.props.children;
    return (
      <div className="agent-detail-error">
        <div className="agent-detail-error-title">Something went wrong</div>
        <div className="agent-detail-error-message">
          {this.state.error?.message || 'An unexpected error occurred while loading this page.'}
        </div>
        <button
          className="btn btn-primary agent-detail-error-action"
          onClick={() => {
            this.setState({ hasError: false, error: null });
            window.location.reload();
          }}
        >
          Reload Page
        </button>
      </div>
    );
  }
}
