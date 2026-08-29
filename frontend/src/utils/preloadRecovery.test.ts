import { readFileSync } from 'node:fs';
import { describe, expect, it, vi } from 'vitest';

import { attemptStalePreloadRecovery } from './preloadRecovery';

function memoryStorage() {
  const values = new Map<string, string>();
  return {
    getItem: (key: string) => values.get(key) ?? null,
    setItem: (key: string, value: string) => { values.set(key, value); },
  };
}

describe('stale preload recovery', () => {
  it('installs recovery before the React route tree can request lazy chunks', () => {
    const main = readFileSync(new URL('../main.tsx', import.meta.url), 'utf8');

    expect(main).toContain("import { installStalePreloadRecovery } from './utils/preloadRecovery'");
    expect(main.indexOf('installStalePreloadRecovery();')).toBeLessThan(main.indexOf('ReactDOM.createRoot'));
  });

  it('reloads the same route once when Vite reports a replaced lazy chunk', () => {
    const storage = memoryStorage();
    const reload = vi.fn();
    const firstEvent = { preventDefault: vi.fn() };

    expect(attemptStalePreloadRecovery(firstEvent, {
      route: '/agents/agent-1/sessions/session-1',
      storage,
      reload,
      now: () => 10_000,
    })).toBe(true);
    expect(firstEvent.preventDefault).toHaveBeenCalledOnce();
    expect(reload).toHaveBeenCalledOnce();

    const repeatedEvent = { preventDefault: vi.fn() };
    expect(attemptStalePreloadRecovery(repeatedEvent, {
      route: '/agents/agent-1/sessions/session-1',
      storage,
      reload,
      now: () => 20_000,
    })).toBe(false);
    expect(repeatedEvent.preventDefault).not.toHaveBeenCalled();
    expect(reload).toHaveBeenCalledOnce();
  });

  it('does not risk a reload loop when recovery state cannot be persisted', () => {
    const event = { preventDefault: vi.fn() };
    const reload = vi.fn();

    expect(attemptStalePreloadRecovery(event, {
      route: '/agents/agent-1',
      storage: {
        getItem: () => { throw new Error('storage unavailable'); },
        setItem: () => { throw new Error('storage unavailable'); },
      },
      reload,
      now: () => 10_000,
    })).toBe(false);
    expect(event.preventDefault).not.toHaveBeenCalled();
    expect(reload).not.toHaveBeenCalled();
  });
});
