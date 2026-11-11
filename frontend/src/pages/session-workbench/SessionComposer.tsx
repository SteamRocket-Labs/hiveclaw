import React from 'react';
import { useTranslation } from 'react-i18next';
import {
  IconCalendarTime,
  IconChecklist,
  IconFileText,
  IconFolder,
  IconLoader2,
  IconPaperclip,
  IconShieldCheck,
  IconTargetArrow,
  IconWorld,
  IconX,
} from '@tabler/icons-react';

import {
  SessionRunControls,
  type SessionPermissionMode,
  type SessionPermissionOption,
} from './SessionRunControls';
import './SessionComposer.css';

export type ComposerActionKey = 'upload' | 'plan' | 'goal' | 'schedule' | 'project' | 'permission' | 'remote';

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
  sendDisabled?: boolean;
  attachments: ComposerAttachment[];
  permissionMode: SessionPermissionMode;
  permissionModeLabel: string;
  permissionModeVisible?: boolean;
  permissionOptions: SessionPermissionOption[];
  projectContextAvailable?: boolean;
  projectContextSelected?: boolean;
  remoteWorkAvailable?: boolean;
  remoteWorkOpen?: boolean;
  modelLabel: string;
  modelTitle?: string;
  runtimeUsageLabel?: string;
  intentLabel?: string | null;
  planModeRequested: boolean;
  goalModeRequested: boolean;
  goalModeAvailable?: boolean;
  uploading: boolean;
  uploadProgress: number;
  running: boolean;
  onChange: (value: string) => void;
  onPaste: React.ClipboardEventHandler<HTMLTextAreaElement>;
  onSubmit: () => void;
  canStop?: boolean;
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
  sendDisabled = false,
  attachments,
  permissionMode,
  permissionModeLabel,
  permissionModeVisible = true,
  permissionOptions,
  projectContextAvailable = false,
  projectContextSelected = false,
  remoteWorkAvailable = false,
  remoteWorkOpen = false,
  modelLabel,
  modelTitle,
  runtimeUsageLabel,
  intentLabel,
  planModeRequested,
  goalModeRequested,
  goalModeAvailable = true,
  uploading,
  uploadProgress,
  running,
  onChange,
  onPaste,
  onSubmit,
  canStop = true,
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
  const canSend = !disabled && !sendDisabled && !uploading && (Boolean(value.trim()) || attachments.length > 0);
  const actions = [
    {
      key: 'upload' as const,
      label: t('agent.chat.composer.uploadFile', 'Upload file'),
      description: t('agent.chat.composer.uploadFileDesc', 'Attach files or screenshots to this turn'),
      icon: uploading ? <IconLoader2 size={16} /> : <IconPaperclip size={16} />,
      disabled: disabled || uploading || attachments.length >= 10,
    },
    {
      key: 'project' as const,
      label: t('agent.chat.composer.projectContext', 'Project files'),
      description: t('agent.chat.composer.projectContextDesc', 'Reference files from a project in the next message'),
      icon: <IconFolder size={16} />,
      selected: projectContextSelected,
      disabled: disabled,
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
      // Session permissions stay reachable from "+" while the badge is hidden
      // on the agent's default mode (product definition P3).
      key: 'permission' as const,
      label: t('agent.chat.composer.permissionEntry', 'Session permissions'),
      description: t('agent.chat.composer.permissionEntryDesc', 'View or change what this session may do without asking'),
      icon: <IconShieldCheck size={16} />,
      disabled: disabled,
    },
    {
      key: 'remote' as const,
      label: t('agent.chat.composer.remoteWork', 'Remote tasks'),
      description: t('agent.chat.composer.remoteWorkDesc', 'Reply to, cancel, or recover files from this conversation’s remote tasks'),
      icon: <IconWorld size={16} />,
      selected: remoteWorkOpen,
      // Task recovery stays operable while message sending is paused (retirement-pending).
      disabled: false,
    },
    {
      key: 'goal' as const,
      label: t('agent.chat.composer.goalMode', 'Goal mode'),
      description: goalModeRequested
        ? t('agent.chat.composer.goalModeOnDesc', 'Create a durable goal and start its first turn')
        : t('agent.chat.composer.goalModeDesc', 'Run the next request as a durable, resumable goal'),
      icon: <IconTargetArrow size={16} />,
      selected: goalModeRequested,
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
  const visibleActions = actions.filter((action) => (goalModeAvailable || action.key !== 'goal')
    && (projectContextAvailable || action.key !== 'project') && (remoteWorkAvailable || action.key !== 'remote'));

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
          {visibleActions.map((action) => (
            <button
              key={action.key}
              type="button"
              role="menuitem"
              aria-pressed={action.selected}
              disabled={action.disabled}
              onClick={() => {
                setComposerMenuOpen(false);
                if (action.key === 'permission') {
                  setPermissionMenuOpen(true);
                  // The chosen menu item disappears; focus moves into the picker.
                  setTimeout(() => document.getElementById('session-composer-permission-menu')
                    ?.querySelector<HTMLButtonElement>('[role="radio"]')?.focus(), 0);
                  return;
                }
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
          plusButtonDisabled={disabled && !remoteWorkAvailable}
          permissionMenuOpen={permissionMenuOpen}
          permissionMode={permissionMode}
          permissionModeLabel={permissionModeLabel}
          permissionModeVisible={permissionModeVisible}
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
          canStop={canStop}
          onStop={onStop}
          onSubmit={onSubmit}
        />
      </div>
    </div>
  );
}
