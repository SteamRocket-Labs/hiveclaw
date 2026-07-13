import { useEffect, useRef } from 'react';
import { useTranslation } from 'react-i18next';

import PromptModal from '../../components/PromptModal';
import { chatApi } from '../../api/domains/chat';
import { isDraftHumanChatSession } from './chatRuntime';
import {
  DEFAULT_SESSION_PERMISSION_MODE,
  sessionBreakGlassExpiryDelay,
} from './agentDetailPolicy';
import type { SessionPermissionMode } from './AgentChatSection';

type SessionPermissionExpiryInput = {
  agentId?: string;
  activeSession: any;
  onExpire: (sessionId: string, mode: SessionPermissionMode) => void;
  onSyncError: (message: string) => void;
  onInvalidate: (agentId: string, sessionId: string) => void;
};

export function useSessionPermissionExpiry(input: SessionPermissionExpiryInput) {
  const { t } = useTranslation();
  const callbacksRef = useRef({
    onExpire: input.onExpire,
    onSyncError: input.onSyncError,
    onInvalidate: input.onInvalidate,
  });
  callbacksRef.current = {
    onExpire: input.onExpire,
    onSyncError: input.onSyncError,
    onInvalidate: input.onInvalidate,
  };

  const sessionId = input.activeSession?.id ? String(input.activeSession.id) : null;
  const isDraft = isDraftHumanChatSession(input.activeSession);
  const permissionMode = input.activeSession?.permission_mode;
  const breakGlassExpiresAt = input.activeSession?.break_glass?.expires_at;
  const transcriptBreakGlassExpiresAt = input.activeSession?.transcript_metadata_json?.break_glass?.expires_at;

  useEffect(() => {
    const expiryDelay = sessionBreakGlassExpiryDelay(input.activeSession);
    if (expiryDelay === null || !sessionId) return;
    const timer = window.setTimeout(() => {
      callbacksRef.current.onExpire(sessionId, DEFAULT_SESSION_PERMISSION_MODE);
      if (input.agentId && !isDraft) {
        void chatApi.updateSessionPermissionProfile(input.agentId, sessionId, {
          permission_mode: DEFAULT_SESSION_PERMISSION_MODE,
        }).catch((error: any) => {
          callbacksRef.current.onSyncError(error?.message || t(
            'agent.chat.permission.expirySyncFailed',
            'Full access expired, but the session status could not be refreshed. Reload before continuing.',
          ));
          callbacksRef.current.onInvalidate(input.agentId!, sessionId);
        });
      }
    }, expiryDelay + 25);
    return () => window.clearTimeout(timer);
  }, [
    input.agentId,
    sessionId,
    isDraft,
    permissionMode,
    breakGlassExpiresAt,
    transcriptBreakGlassExpiresAt,
    t,
  ]);
}

type SessionFullAccessPromptProps = {
  open: boolean;
  onCancel: () => void;
  onConfirm: (reason: string) => void;
};

export function SessionFullAccessPrompt({ open, onCancel, onConfirm }: SessionFullAccessPromptProps) {
  const { t } = useTranslation();
  return (
    <PromptModal
      open={open}
      title={t('agent.chat.permission.fullAccessTitle', 'Enable full access for 60 minutes')}
      placeholder={t(
        'agent.chat.permission.fullAccessReasonPlaceholder',
        'Reason for this session (at least 8 characters)',
      )}
      onCancel={onCancel}
      onConfirm={onConfirm}
    />
  );
}
