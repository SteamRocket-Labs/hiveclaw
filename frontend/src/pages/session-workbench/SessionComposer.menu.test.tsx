// @vitest-environment jsdom
import React from 'react';
import { cleanup, fireEvent, render, screen } from '@testing-library/react';
import { afterEach, expect, it, vi } from 'vitest';

vi.mock('react-i18next', () => ({
  useTranslation: () => ({
    t: (_key: string, fallback?: string) => fallback || _key,
  }),
}));

import { SessionComposer } from './SessionComposer';

const permissionOptions = [
  { value: 'default' as const, label: 'Ask first', description: 'Ask before sensitive actions' },
  { value: 'auto' as const, label: 'Approve for me', description: 'Approve low-risk actions' },
];

function renderMenuComposer(overrides: Partial<React.ComponentProps<typeof SessionComposer>> = {}) {
  const props: React.ComponentProps<typeof SessionComposer> = {
    value: '',
    inputRef: React.createRef<HTMLTextAreaElement>(),
    fileInputRef: React.createRef<HTMLInputElement>(),
    placeholder: 'Ask the agent',
    disabled: false,
    attachments: [],
    permissionMode: 'default',
    permissionModeLabel: 'Ask first',
    // Default mode: the badge itself stays hidden (product definition P3).
    permissionModeVisible: false,
    permissionOptions,
    projectContextAvailable: true,
    modelLabel: 'GPT Test',
    planModeRequested: false,
    goalModeRequested: false,
    uploading: false,
    uploadProgress: -1,
    running: false,
    onChange: vi.fn(),
    onPaste: vi.fn(),
    onSubmit: vi.fn(),
    onStop: vi.fn(),
    onAction: vi.fn(),
    onPermissionModeChange: vi.fn(),
    onFilesSelected: vi.fn(),
    onRemoveAttachment: vi.fn(),
    onCancelUpload: vi.fn(),
    ...overrides,
  };
  return render(<SessionComposer {...props} />);
}

afterEach(() => cleanup());

it('keeps the permission badge hidden in default mode while the "+" menu opens the same selection', () => {
  const onPermissionModeChange = vi.fn();
  renderMenuComposer({ onPermissionModeChange });
  expect(screen.queryByTestId('session-composer-permission-badge')).toBeNull();
  expect(screen.queryByTestId('session-composer-permission-menu')).toBeNull();
  fireEvent.click(screen.getByRole('button', { name: 'Open composer actions' }));
  fireEvent.click(screen.getByRole('menuitem', { name: /Session permissions/ }));
  const menu = screen.getByTestId('session-composer-permission-menu');
  expect(menu.hidden).toBe(false);
  expect(screen.queryByTestId('session-composer-permission-badge')).toBeNull();
  expect(screen.getByTestId('session-composer-permission-mode-default').getAttribute('aria-checked')).toBe('true');
  fireEvent.click(screen.getByTestId('session-composer-permission-mode-auto'));
  expect(onPermissionModeChange).toHaveBeenCalledWith('auto');
});

it('offers remote tasks only where the conversation supports them and opens the panel on demand', () => {
  const onAction = vi.fn();
  const unavailable = renderMenuComposer({ onAction });
  fireEvent.click(screen.getByRole('button', { name: 'Open composer actions' }));
  expect(screen.queryByRole('menuitem', { name: /Remote tasks/ })).toBeNull();
  unavailable.unmount();

  const available = renderMenuComposer({ remoteWorkAvailable: true, onAction });
  fireEvent.click(screen.getByRole('button', { name: 'Open composer actions' }));
  fireEvent.click(screen.getByRole('menuitem', { name: /Remote tasks/ }));
  expect(onAction).toHaveBeenCalledWith('remote');
  expect(screen.getByRole('button', { name: 'Open composer actions' }).getAttribute('aria-expanded')).toBe('false');
});
