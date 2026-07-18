import { describe, expect, it } from 'vitest';

import { phaseFromWeChatPersonalStatus } from './WeChatPersonalSetup';
import en from '../i18n/en.json';
import zh from '../i18n/zh.json';

describe('WeChatPersonalSetup identity state', () => {
  it('does not present a transport-only legacy connection as usable', () => {
    expect(
      phaseFromWeChatPersonalStatus({
        connected: false,
        transport_connected: true,
        identity_status: 'rebind_required',
        requires_rebind: true,
        requires_access_recovery: false,
      }),
    ).toBe('rebind-required');
    expect(
      phaseFromWeChatPersonalStatus({ connected: true } as Parameters<
        typeof phaseFromWeChatPersonalStatus
      >[0]),
    ).toBe('rebind-required');
  });

  it('presents only a verified identity binding as connected', () => {
    expect(
      phaseFromWeChatPersonalStatus({
        connected: true,
        transport_connected: true,
        identity_status: 'verified',
        requires_rebind: false,
        requires_access_recovery: false,
      }),
    ).toBe('done');
    expect(phaseFromWeChatPersonalStatus(null)).toBe('idle');
  });

  it('presents revoked Agent access as account access recovery, not administrator authorization', () => {
    expect(
      phaseFromWeChatPersonalStatus({
        connected: false,
        transport_connected: true,
        identity_status: 'access_denied',
        requires_rebind: false,
        requires_access_recovery: true,
      }),
    ).toBe('access-recovery-required');

    const zhMessages = zh as Record<string, any>;
    const enMessages = en as Record<string, any>;
    expect(zhMessages.agent.settings.channel.accessRecoveryRequired).toBe('绑定账号权限已失效');
    expect(zhMessages.wechatPersonal.qr.accessRecoveryRequired).not.toContain('管理员授权');
    expect(enMessages.agent.settings.channel.accessRecoveryRequired).toBe('Bound account access lost');
  });
});
