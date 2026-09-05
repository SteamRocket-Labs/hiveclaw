// @vitest-environment jsdom

import React from 'react';
import { cleanup, fireEvent, render, screen } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

const harness = vi.hoisted(() => ({
  navigate: vi.fn(),
  getHrAgent: vi.fn(),
  location: {
    pathname: '/home',
    search: '',
    hash: '',
  },
}));

vi.mock('react-i18next', () => ({
  useTranslation: () => ({
    t: (_key: string, fallback?: string) => fallback ?? _key,
    i18n: { language: 'en' },
  }),
}));

vi.mock('react-router-dom', () => ({
  NavLink: ({ to, children, className, end: _end, ...props }: any) => (
    <a href={String(to)} className={typeof className === 'function' ? className({ isActive: false }) : className} {...props}>
      {children}
    </a>
  ),
  useLocation: () => harness.location,
  useNavigate: () => harness.navigate,
}));

vi.mock('../../api/domains/agents', () => ({
  agentApi: { getHrAgent: harness.getHrAgent },
}));

vi.mock('../../api/domains/chat', () => ({
  chatApi: {
    createSession: vi.fn(),
    listSessions: vi.fn().mockResolvedValue([]),
    deleteSession: vi.fn().mockResolvedValue(undefined),
  },
}));

vi.mock('../../api/domains/localBridge', () => ({
  localBridgeApi: {
    createAgentChannelSession: vi.fn(),
    listAgentChannelSessions: vi.fn().mockResolvedValue([]),
    deleteAgentChannelSession: vi.fn().mockResolvedValue(undefined),
  },
}));

import AppSidebar from './AppSidebar';

function SettingsHarness({ onCloseAccountMenu }: { onCloseAccountMenu: () => void }) {
  const [open, setOpen] = React.useState(false);
  const accountMenuRef = React.useRef<HTMLDivElement | null>(null);
  return (
    <AppSidebar
      user={{ id: 'user-1', role: 'org_admin', display_name: 'Example Owner' }}
      theme="light"
      isSidebarCollapsed
      onToggleSidebar={vi.fn()}
      tenants={[{ id: 'tenant-1', name: 'Company A' }]}
      currentTenant="tenant-1"
      onSwitchTenant={vi.fn()}
      agents={[]}
      hrAgent={null}
      isChinese={false}
      onToggleTheme={vi.fn()}
      onOpenNotifications={vi.fn()}
      unreadCount={0}
      accountMenuRef={accountMenuRef}
      showAccountMenu={open}
      onToggleAccountMenu={() => setOpen((value) => !value)}
      onCloseAccountMenu={() => {
        onCloseAccountMenu();
        setOpen(false);
      }}
      onToggleLang={vi.fn()}
      onOpenAccountSettings={vi.fn()}
      onLogout={vi.fn()}
      versionDisplay={null}
    />
  );
}

describe('AppSidebar settings menu wiring', () => {
  beforeEach(() => {
    harness.location.pathname = '/home';
    harness.location.search = '';
    harness.location.hash = '';
  });

  afterEach(() => cleanup());

  it('moves focus into the open menu, closes on Escape, and returns focus to the trigger', () => {
    const onCloseAccountMenu = vi.fn();
    render(<SettingsHarness onCloseAccountMenu={onCloseAccountMenu} />);

    const trigger = screen.getByRole('button', { name: 'Settings' });
    trigger.focus();
    fireEvent.click(trigger);

    const firstItem = screen.getByRole('button', { name: 'Account Settings' });
    expect(trigger.getAttribute('aria-expanded')).toBe('true');
    expect(document.activeElement).toBe(firstItem);

    fireEvent.keyDown(document, { key: 'Escape' });

    expect(onCloseAccountMenu).toHaveBeenCalledTimes(1);
    expect(screen.queryByRole('button', { name: 'Account Settings' })).toBeNull();
    expect(trigger.getAttribute('aria-expanded')).toBe('false');
    expect(document.activeElement).toBe(trigger);
  });

  it('closes the open menu on route change', () => {
    const onCloseAccountMenu = vi.fn();
    const { rerender } = render(<SettingsHarness onCloseAccountMenu={onCloseAccountMenu} />);

    fireEvent.click(screen.getByRole('button', { name: 'Settings' }));
    expect(screen.getByRole('button', { name: 'Account Settings' })).toBeTruthy();

    harness.location = { pathname: '/knowledge', search: '', hash: '' };
    rerender(<SettingsHarness onCloseAccountMenu={onCloseAccountMenu} />);

    expect(onCloseAccountMenu).toHaveBeenCalledTimes(1);
    expect(screen.queryByRole('button', { name: 'Account Settings' })).toBeNull();
  });
});
