// @vitest-environment jsdom

import { beforeEach, expect, it, vi } from 'vitest';

const LEGACY_KEY = 'clawith-accent-color';
const STORAGE_KEY = 'hiveclaw-accent-color';

beforeEach(() => {
    localStorage.clear();
    vi.resetModules();
});

it('migrates the legacy accent key without losing the saved color', async () => {
    localStorage.setItem(LEGACY_KEY, '#123456');

    await import('./theme');

    expect(localStorage.getItem(STORAGE_KEY)).toBe('#123456');
    expect(localStorage.getItem(LEGACY_KEY)).toBeNull();
});

it('keeps the current accent and removes a stale legacy key', async () => {
    localStorage.setItem(STORAGE_KEY, '#abcdef');
    localStorage.setItem(LEGACY_KEY, '#123456');

    await import('./theme');

    expect(localStorage.getItem(STORAGE_KEY)).toBe('#abcdef');
    expect(localStorage.getItem(LEGACY_KEY)).toBeNull();
});
