import { renderToStaticMarkup } from 'react-dom/server';
import { describe, expect, it } from 'vitest';

import MarkdownRenderer from './MarkdownRenderer';

// 渲染契约：MarkdownRenderer 只输出语义 class（.md-content 体系），
// 不再内嵌 inline style —— 视觉全部由 index.css 承担。

describe('MarkdownRenderer', () => {
  it('wraps output in md-content and renders semantic classes', () => {
    const html = renderToStaticMarkup(
      <MarkdownRenderer content={'# Title\n\nBody with `code` and **bold**.\n\n- item one\n- item two'} />,
    );
    expect(html).toContain('class="md-content"');
    expect(html).toContain('class="md-h md-h1"');
    expect(html).toContain('class="md-code"');
    expect(html).toContain('class="md-list"');
    expect(html).toContain('<strong>bold</strong>');
  });

  it('does not emit inline styles from markdown structures', () => {
    const html = renderToStaticMarkup(
      <MarkdownRenderer
        content={'## Head\n\n> quote line\n\n| a | b |\n| - | - |\n| 1 | 2 |\n\n```js\nconst x = 1;\n```'}
      />,
    );
    expect(html).toContain('class="md-quote"');
    expect(html).toContain('class="md-table"');
    expect(html).toContain('class="md-pre"');
    expect(html).not.toMatch(/<(p|h2|pre|table|blockquote|code)[^>]*style=/);
  });

  it('treats blank lines as block separators without emitting <br>', () => {
    const html = renderToStaticMarkup(<MarkdownRenderer content={'para one\n\npara two'} />);
    expect(html).toContain('<p>para one</p>');
    expect(html).toContain('<p>para two</p>');
    expect(html).not.toContain('<br');
  });

  it('renders links with md-link and external rel', () => {
    const html = renderToStaticMarkup(<MarkdownRenderer content={'[docs](https://example.com)'} />);
    expect(html).toContain('class="md-link"');
    expect(html).toContain('rel="noopener noreferrer"');
  });

  it('never renders raw HTML or executable event attributes', () => {
    const html = renderToStaticMarkup(
      <MarkdownRenderer
        content={'before <img src=x onerror="globalThis.pwned=true"><script>globalThis.pwned=true</script> after'}
      />,
    );

    expect(html).not.toContain('<img src="x"');
    expect(html).not.toContain('<script>');
    expect(html).not.toContain('onerror=');
    expect(html).toContain('before');
    expect(html).toContain('after');
  });

  it('rejects executable and credential-bearing markdown URLs', () => {
    const html = renderToStaticMarkup(
      <MarkdownRenderer
        content={'[unsafe](javascript:alert(1)) ![bad](data:text/html;base64,PHNjcmlwdD4=) [token](/api/agents/a/files/download?token=secret)'}
      />,
    );

    expect(html).not.toContain('javascript:');
    expect(html).not.toContain('data:text/html');
    expect(html).not.toContain('token=secret');
  });
});
