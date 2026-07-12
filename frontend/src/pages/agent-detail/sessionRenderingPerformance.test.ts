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

  it('bounds large workspace DOM and display-locks offscreen file rows', async () => {
    const source = await readSource('../../components/FileBrowser.tsx');
    const css = await readSource('../../components/FileBrowser.css');

    expect(source).toContain('visibleFileWindow(files, visibleLimit)');
    expect(source).toContain('setVisibleLimit(FILE_LIST_PAGE_SIZE)');
    expect(source).toContain('file-browser-list-more');
    expect(css).toContain('.file-browser-row');
    expect(css).toContain('content-visibility: auto');
    expect(css).toContain('contain-intrinsic-size: 44px');
  });
});
