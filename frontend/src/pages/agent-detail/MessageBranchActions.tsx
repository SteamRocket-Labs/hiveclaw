import React from 'react';
import { useTranslation } from 'react-i18next';
import { IconThumbUp, IconThumbDown, IconGitBranch, IconHistory } from '@tabler/icons-react';
import type { RecordSessionFeedbackInput } from '../../api/domains/chat';
import type { AgentChatMessage } from './chatRuntime';

export default function MessageBranchActions({
  message,
  checkpointMessage,
  isLeft,
  onBranchMessage,
  onFeedbackMessage,
  onRewindMessage,
}: {
  message: AgentChatMessage;
  checkpointMessage?: AgentChatMessage | null;
  isLeft: boolean;
  onBranchMessage?: (message: AgentChatMessage) => void | Promise<unknown>;
  onFeedbackMessage?: (message: AgentChatMessage, label: RecordSessionFeedbackInput['label']) => void | Promise<unknown>;
  onRewindMessage?: (message: AgentChatMessage) => void | Promise<unknown>;
}) {
  const { t } = useTranslation();
  if (!message.id) return null;
  if (message.role !== 'assistant') return null;
  if (!onBranchMessage && !onFeedbackMessage && !onRewindMessage) return null;
  const checkpointReady = Boolean(checkpointMessage?.transcriptEventId);

  const actions: Array<{
    key: string;
    label: string;
    testId: string;
    icon: React.ReactNode;
    onClick: () => void;
    disabled?: boolean;
  }> = [
    {
      key: 'like',
      label: t('agent.chat.actions.like', 'Like'),
      testId: 'message-action-like',
      icon: <IconThumbUp size={12} />,
      disabled: !onFeedbackMessage,
      onClick: () => void onFeedbackMessage?.(message, 'useful'),
    },
    {
      key: 'dislike',
      label: t('agent.chat.actions.dislike', 'Dislike'),
      testId: 'message-action-dislike',
      icon: <IconThumbDown size={12} />,
      disabled: !onFeedbackMessage,
      onClick: () => void onFeedbackMessage?.(message, 'misleading'),
    },
    {
      key: 'branch',
      label: t('agent.chat.actions.branch', 'Branch'),
      testId: 'message-action-branch',
      icon: <IconGitBranch size={12} />,
      disabled: !onBranchMessage || !checkpointReady,
      onClick: () => {
        if (!checkpointMessage) return;
        void onBranchMessage?.(checkpointMessage);
      },
    },
    {
      key: 'rewind',
      label: t('agent.chat.actions.rewind', 'Rewind'),
      testId: 'message-action-rewind',
      icon: <IconHistory size={12} />,
      disabled: !onRewindMessage || !checkpointReady,
      onClick: () => {
        if (!checkpointMessage) return;
        void onRewindMessage?.(checkpointMessage);
      },
    },
  ];

  return (
    <div
      style={{
        display: 'inline-flex',
        gap: '4px',
        marginLeft: isLeft ? '6px' : 0,
        marginRight: isLeft ? 0 : '6px',
        flexWrap: 'wrap',
        justifyContent: isLeft ? 'flex-start' : 'flex-end',
      }}
    >
      {actions.map((action) => (
        <button
          key={action.key}
          type="button"
          data-testid={action.testId}
          aria-label={action.label}
          title={action.label}
          disabled={action.disabled}
          onClick={action.onClick}
          style={{
            border: '1px solid var(--border-subtle)',
            background: 'transparent',
            color: action.disabled ? 'var(--text-disabled)' : 'var(--text-tertiary)',
            borderRadius: '4px',
            fontSize: '10px',
            lineHeight: 1,
            padding: '3px',
            width: '20px',
            height: '20px',
            display: 'inline-flex',
            alignItems: 'center',
            justifyContent: 'center',
            cursor: action.disabled ? 'not-allowed' : 'pointer',
          }}
        >
          {action.icon}
        </button>
      ))}
    </div>
  );
}
