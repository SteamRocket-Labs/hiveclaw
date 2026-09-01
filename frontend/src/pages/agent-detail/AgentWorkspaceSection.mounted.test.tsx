// @vitest-environment jsdom

import React from 'react';
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

const harness = vi.hoisted(() => ({
  read: vi.fn(),
  nextBrowserId: 0,
}));

vi.mock('react-i18next', () => ({
  useTranslation: () => ({ t: (_key: string, fallback?: string) => fallback ?? _key }),
}));

vi.mock('../../api/domains/files', () => ({
  fileApi: {
    list: vi.fn(),
    read: harness.read,
    versions: vi.fn(),
    readVersion: vi.fn(),
    restoreVersion: vi.fn(),
    downloadVersion: vi.fn(),
    write: vi.fn(),
    delete: vi.fn(),
    upload: vi.fn(),
    download: vi.fn(),
  },
}));

vi.mock('../../components/FileBrowser', () => ({
  default: ({ api }: { api: { read: (path: string) => Promise<any> } }) => {
    const [browserId] = React.useState(() => ++harness.nextBrowserId);
    const [text, setText] = React.useState('');
    return (
      <div data-testid="file-browser" data-browser-id={browserId}>
        <button type="button" onClick={() => void api.read('workspace/private.txt').then((result) => setText(result.content))}>
          Load workspace file
        </button>
        <span>{text}</span>
      </div>
    );
  },
}));

import AgentWorkspaceSection from './AgentWorkspaceSection';

beforeEach(() => {
  harness.nextBrowserId = 0;
  harness.read.mockReset();
  harness.read.mockImplementation((_agentId, _path, authority) => Promise.resolve({
    content: authority ? `PRIVATE:${authority.reason}` : 'OWNER',
  }));
});

afterEach(cleanup);

describe('AgentWorkspaceSection operator authority lifetime', () => {
  it('exits operator view and remounts FileBrowser when reason or capability changes', async () => {
    const view = render(
      <AgentWorkspaceSection agentId="agent-1" canUseOperatorView operatorReason="Reason A" />,
    );

    fireEvent.click(screen.getByRole('button', { name: 'Enter operator view' }));
    expect(screen.getByText('Operator view')).toBeTruthy();
    const reasonABrowserId = screen.getByTestId('file-browser').getAttribute('data-browser-id');
    fireEvent.click(screen.getByRole('button', { name: 'Load workspace file' }));
    expect(await screen.findByText('PRIVATE:Reason A')).toBeTruthy();
    expect(harness.read).toHaveBeenLastCalledWith(
      'agent-1',
      'workspace/private.txt',
      { operatorView: true, reason: 'Reason A' },
    );

    view.rerender(
      <AgentWorkspaceSection agentId="agent-1" canUseOperatorView operatorReason="Reason B" />,
    );
    expect(screen.queryByText('PRIVATE:Reason A')).toBeNull();
    expect(screen.queryByText('Operator view')).toBeNull();
    expect(screen.getByTestId('file-browser').getAttribute('data-browser-id')).not.toBe(reasonABrowserId);

    fireEvent.click(screen.getByRole('button', { name: 'Enter operator view' }));
    fireEvent.click(screen.getByRole('button', { name: 'Load workspace file' }));
    expect(await screen.findByText('PRIVATE:Reason B')).toBeTruthy();

    view.rerender(
      <AgentWorkspaceSection agentId="agent-1" canUseOperatorView={false} operatorReason="Reason B" />,
    );
    await waitFor(() => {
      expect(screen.queryByText('PRIVATE:Reason B')).toBeNull();
      expect(screen.queryByText('Operator view')).toBeNull();
      expect(screen.queryByRole('button', { name: 'Enter operator view' })).toBeNull();
    });
  });

  it('gates operator-only workspace until a reason is committed, then enters audited read-only mode without a second toggle', async () => {
    const view = render(
      <AgentWorkspaceSection
        agentId="agent-1"
        canUseOperatorView
        operatorOnly
        operatorReason=""
      />,
    );

    expect(screen.getByTestId('operator-workspace-reason-gate')).toBeTruthy();
    expect(screen.queryByTestId('file-browser')).toBeNull();
    expect(harness.read).not.toHaveBeenCalled();

    view.rerender(
      <AgentWorkspaceSection
        agentId="agent-1"
        canUseOperatorView
        operatorOnly
        operatorReason="Reason A"
      />,
    );
    expect(screen.queryByTestId('operator-workspace-reason-gate')).toBeNull();
    expect(screen.queryByRole('button', { name: 'Enter operator view' })).toBeNull();
    expect(screen.getByText('Operator view')).toBeTruthy();

    fireEvent.click(screen.getByRole('button', { name: 'Load workspace file' }));
    expect(await screen.findByText('PRIVATE:Reason A')).toBeTruthy();
    expect(harness.read).toHaveBeenCalledTimes(1);
    expect(harness.read).toHaveBeenLastCalledWith(
      'agent-1',
      'workspace/private.txt',
      { operatorView: true, reason: 'Reason A' },
    );

    view.rerender(
      <AgentWorkspaceSection
        agentId="agent-1"
        canUseOperatorView={false}
        operatorOnly
        operatorReason="Reason A"
      />,
    );
    expect(screen.getByTestId('operator-workspace-reason-gate')).toBeTruthy();
    expect(screen.queryByTestId('file-browser')).toBeNull();
    expect(screen.queryByText('PRIVATE:Reason A')).toBeNull();
  });
});
