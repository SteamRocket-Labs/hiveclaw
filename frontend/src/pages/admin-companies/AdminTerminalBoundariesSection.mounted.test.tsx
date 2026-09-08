// @vitest-environment jsdom
//
// Mounted interaction tests for AdminTerminalBoundariesSection: the real
// component is rendered in jsdom with @testing-library/react while only the
// API domain boundary is mocked (real i18n catalog). These tests prove the
// explicit terminal-boundary recovery operator path: truthful dead-letter
// listing for the selected company, required audit reason, explicit
// confirmation, exact adapter arguments (no silent summary_disposition),
// single-flight redrive, and truthful post-mutation reload behavior.

import { StrictMode } from 'react';
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { adminApi, type RuntimeTerminalBoundary } from '../../api/domains/admin';
import '../../i18n';

vi.mock('../../api/domains/admin', () => ({
  adminApi: {
    listRuntimeTerminalBoundaries: vi.fn(),
    redriveRuntimeTerminalBoundary: vi.fn(),
  },
}));

import AdminTerminalBoundariesSection from './AdminTerminalBoundariesSection';

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

const api = vi.mocked(adminApi);

function makeBoundary(id: string, overrides: Partial<RuntimeTerminalBoundary> = {}): RuntimeTerminalBoundary {
  return {
    id,
    runtime_task_id: '0f0e0d0c-1111-4222-8333-444455556666',
    agent_id: '1a2b3c4d-5e6f-4788-9900-aabbccddeeff',
    session_id: 'runtime-session-7',
    event_kind: 'turn_stop',
    terminal_status: 'completed',
    authority_ref: 'session_run_outcome',
    authority_id: '42',
    status: 'dead_letter',
    attempt_count: 8,
    last_error: 'LLMError',
    available_at: '2026-09-08T00:00:00Z',
    delivered_at: null,
    created_at: '2026-09-08T00:00:00Z',
    updated_at: '2026-09-08T01:00:00Z',
    ...overrides,
  };
}

function deferred<T>() {
  let resolve!: (value: T) => void;
  let reject!: (reason?: unknown) => void;
  const promise = new Promise<T>((res, rej) => {
    resolve = res;
    reject = rej;
  });
  return { promise, resolve, reject };
}

function redriveButton(label: string): HTMLButtonElement {
  return screen.getByRole('button', { name: `Redrive — ${label}` }) as HTMLButtonElement;
}

beforeEach(() => {
  vi.clearAllMocks();
  localStorage.clear();
});

afterEach(() => {
  cleanup();
});

