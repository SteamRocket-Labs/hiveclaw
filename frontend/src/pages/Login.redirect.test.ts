import { describe, expect, it } from 'vitest';

import { safePostLoginRedirect } from '../routing/authRedirect';

describe('Login redirect target', () => {
  it('allows returning to a local activation URL after successful login', () => {
    expect(safePostLoginRedirect('/local-bridge/activate?user_code=HIVE-1234')).toBe(
      '/local-bridge/activate?user_code=HIVE-1234',
    );
  });

  it('rejects external redirect targets', () => {
    expect(safePostLoginRedirect('https://evil.example/phish')).toBe('/');
    expect(safePostLoginRedirect('//evil.example/phish')).toBe('/');
  });
});
