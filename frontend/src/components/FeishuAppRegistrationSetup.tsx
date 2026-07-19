import { useEffect, useRef, useState } from 'react';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { useTranslation } from 'react-i18next';
import { QRCodeSVG } from 'qrcode.react';

import { ApiError } from '../api/core';
import {
  channelApi,
  type FeishuAppRegistration,
  type FeishuAppRegistrationStatus,
  type FeishuPlatformRegion,
} from '../api/domains/channels';

interface FeishuAppRegistrationSetupProps {
  agentId: string;
  platformRegion: FeishuPlatformRegion;
  onConnected?: () => void;
  onManualConfigure?: () => void;
  onClose?: () => void;
}

type RegistrationPhase = 'checking' | 'idle' | 'initializing' | 'scanning' | 'connecting' | 'done' | 'error';

const POLLING_STATUSES = new Set<FeishuAppRegistrationStatus>([
  'initializing',
  'scanning',
  'polling',
  'slow_down',
  'domain_switched',
  'credentials_received',
  'connecting',
]);

const CANCELLABLE_STATUSES = new Set<FeishuAppRegistrationStatus>([
  'initializing',
  'scanning',
  'polling',
  'slow_down',
  'domain_switched',
]);

export function phaseFromFeishuRegistrationStatus(
  status?: FeishuAppRegistrationStatus | null,
): RegistrationPhase {
  if (!status) return 'idle';
  if (status === 'initializing') return 'initializing';
  if (status === 'scanning' || status === 'polling' || status === 'slow_down' || status === 'domain_switched') {
    return 'scanning';
  }
  if (status === 'credentials_received' || status === 'connecting') return 'connecting';
  if (status === 'connected') return 'done';
  return 'error';
}

export function isFeishuRegistrationCancellable(status?: FeishuAppRegistrationStatus | null): boolean {
  return Boolean(status && CANCELLABLE_STATUSES.has(status));
}

function errorMessage(error: unknown): string {
  return error instanceof Error ? error.message : String(error);
}

function errorCode(error: unknown): string | null {
  if (!(error instanceof ApiError) || !error.data || typeof error.data !== 'object') return null;
  const code = (error.data as { code?: unknown }).code;
  return typeof code === 'string' ? code : null;
}