describe('AdminTerminalBoundariesSection — explicit dead-letter redrive (mounted)', () => {
  it('auto-loads the dead_letter queue for the selected company on mount without any mutation', async () => {
    const row = makeBoundary('b0b0b0b0-1111-4222-8333-444455556666');
    api.listRuntimeTerminalBoundaries.mockResolvedValue([row]);

    render(
      <StrictMode>
        <AdminTerminalBoundariesSection initialTenantId="tenant-1" />
      </StrictMode>,
    );

    await screen.findByText('1 boundaries');
    expect(api.listRuntimeTerminalBoundaries).toHaveBeenCalledTimes(1);
    expect(api.listRuntimeTerminalBoundaries).toHaveBeenCalledWith({
      tenantId: 'tenant-1',
      status: 'dead_letter',
      limit: 100,
    });
    expect(api.redriveRuntimeTerminalBoundary).not.toHaveBeenCalled();
    // The exact boundary identity and truthful failure evidence are visible.
    expect(screen.getByText(`Boundary ${row.id}`)).toBeTruthy();
    expect(screen.getByText(`Runtime task ${row.runtime_task_id}`)).toBeTruthy();
    expect(screen.getByText(/session runtime-session-7/)).toBeTruthy();
    expect(screen.getByText(/Last error: LLMError/)).toBeTruthy();
    expect(screen.getByText(/attempts 8/)).toBeTruthy();
    // The recomputation-risk disclosure is always visible, not modal-only.
    expect(screen.getByText(
      'Summary recomputation can repeat previously accepted summary subcalls and incur extra usage.',
    )).toBeTruthy();
  });

  it('shows a failed initial load as an error, never as an empty success', async () => {
    api.listRuntimeTerminalBoundaries.mockRejectedValue(new Error('HTTP 500'));

    render(<AdminTerminalBoundariesSection initialTenantId="tenant-1" />);

    await screen.findByRole('alert');
    expect(screen.getByText('HTTP 500')).toBeTruthy();
    expect(screen.queryByText('0 boundaries')).toBeNull();
    expect(screen.queryByText('No terminal boundaries in this state.')).toBeNull();
  });

  it('requires an audit reason and an explicit confirm; sends no summary_disposition by default', async () => {
    const row = makeBoundary('b1b1b1b1-1111-4222-8333-444455556666');
    api.listRuntimeTerminalBoundaries.mockResolvedValue([row]);
    const redriven = makeBoundary(row.id, { status: 'pending', attempt_count: 9 });
    api.redriveRuntimeTerminalBoundary.mockResolvedValue(redriven);

    render(<AdminTerminalBoundariesSection initialTenantId="tenant-1" />);
    await screen.findByText('1 boundaries');

    const button = redriveButton('runtime-session-7');
    expect(button.disabled).toBe(true);
    fireEvent.change(screen.getByPlaceholderText('Required audit reason'), {
      target: { value: 'Owner-approved recovery of the stuck second-turn input.' },
    });
    expect(button.disabled).toBe(false);
    fireEvent.click(button);

    // The confirm dialog is the explicit gate; nothing has been sent yet.
    expect(api.redriveRuntimeTerminalBoundary).not.toHaveBeenCalled();
    fireEvent.click(screen.getByRole('button', { name: /^Redrive$/ }));

    await waitFor(() => expect(api.redriveRuntimeTerminalBoundary).toHaveBeenCalledWith(
      row.id,
      {
        tenantId: 'tenant-1',
        reason: 'Owner-approved recovery of the stuck second-turn input.',
      },
    ));
    // Truthful requeued receipt, then an in-place reload of the same state.
    await screen.findByText(
      `Boundary ${row.id} requeued with status pending. Requeued means it will be attempted again — not delivered and not business success.`,
    );
    await waitFor(() => expect(api.listRuntimeTerminalBoundaries).toHaveBeenCalledTimes(2));
    expect(api.listRuntimeTerminalBoundaries).toHaveBeenLastCalledWith({
      tenantId: 'tenant-1',
      status: 'dead_letter',
      limit: 100,
    });
  });

  it('sends summary_disposition=retry only when the operator explicitly opts in', async () => {
    const row = makeBoundary('b2b2b2b2-1111-4222-8333-444455556666');
    api.listRuntimeTerminalBoundaries.mockResolvedValue([row]);
    api.redriveRuntimeTerminalBoundary.mockResolvedValue(
      makeBoundary(row.id, { status: 'pending' }),
    );

    render(<AdminTerminalBoundariesSection initialTenantId="tenant-1" />);
    await screen.findByText('1 boundaries');

    fireEvent.change(screen.getByPlaceholderText('Required audit reason'), {
      target: { value: 'Retry summary projection after the model switch.' },
    });
    fireEvent.click(screen.getByRole('checkbox'));
    fireEvent.click(redriveButton('runtime-session-7'));
    // The confirm dialog carries the recomputation-risk disclosure.
    expect(screen.getByText(/Summary recomputation is requested and can repeat previously accepted summary subcalls/)).toBeTruthy();
    fireEvent.click(screen.getByRole('button', { name: /^Redrive$/ }));

    await waitFor(() => expect(api.redriveRuntimeTerminalBoundary).toHaveBeenCalledWith(
      row.id,
      {
        tenantId: 'tenant-1',
        reason: 'Retry summary projection after the model switch.',
        summaryDisposition: 'retry',
      },
    ));
  });

  it('cannot double-submit while a redrive is in flight', async () => {
    const row = makeBoundary('b3b3b3b3-1111-4222-8333-444455556666');
    api.listRuntimeTerminalBoundaries.mockResolvedValue([row]);
    const gate = deferred<RuntimeTerminalBoundary>();
    api.redriveRuntimeTerminalBoundary.mockReturnValue(gate.promise);

    render(<AdminTerminalBoundariesSection initialTenantId="tenant-1" />);
    await screen.findByText('1 boundaries');

    fireEvent.change(screen.getByPlaceholderText('Required audit reason'), {
      target: { value: 'One explicit recovery.' },
    });
    const button = redriveButton('runtime-session-7');
    fireEvent.click(button);
    fireEvent.click(screen.getByRole('button', { name: /^Redrive$/ }));
    expect(api.redriveRuntimeTerminalBoundary).toHaveBeenCalledTimes(1);

    // While in flight, every operator control (including the reason field and
    // refresh) is disabled, so a second submit cannot be issued.
    expect(button.disabled).toBe(true);
    expect((screen.getByPlaceholderText('Required audit reason') as HTMLInputElement).disabled).toBe(true);
    expect((screen.getByRole('button', { name: 'Refresh' }) as HTMLButtonElement).disabled).toBe(true);

    gate.resolve(makeBoundary(row.id, { status: 'pending' }));
    await waitFor(() => expect(api.redriveRuntimeTerminalBoundary).toHaveBeenCalledTimes(1));
    await screen.findByText(/requeued with status pending/);
  });

  it('shows the redrive error and keeps the queue truthful when the mutation fails', async () => {
    const row = makeBoundary('b4b4b4b4-1111-4222-8333-444455556666');
    api.listRuntimeTerminalBoundaries.mockResolvedValue([row]);
    api.redriveRuntimeTerminalBoundary.mockRejectedValue(new Error('HTTP 409: only a dead-letter terminal boundary can be redriven'));

    render(<AdminTerminalBoundariesSection initialTenantId="tenant-1" />);
    await screen.findByText('1 boundaries');

    fireEvent.change(screen.getByPlaceholderText('Required audit reason'), {
      target: { value: 'Attempt recovery.' },
    });
    fireEvent.click(redriveButton('runtime-session-7'));
    fireEvent.click(screen.getByRole('button', { name: /^Redrive$/ }));

    await screen.findByText(/only a dead-letter terminal boundary can be redriven/);
    // No reload was triggered by the failed mutation, and the row is unchanged.
    expect(api.listRuntimeTerminalBoundaries).toHaveBeenCalledTimes(1);
    expect(screen.getByText('1 boundaries')).toBeTruthy();
    // The safe next action remains available: the operator control re-enables.
    await waitFor(() => expect(redriveButton('runtime-session-7').disabled).toBe(false));
  });

  it('clears rows on tenant change so an old-company row cannot execute under another selected company', async () => {
    const row = makeBoundary('b5b5b5b5-1111-4222-8333-444455556666');
    api.listRuntimeTerminalBoundaries.mockResolvedValue([row]);

    render(<AdminTerminalBoundariesSection initialTenantId="tenant-1" />);
    await screen.findByText('1 boundaries');

    const tenantInput = screen.getByPlaceholderText('Tenant ID') as HTMLInputElement;
    fireEvent.change(tenantInput, { target: { value: 'tenant-2' } });

    expect(screen.getByText('Queue not loaded')).toBeTruthy();
    expect(screen.queryByText(`Boundary ${row.id}`)).toBeNull();
    expect(screen.queryByRole('button', { name: /Redrive —/ })).toBeNull();

    // Loading the new company only happens through an explicit Refresh and
    // carries the new tenant binding.
    api.listRuntimeTerminalBoundaries.mockClear();
    fireEvent.click(screen.getByRole('button', { name: 'Refresh' }));
    await waitFor(() => expect(api.listRuntimeTerminalBoundaries).toHaveBeenCalledWith({
      tenantId: 'tenant-2',
      status: 'dead_letter',
      limit: 100,
    }));
  });

  it('does not present a previously empty queue as truth after a failed refresh', async () => {
    api.listRuntimeTerminalBoundaries.mockResolvedValueOnce([]);

    render(<AdminTerminalBoundariesSection initialTenantId="tenant-1" />);
    await screen.findByText('0 boundaries');
    expect(screen.getByText('No terminal boundaries in this state.')).toBeTruthy();

    api.listRuntimeTerminalBoundaries.mockRejectedValueOnce(new Error('HTTP 503'));
    fireEvent.click(screen.getByRole('button', { name: 'Refresh' }));

    await screen.findByText('HTTP 503');
    // The successful-empty conclusion is invalidated by the failed read: no
    // count and no empty message may masquerade as current truth next to the
    // error, and Refresh remains the safe next action.
    expect(screen.queryByText('0 boundaries')).toBeNull();
    expect(screen.queryByText('No terminal boundaries in this state.')).toBeNull();
    expect((screen.getByRole('button', { name: 'Refresh' }) as HTMLButtonElement).disabled).toBe(false);
  });

  it('keeps the requeued receipt but stops acting on stale rows when the post-redrive reload fails', async () => {
    const row = makeBoundary('b6b6b6b6-1111-4222-8333-444455556666');
    api.listRuntimeTerminalBoundaries.mockResolvedValueOnce([row]);
    api.redriveRuntimeTerminalBoundary.mockResolvedValue(
      makeBoundary(row.id, { status: 'pending' }),
    );
    api.listRuntimeTerminalBoundaries.mockRejectedValueOnce(new Error('HTTP 500'));

    render(<AdminTerminalBoundariesSection initialTenantId="tenant-1" />);
    await screen.findByText('1 boundaries');

    fireEvent.change(screen.getByPlaceholderText('Required audit reason'), {
      target: { value: 'Owner-approved recovery; reload failed afterwards.' },
    });
    fireEvent.click(redriveButton('runtime-session-7'));
    fireEvent.click(screen.getByRole('button', { name: /^Redrive$/ }));

    // The truthful requeued receipt survives the failed reload and is shown
    // alongside the reload error.
    await screen.findByText(
      `Boundary ${row.id} requeued with status pending. Requeued means it will be attempted again — not delivered and not business success.`,
    );
    await screen.findByText('HTTP 500');
    // The old dead-letter rows are no longer bound truth: no count claim and
    // no actionable Redrive control until an explicit Refresh succeeds.
    expect(screen.queryByText('1 boundaries')).toBeNull();
    expect(screen.queryByRole('button', { name: /Redrive —/ })).toBeNull();
    expect((screen.getByRole('button', { name: 'Refresh' }) as HTMLButtonElement).disabled).toBe(false);
  });
});
