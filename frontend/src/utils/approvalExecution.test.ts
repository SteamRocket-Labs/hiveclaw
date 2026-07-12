import { describe, expect, it } from 'vitest';

import { approvalContinuationPresentation, approvalExecutionPresentation } from './approvalExecution';


describe('approvalExecutionPresentation', () => {
  it.each([
    ['pending', 'pending', 'Pending'],
    ['queued', 'queued', 'Queued'],
    ['executing', 'executing', 'Executing'],
    ['succeeded', 'succeeded', 'Succeeded'],
    ['failed', 'failed', 'Failed'],
    ['needs_reconciliation', 'needs_reconciliation', 'Needs reconciliation'],
    ['needs_reapproval', 'needs_reapproval', 'Needs reapproval'],
  ])('maps %s to a user-facing durable execution state', (executionStatus, key, label) => {
    expect(
      approvalExecutionPresentation({
        status: executionStatus === 'pending' ? 'pending' : 'approved',
        tool_name: executionStatus === 'pending' ? null : 'write_file',
        execution_status: executionStatus,
      }),
    ).toMatchObject({ key, label });
  });

  it.each([
    ['queued', 'Continuing original session'],
    ['continuing', 'Continuing original session'],
    ['delivered', 'Original session resumed'],
    ['retrying', 'Continuation retrying'],
    ['needs_reconciliation', 'Continuation needs attention'],
  ])('maps continuation %s to a separate user-facing state', (status, label) => {
    expect(
      approvalContinuationPresentation({ execution_receipt: { continuation_status: status } }),
    ).toMatchObject({ status, label });
  });

  it('keeps non-tool decisions as approved or rejected', () => {
    expect(approvalExecutionPresentation({ status: 'approved', tool_name: null })).toMatchObject({
      key: 'approved',
      label: 'Approved',
    });
    expect(approvalExecutionPresentation({ status: 'rejected', tool_name: 'write_file' })).toMatchObject({
      key: 'rejected',
      label: 'Rejected',
    });
  });
});
