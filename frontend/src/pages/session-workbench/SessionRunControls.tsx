import React from 'react';
import { useTranslation } from 'react-i18next';
import {
  IconCircleDashedCheck,
  IconPlus,
  IconSend2,
  IconShieldCheck,
  IconSquare,
  IconX,
} from '@tabler/icons-react';

export type SessionPermissionMode = 'auto' | 'default' | 'bypassPermissions';

export interface SessionPermissionOption {
  value: SessionPermissionMode;
  label: string;
  description: string;
}

export interface SessionRunControlsProps {
  disabled: boolean;
  canSend: boolean;
  running: boolean;
  composerMenuOpen: boolean;
  permissionMenuOpen: boolean;
  permissionMode: SessionPermissionMode;
  permissionModeLabel: string;
  permissionOptions: SessionPermissionOption[];
  intentLabel?: string | null;
  modelLabel: string;
  modelTitle?: string;
  runtimeUsageLabel?: string;
  uploading: boolean;
  uploadProgress: number;
  onToggleComposerMenu: () => void;
  onTogglePermissionMenu: () => void;
  onPermissionModeChange: (mode: SessionPermissionMode) => void;
  onCancelUpload: () => void;
  onStop: () => void;
  onSubmit: () => void;
}

export function SessionRunControls({
  disabled,
  canSend,
  running,
  composerMenuOpen,
  permissionMenuOpen,
  permissionMode,
  permissionModeLabel,
  permissionOptions,
  intentLabel,
  modelLabel,
  modelTitle,
  runtimeUsageLabel,
  uploading,
  uploadProgress,
  onToggleComposerMenu,
  onTogglePermissionMenu,
  onPermissionModeChange,
  onCancelUpload,
  onStop,
  onSubmit,
}: SessionRunControlsProps) {
  const { t } = useTranslation();
  return (
    <div className="session-composer-controls">
      <button
        type="button"
        className="session-composer-icon-button"
        onClick={onToggleComposerMenu}
        aria-label={t('agent.chat.composer.openMenu', 'Open composer actions')}
        aria-expanded={composerMenuOpen}
        aria-controls="session-composer-actions"
        disabled={disabled}
      >
        <IconPlus size={20} stroke={1.7} aria-hidden="true" />
      </button>
      <button
        type="button"
        data-testid="session-composer-permission-badge"
        className="session-composer-permission-button"
        aria-label={t('agent.chat.composer.permissionTitle', 'Session permission mode')}
        aria-expanded={permissionMenuOpen}
        aria-controls="session-composer-permission-menu"
        onClick={onTogglePermissionMenu}
      >
        <IconShieldCheck size={15} stroke={1.8} aria-hidden="true" />
        {permissionModeLabel}
      </button>
      <div
        id="session-composer-permission-menu"
        data-testid="session-composer-permission-menu"
        className="session-composer-menu session-composer-permission-menu"
        role="radiogroup"
        aria-label={t('agent.chat.composer.permissionTitle', 'Session permission mode')}
        hidden={!permissionMenuOpen}
      >
        {permissionOptions.map((option) => {
          const selected = option.value === permissionMode;
          return (
            <button
              key={option.value}
              type="button"
              role="radio"
              aria-checked={selected}
              data-testid={`session-composer-permission-mode-${option.value}`}
              className={selected ? 'is-selected' : undefined}
              onClick={() => onPermissionModeChange(option.value)}
            >
              <IconShieldCheck size={15} stroke={selected ? 2.2 : 1.7} aria-hidden="true" />
              <span>
                <strong>{t(`agent.chat.composer.permissionMode.${option.value}`, option.label)}</strong>
                <small>{t(`agent.chat.composer.permissionModeDesc.${option.value}`, option.description)}</small>
              </span>
            </button>
          );
        })}
      </div>
      {intentLabel && <span data-testid="session-composer-intent-badge" className="session-composer-intent">{intentLabel}</span>}
      {uploading && uploadProgress >= 0 && (
        <div className="session-composer-upload" role="status" aria-live="polite">
          {uploadProgress <= 100 ? (
            <>
              <span className="session-composer-progress" aria-hidden="true">
                <span style={{ transform: `scaleX(${Math.max(0, Math.min(100, uploadProgress)) / 100})` }} />
              </span>
              <span>{uploadProgress}%</span>
            </>
          ) : (
            <span>{t('agent.chat.composer.processing', 'Processing...')}</span>
          )}
          <button type="button" onClick={onCancelUpload} aria-label={t('common.cancel', 'Cancel')}>
            <IconX size={13} aria-hidden="true" />
          </button>
        </div>
      )}
      <span className="session-composer-spacer" />
      <span data-testid="session-composer-model-badge" className="session-composer-model" title={modelTitle || t('agent.chat.composer.modelTitle', 'Model information')}>
        <IconCircleDashedCheck size={17} stroke={1.9} aria-hidden="true" />
        <span>{modelLabel}</span>
        {runtimeUsageLabel && <small>{runtimeUsageLabel}</small>}
      </span>
      {running && (
        <button
          type="button"
          data-testid="session-composer-stop"
          className="btn btn-stop-generation session-composer-stop"
          onClick={onStop}
          aria-label={t('chat.stop', 'Stop')}
          title={t('chat.stop', 'Stop')}
        >
          <IconSquare size={13} fill="currentColor" aria-hidden="true" />
          <span>{t('chat.stop', 'Stop')}</span>
        </button>
      )}
      <button
        type="button"
        data-testid="session-composer-send"
        className="btn btn-primary session-composer-send"
        onClick={onSubmit}
        disabled={!canSend}
        aria-label={t('chat.send')}
        title={t('chat.send')}
      >
        <IconSend2 size={18} aria-hidden="true" />
      </button>
    </div>
  );
}
