import React from 'react';
import { useTranslation } from 'react-i18next';
import {
  IconCalendarTime,
  IconChecklist,
  IconFileText,
  IconLoader2,
  IconPaperclip,
  IconTargetArrow,
  IconX,
} from '@tabler/icons-react';

import {
  SessionRunControls,
  type SessionPermissionMode,
  type SessionPermissionOption,
} from './SessionRunControls';
import './SessionComposer.css';

export type ComposerActionKey = 'upload' | 'plan' | 'goal' | 'schedule';

export interface ComposerAttachment {
  name: string;
  imageUrl?: string;
}

export function shouldSubmitComposerKey(event: { key: string; shiftKey: boolean; isComposing: boolean }): boolean {
  return event.key === 'Enter' && !event.shiftKey && !event.isComposing;
}

export interface SessionComposerProps {
  value: string;
  inputRef: React.RefObject<HTMLTextAreaElement | null>;
  fileInputRef: React.RefObject<HTMLInputElement | null>;
  placeholder: string;
  disabled: boolean;
  attachments: ComposerAttachment[];
  permissionMode: SessionPermissionMode;
  permissionModeLabel: string;
  permissionOptions: SessionPermissionOption[];
  modelLabel: string;
  modelTitle?: string;
  runtimeUsageLabel?: string;
  intentLabel?: string | null;
  planModeRequested: boolean;
  uploading: boolean;
  uploadProgress: number;
  running: boolean;
  onChange: (value: string) => void;
  onPaste: React.ClipboardEventHandler<HTMLTextAreaElement>;
  onSubmit: () => void;
  onStop: () => void;
  onAction: (action: ComposerActionKey) => void;
  onPermissionModeChange: (mode: SessionPermissionMode) => void;
  onFilesSelected: React.ChangeEventHandler<HTMLInputElement>;
  onRemoveAttachment: (index: number) => void;
  onCancelUpload: () => void;
}

