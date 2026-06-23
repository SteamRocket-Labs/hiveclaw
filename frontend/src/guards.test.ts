import { describe, expect, it } from 'vitest';

import { protectedLoginRedirect } from './routing/authRedirect';

describe('route guards', () => {
  it('preserves the Local Agent activation URL through login', () => {
    const redirect = protectedLoginRedirect('/local-bridge/activate?user_code=HIVE-1234');

    expect(redirect).toBe('/login?next=%2Flocal-bridge%2Factivate%3Fuser_code%3DHIVE-1234');
  });
});
