import { expect, it } from 'vitest';
import { triggerScheduleLabel } from './wakePolicyForm';

it('keeps the original unknown type fallback for administrator views', () => {
  expect(triggerScheduleLabel({ type: 'future_trigger' }, (_key, fallback) => fallback)).toBe('future_trigger');
});