export default function FeishuAppRegistrationSetup({
  agentId,
  platformRegion,
  onConnected,
  onManualConfigure,
  onClose,
}: FeishuAppRegistrationSetupProps) {
  const { t } = useTranslation();
  const queryClient = useQueryClient();
  const [sessionId, setSessionId] = useState<string | null>(null);
  const connectedNotificationRef = useRef<string | null>(null);

  const activeQuery = useQuery({
    queryKey: ['feishu-app-registration-active', agentId],
    queryFn: ({ signal }) => channelApi.feishuRegistrationActive(agentId, signal),
    retry: false,
    refetchOnWindowFocus: false,
  });

  useEffect(() => {
    if (activeQuery.data?.session_id) {
      setSessionId(activeQuery.data.session_id);
    }
  }, [activeQuery.data?.session_id]);

  const registrationQuery = useQuery({
    queryKey: ['feishu-app-registration', agentId, sessionId],
    queryFn: ({ signal }) => channelApi.feishuRegistrationGet(agentId, sessionId!, signal),
    enabled: Boolean(sessionId),
    retry: false,
    refetchInterval: (query) => {
      const registration = query.state.data as FeishuAppRegistration | undefined;
      return registration && POLLING_STATUSES.has(registration.status) ? 1500 : false;
    },
    refetchIntervalInBackground: false,
  });

  const startMutation = useMutation({
    mutationFn: () => channelApi.feishuRegistrationStart(agentId, platformRegion),
    onSuccess: (registration) => {
      setSessionId(registration.session_id);
      queryClient.setQueryData(
        ['feishu-app-registration', agentId, registration.session_id],
        registration,
      );
      queryClient.setQueryData(['feishu-app-registration-active', agentId], registration);
    },
  });

  const cancelMutation = useMutation({
    mutationFn: (targetSessionId: string) =>
      channelApi.feishuRegistrationCancel(agentId, targetSessionId),
    onSuccess: (registration) => {
      queryClient.setQueryData(
        ['feishu-app-registration', agentId, registration.session_id],
        registration,
      );
      queryClient.setQueryData(['feishu-app-registration-active', agentId], null);
    },
  });

  const registration = registrationQuery.data ?? activeQuery.data ?? null;
  const phase = activeQuery.isLoading && !registration
    ? 'checking'
    : phaseFromFeishuRegistrationStatus(registration?.status);
  const requestError = startMutation.error ?? cancelMutation.error ?? registrationQuery.error ?? activeQuery.error;
  const failureCode = registration?.error_code ?? errorCode(requestError);

  useEffect(() => {
    if (!registration?.connected || registration.status !== 'connected') return;
    if (connectedNotificationRef.current === registration.session_id) return;
    connectedNotificationRef.current = registration.session_id;
    onConnected?.();
  }, [onConnected, registration?.connected, registration?.session_id, registration?.status]);

  const effectivePlatformRegion = registration?.platform_region ?? platformRegion;
  const platformName = effectivePlatformRegion === 'lark_global' ? 'Lark' : t(
    'agent.settings.channel.registration.feishuName',
    'Feishu',
  );
  const scanPrompt = effectivePlatformRegion === 'lark_global'
    ? t('agent.settings.channel.registration.larkScanPrompt', 'Scan with Lark to create or bind the app')
    : t('agent.settings.channel.registration.feishuScanPrompt', 'Scan with Feishu to create or bind the app');
  const scanningStatus = registration?.status === 'domain_switched'
    ? t('agent.settings.channel.registration.domainSwitched', 'Continuing registration on Lark Global…')
    : registration?.status === 'slow_down'
      ? t('agent.settings.channel.registration.slowDown', 'Confirmation is taking longer than expected…')
      : t('agent.settings.channel.registration.waiting', 'Waiting for scan confirmation…');
  const failureText = (() => {
    switch (failureCode) {
      case 'registration_denied':
        return t('agent.settings.channel.registration.denied', 'Registration was cancelled or denied in Feishu/Lark.');
      case 'registration_expired':
        return t('agent.settings.channel.registration.expired', 'The QR code expired. Generate a new one.');
      case 'registration_cancelled':
        return t('agent.settings.channel.registration.cancelled', 'Registration cancelled.');
      case 'registration_interrupted':
        return t('agent.settings.channel.registration.interrupted', 'The registration worker stopped. Generate a new QR code.');
      case 'registration_authorization_lost':
        return t('agent.settings.channel.registration.accessLost', 'Your Agent management access changed. Start again after access is restored.');
      case 'registration_identity_missing':
        return t(
          'agent.settings.channel.registration.identityMissing',
          'Feishu/Lark did not return your verified scanner identity. Generate a new QR code and scan again.',
        );
      case 'websocket_credentials_rejected':
        return t('agent.settings.channel.registration.credentialsRejected', 'Feishu/Lark rejected the app credentials. Scan again.');
      case 'registration_state_unavailable':
        return t('agent.settings.channel.registration.unavailable', 'Registration is temporarily unavailable. Try again shortly.');
      case 'registration_already_active':
        return t('agent.settings.channel.registration.conflict', 'Another manager is already registering this Agent channel.');
      default:
        return requestError
          ? errorMessage(requestError)
          : t('agent.settings.channel.registration.failed', 'Registration failed. Generate a new QR code and try again.');
    }
  })();

  const cancelThen = async (callback: () => void) => {
    try {
      if (registration && isFeishuRegistrationCancellable(registration.status)) {
        await cancelMutation.mutateAsync(registration.session_id);
      }
      callback();
    } catch {
      // React Query owns and renders the typed mutation error.
    }
  };

  return (
    <section className="feishu-registration" aria-live="polite">
      <div className="feishu-registration-heading">
        {t('agent.settings.channel.registration.title', 'Scan to create or bind the app')}
      </div>
      <p className="feishu-registration-description">
        {t(
          'agent.settings.channel.registration.description',
          'Hive uses the official registration flow. App credentials are saved directly on the server and are never shown in the browser.',
        )}
      </p>

      {(phase === 'checking' || phase === 'initializing') && (
        <div className="feishu-registration-state">
          <span className="feishu-registration-spinner" aria-hidden="true" />
          {t('agent.settings.channel.registration.preparing', 'Preparing the official QR code…')}
        </div>
      )}

      {phase === 'idle' && (
        <div className="feishu-registration-idle">
          <div>{scanPrompt}</div>
          <button
            type="button"
            className="btn btn-primary"
            onClick={() => startMutation.mutate()}
            disabled={startMutation.isPending}
          >
            {startMutation.isPending
              ? t('common.loading')
              : t('agent.settings.channel.registration.start', 'Generate QR code')}
          </button>
        </div>
      )}

      {phase === 'scanning' && registration?.verification_url && (
        <div className="feishu-registration-scan">
          <div className="feishu-registration-prompt">{scanPrompt}</div>
          <div className="feishu-registration-qr">
            <QRCodeSVG
              value={registration.verification_url}
              size={208}
              level="M"
              bgColor="#ffffff"
              fgColor="#000000"
              title={`${platformName} QR code`}
            />
          </div>
          <div className="feishu-registration-state">
            {scanningStatus}
          </div>
          <a
            className="feishu-registration-link"
            href={registration.verification_url}
            target="_blank"
            rel="noopener noreferrer"
          >
            {t('agent.settings.channel.registration.openApp', 'Open registration page')}
          </a>
        </div>
      )}

      {phase === 'scanning' && !registration?.verification_url && (
        <div className="feishu-registration-state">
          <span className="feishu-registration-spinner" aria-hidden="true" />
          {t('agent.settings.channel.registration.preparing', 'Preparing the official QR code…')}
        </div>
      )}

      {phase === 'connecting' && (
        <div className="feishu-registration-state feishu-registration-state-connecting">
          <span className="feishu-registration-spinner" aria-hidden="true" />
          <div>
            <strong>{t('agent.settings.channel.registration.appCreated', 'App created')}</strong>
            <div>
              {t(
                'agent.settings.channel.registration.connecting',
                'Credentials are encrypted. Waiting for the real WebSocket connection…',
              )}
            </div>
          </div>
        </div>
      )}

      {phase === 'done' && (
        <div className="feishu-registration-success">
          {t('agent.settings.channel.registration.connected', 'Connected through WebSocket')}
        </div>
      )}

      {(phase === 'error' || requestError) && (
        <div className="feishu-registration-error" role="alert">
          {failureText}
        </div>
      )}

      <div className="feishu-registration-actions">
        {registration && isFeishuRegistrationCancellable(registration.status) && (
          <button
            type="button"
            className="btn btn-secondary"
            onClick={() => cancelMutation.mutate(registration.session_id)}
            disabled={cancelMutation.isPending}
          >
            {t('common.cancel', 'Cancel')}
          </button>
        )}
        {phase === 'error' && (
          <button
            type="button"
            className="btn btn-primary"
            onClick={() => startMutation.mutate()}
            disabled={startMutation.isPending}
          >
            {t('agent.settings.channel.registration.retry', 'Generate a new QR code')}
          </button>
        )}
        {onClose && phase !== 'connecting' && (
          <button
            type="button"
            className="btn btn-secondary"
            onClick={() => void cancelThen(onClose)}
            disabled={cancelMutation.isPending}
          >
            {t('agent.settings.channel.registration.back', 'Back to current connection')}
          </button>
        )}
      </div>

      {onManualConfigure && phase !== 'connecting' && (
        <button
          type="button"
          className="feishu-registration-manual"
          onClick={() => void cancelThen(onManualConfigure)}
          disabled={cancelMutation.isPending}
        >
          {t('agent.settings.channel.registration.manual', 'Manual configuration (advanced)')}
        </button>
      )}
    </section>
  );
}
