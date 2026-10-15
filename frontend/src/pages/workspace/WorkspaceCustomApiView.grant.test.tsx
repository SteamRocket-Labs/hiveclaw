// @vitest-environment jsdom

import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';

import en from '../../i18n/en.json';
import zh from '../../i18n/zh.json';
import { translateFromCatalog } from '../../test/i18nMock';
import WorkspaceCustomApiView from './WorkspaceCustomApiView';

const mocks = vi.hoisted(() => ({
  list: vi.fn(),
  setNetworkGrant: vi.fn(),
  showAppToast: vi.fn(),
}));

vi.mock('react-i18next', () => ({
  useTranslation: () => ({
    t: (key: string, fallback?: string) => translateFromCatalog(zh, key, fallback),
    i18n: { language: 'zh' },
  }),
}));

vi.mock('../../api/domains/customApiConnectors', () => ({
  customApiConnectorsApi: {
    list: mocks.list,
    setNetworkGrant: mocks.setNetworkGrant,
    create: vi.fn(),
    delete: vi.fn(),
    test: vi.fn(),
  },
}));

vi.mock('../../components/AppDialogs', () => ({
  requestAppConfirm: vi.fn(),
  showAppToast: mocks.showAppToast,
}));

const connector = {
  id: 'connector-1',
  name: 'custom_api__review__read',
  display_name: '内网测试连接',
  description: '',
  enabled: true,
  is_default: false,
  parameters_schema: {},
  config_schema: {},
  masked_secrets: {},
  config: { approved_networks: ['10.0.0.0/8'] },
};

describe('custom API network grant label', () => {
  afterEach(() => cleanup());

  it('shows the Chinese label and the canonical grant returned by the server', async () => {
    expect(en.enterprise.tools.saveNetworkGrant).toBe('Save network grant');
    expect(zh.enterprise.tools.saveNetworkGrant).toBe('保存网络授权');
    mocks.list.mockResolvedValue([connector]);
    mocks.setNetworkGrant.mockResolvedValue({
      ok: true,
      tool_id: connector.id,
      approved_networks: ['10.8.0.0/16'],
    });

    render(<WorkspaceCustomApiView selectedTenantId="tenant-1" />);
    const input = (await screen.findByLabelText('已批准的内网网段')) as HTMLInputElement;
    expect(input.value).toBe('10.0.0.0/8');
    fireEvent.change(input, { target: { value: '10.8.3.7/16' } });
    fireEvent.click(screen.getByRole('button', { name: '保存网络授权' }));

    await waitFor(() => expect(input.value).toBe('10.8.0.0/16'));
    expect(mocks.setNetworkGrant).toHaveBeenCalledWith(connector.id, ['10.8.3.7/16']);
    expect(mocks.showAppToast).toHaveBeenCalledWith('网络授权已保存', 'success');
  });

  it('keeps the typed grant when saving fails and can clear it', async () => {
    mocks.list.mockResolvedValue([connector]);
    mocks.setNetworkGrant.mockRejectedValueOnce(new Error('Invalid CIDR'));

    render(<WorkspaceCustomApiView selectedTenantId="tenant-1" />);
    const input = (await screen.findByLabelText('已批准的内网网段')) as HTMLInputElement;
    fireEvent.change(input, { target: { value: 'bad-cidr' } });
    fireEvent.click(screen.getByRole('button', { name: '保存网络授权' }));
    await waitFor(() => expect(mocks.showAppToast).toHaveBeenCalledWith('Invalid CIDR', 'error'));
    expect(input.value).toBe('bad-cidr');

    mocks.setNetworkGrant.mockResolvedValueOnce({ ok: true, tool_id: connector.id, approved_networks: [] });
    fireEvent.change(input, { target: { value: '' } });
    fireEvent.click(screen.getByRole('button', { name: '保存网络授权' }));
    await waitFor(() => expect(input.value).toBe(''));
    expect(mocks.setNetworkGrant).toHaveBeenLastCalledWith(connector.id, []);
  });
});
