import { describe, expect, it } from 'vitest';

async function readSource(relativePath: string): Promise<string> {
  const fsModuleId = 'node:fs';
  const { readFileSync } = (await import(/* @vite-ignore */ fsModuleId)) as {
    readFileSync: (path: URL, encoding: string) => string;
  };
  return readFileSync(new URL(relativePath, import.meta.url), 'utf8');
}

describe('session rendering performance source contracts', () => {
  it('display-locks session rows so long loaded histories do not fully paint offscreen content', async () => {
    const css = await readSource('../../index.css');

    expect(css).toContain('.session-tui-render-cell');
    expect(css).toContain('content-visibility: auto');
    expect(css).toContain('contain-intrinsic-size');
  });
});