export function SessionComposer({
  value,
  inputRef,
  fileInputRef,
  placeholder,
  disabled,
  attachments,
  permissionMode,
  permissionModeLabel,
  permissionOptions,
  modelLabel,
  modelTitle,
  runtimeUsageLabel,
  intentLabel,
  planModeRequested,
  uploading,
  uploadProgress,
  running,
  onChange,
  onPaste,
  onSubmit,
  onStop,
  onAction,
  onPermissionModeChange,
  onFilesSelected,
  onRemoveAttachment,
  onCancelUpload,
}: SessionComposerProps) {
  const { t } = useTranslation();
  const [composerMenuOpen, setComposerMenuOpen] = React.useState(false);
  const [permissionMenuOpen, setPermissionMenuOpen] = React.useState(false);
  const canSend = !disabled && (Boolean(value.trim()) || attachments.length > 0);
  const actions = [
    {
      key: 'upload' as const,
      label: t('agent.chat.composer.uploadFile', 'Upload file'),
      description: t('agent.chat.composer.uploadFileDesc', 'Attach files or screenshots to this turn'),
      icon: uploading ? <IconLoader2 size={16} /> : <IconPaperclip size={16} />,
      disabled: disabled || uploading || attachments.length >= 10,
    },
    {
      key: 'plan' as const,
      label: t('agent.chat.composer.planMode', 'Plan Mode'),
      description: planModeRequested
        ? t('agent.chat.composer.planModeOnDesc', 'Next message will request a plan first')
        : t('agent.chat.composer.planModeDesc', 'Ask the agent to plan before execution'),
      icon: <IconChecklist size={16} />,
      selected: planModeRequested,
      disabled: disabled || running,
    },
    {
      key: 'goal' as const,
      label: t('agent.chat.composer.goalMode', 'Goal mode'),
      description: t('agent.chat.composer.goalModeDesc', 'Start a session goal through the command surface'),
      icon: <IconTargetArrow size={16} />,
      disabled: disabled || running,
    },
    {
      key: 'schedule' as const,
      label: t('agent.chat.composer.scheduledTask', 'Scheduled task'),
      description: t('agent.chat.composer.scheduledTaskDesc', 'Draft a scheduled task request for this agent'),
      icon: <IconCalendarTime size={16} />,
      disabled: disabled || running,
    },
  ];

  return (
    <div
      className="session-composer-module"
      onKeyDown={(event) => {
        if (event.key !== 'Escape') return;
        setComposerMenuOpen(false);
        setPermissionMenuOpen(false);
      }}
    >
      {attachments.length > 0 && (
        <div data-testid="session-composer-attachments" className="session-composer-attachments">
          {attachments.map((file, index) => (
            <div key={`${file.name}:${index}`} className="session-composer-attachment">
              {file.imageUrl ? <img src={file.imageUrl} alt="" /> : <IconFileText size={14} aria-hidden="true" />}
              <span title={file.name}>{file.name}</span>
              <button type="button" onClick={() => onRemoveAttachment(index)} aria-label={`${t('agent.chat.removeAttachment', 'Remove attachment')}: ${file.name}`}>
                <IconX size={13} aria-hidden="true" />
              </button>
            </div>
          ))}
        </div>
      )}
      <div data-testid="session-composer-shell" className="session-composer-shell">
        <div
          id="session-composer-actions"
          data-testid="session-composer-plus-menu"
          className="session-composer-menu session-composer-actions"
          role="menu"
          hidden={!composerMenuOpen}
        >
          {actions.map((action) => (
            <button
              key={action.key}
              type="button"
              role="menuitem"
              aria-pressed={action.selected}
              disabled={action.disabled}
              onClick={() => {
                setComposerMenuOpen(false);
                onAction(action.key);
              }}
            >
              <span>{action.icon}</span>
              <span>
                <strong>{action.label}</strong>
                <small>{action.description}</small>
              </span>
              {action.selected !== undefined && (
                <span
                  data-testid={`session-composer-action-${action.key}-switch`}
                  className="session-composer-action-switch"
                  role="switch"
                  aria-checked={action.selected}
                  aria-label={action.label}
                >
                  <span />
                </span>
              )}
            </button>
          ))}
        </div>
        <input type="file" multiple ref={fileInputRef} onChange={onFilesSelected} hidden />
        <textarea
          data-testid="session-composer-input"
          ref={inputRef}
          className="chat-input"
          value={value}
          onChange={(event) => onChange(event.target.value)}
          onKeyDown={(event) => {
            if (!shouldSubmitComposerKey({ key: event.key, shiftKey: event.shiftKey, isComposing: event.nativeEvent.isComposing })) return;
            event.preventDefault();
            if (canSend) onSubmit();
          }}
          onPaste={onPaste}
          placeholder={placeholder}
          disabled={disabled}
          rows={1}
          autoFocus
        />
        <SessionRunControls
          disabled={disabled}
          canSend={canSend}
          running={running}
          composerMenuOpen={composerMenuOpen}
          permissionMenuOpen={permissionMenuOpen}
          permissionMode={permissionMode}
          permissionModeLabel={permissionModeLabel}
          permissionOptions={permissionOptions}
          intentLabel={intentLabel}
          modelLabel={modelLabel}
          modelTitle={modelTitle}
          runtimeUsageLabel={runtimeUsageLabel}
          uploading={uploading}
          uploadProgress={uploadProgress}
          onToggleComposerMenu={() => {
            setComposerMenuOpen((open) => !open);
            setPermissionMenuOpen(false);
          }}
          onTogglePermissionMenu={() => {
            setPermissionMenuOpen((open) => !open);
            setComposerMenuOpen(false);
          }}
          onPermissionModeChange={(mode) => {
            onPermissionModeChange(mode);
            setPermissionMenuOpen(false);
          }}
          onCancelUpload={onCancelUpload}
          onStop={onStop}
          onSubmit={onSubmit}
        />
      </div>
    </div>
  );
}
