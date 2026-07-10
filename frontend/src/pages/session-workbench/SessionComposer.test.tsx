import React from 'react';
import { renderToStaticMarkup } from 'react-dom/server';
import { describe, expect, it, vi } from 'vitest';

vi.mock('react-i18next', () => ({
  useTranslation: () => ({
    t: (_key: string, fallback?: string) => fallback || _key,
  }),
}));

import { SessionComposer, shouldSubmitComposerKey } from './SessionComposer';

const permissionOptions = [
  { value: 'default' as const, label: 'Ask first', description: 'Ask before sensitive actions' },
  { value: 'auto' as const, label: 'Approve for me', description: 'Approve low-risk actions' },
];

function renderComposer(overrides: Partial<React.ComponentProps<typeof SessionComposer>> = {}) {
  const props: React.ComponentProps<typeof SessionComposer> = {
    value: '',
    inputRef: React.createRef<HTMLTextAreaElement>(),
    fileInputRef: React.createRef<HTMLInputElement>(),
    placeholder: 'Ask the agent',
    disabled: false,
    attachments: [],
    permissionMode: 'default',
    permissionModeLabel: 'Ask first',
    permissionOptions,
    modelLabel: 'GPT Test',
    modelTitle: 'provider · model',
    runtimeUsageLabel: '12% used',
    planModeRequested: false,
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
  return renderToStaticMarkup(<SessionComposer {...props} />);
}

describe('SessionComposer', () => {
  it('submits Enter only when Shift and IME composition are absent', () => {
    expect(shouldSubmitComposerKey({ key: 'Enter', shiftKey: false, isComposing: false })).toBe(true);
    expect(shouldSubmitComposerKey({ key: 'Enter', shiftKey: true, isComposing: false })).toBe(false);
    expect(shouldSubmitComposerKey({ key: 'Enter', shiftKey: false, isComposing: true })).toBe(false);
    expect(shouldSubmitComposerKey({ key: 'Escape', shiftKey: false, isComposing: false })).toBe(false);
  });

  it('exposes attachment removal, permission, model, and disabled send semantics', () => {
    const markup = renderComposer({
      attachments: [{ name: 'report.md' }],
      disabled: true,
    });

    expect(markup).toContain('data-testid="session-composer-shell"');
    expect(markup).toContain('report.md');
    expect(markup).toContain('Remove attachment');
    expect(markup).toContain('Ask first');
    expect(markup).toContain('GPT Test');
    expect(markup).toContain('data-testid="session-composer-send"');
    expect(markup).toContain('disabled=""');
  });

  it('announces upload progress and distinguishes Stop from Send', () => {
    const markup = renderComposer({ uploading: true, uploadProgress: 42, running: true, value: 'hello' });

    expect(markup).toContain('role="status"');
    expect(markup).toContain('aria-live="polite"');
    expect(markup).toContain('42%');
    expect(markup).toContain('data-testid="session-composer-stop"');
    expect(markup).toContain('data-testid="session-composer-send"');
  });
});
