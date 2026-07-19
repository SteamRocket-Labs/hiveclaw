import React from 'react';
import { renderToStaticMarkup } from 'react-dom/server';
import { describe, expect, it, vi } from 'vitest';

import ExternalPrincipalBindingsPanel from './ExternalPrincipalBindingsPanel';

vi.mock('react-i18next', () => ({
  useTranslation: () => ({
    t: (_key: string, fallback?: string) => fallback ?? _key,
  }),
}));

describe('ExternalPrincipalBindingsPanel', () => {
  it('keeps external people separate and lets admins revoke but never assign identity', () => {
    const markup = renderToStaticMarkup(
      <ExternalPrincipalBindingsPanel
        principals={[
          {
            id: 'principal-unbound',
            provider: 'slack',
            installation_ref: 'technical-installation-uuid',
            channel_config_id: 'technical-config-uuid',
            subject_id: 'U123',
            display_name: 'Slack Guest',
            linked_user_id: null,
            binding_method: null,
            binding_verified_at: null,
            status: 'active',
            first_seen_at: '2026-07-11T10:00:00Z',
            last_seen_at: '2026-07-11T11:00:00Z',
            linked_at: null,
            revoked_at: null,
          },
          {
            id: 'principal-bound',
            provider: 'telegram',
            installation_ref: 'another-technical-installation',
            channel_config_id: null,
            subject_id: '42',
            display_name: 'Telegram Rocky',
            linked_user_id: 'user-1',
            binding_method: 'feishu_qr',
            binding_verified_at: '2026-07-11T10:30:00Z',
            status: 'active',
            first_seen_at: '2026-07-11T10:00:00Z',
            last_seen_at: '2026-07-11T11:00:00Z',
            linked_at: '2026-07-11T10:30:00Z',
            revoked_at: null,
          },
        ]}
        users={[
          { id: 'user-1', display_name: 'Rocky', username: 'rocky', is_active: true },
          { id: 'inactive', display_name: 'Inactive', username: 'inactive', is_active: false },
        ]}
        loading={false}
        busyPrincipalId={null}
        onUnlink={vi.fn()}
      />,
    );

    expect(markup).toContain('External channel identities');
    expect(markup).toContain('Slack Guest');
    expect(markup).toContain('Waiting for the user to verify this identity from the channel connection flow');
    expect(markup).toContain('Rocky');
    expect(markup).toContain('Unlink');
    expect(markup).not.toContain('Bind to invited member');
    expect(markup).not.toContain('<select');
    expect(markup).not.toContain('Inactive');
    expect(markup).not.toContain('technical-installation-uuid');
    expect(markup).not.toContain('technical-config-uuid');
  });
});
