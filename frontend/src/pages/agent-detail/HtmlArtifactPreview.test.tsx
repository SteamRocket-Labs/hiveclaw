// @vitest-environment jsdom
import { fireEvent, render, screen, cleanup } from '@testing-library/react';
import { afterEach, expect, it, vi } from 'vitest';
import { createInstance } from 'i18next';
import en from '../../i18n/en.json';
import zh from '../../i18n/zh.json';
import { ArtifactCards, ArtifactPreviewPanel, getArtifactOpenMode, getEffectiveArtifactPreviewKind } from './ArtifactSurface';
import { htmlPreviewDocument } from './HtmlArtifactPreview';

const translations = vi.hoisted(() => ({ t: (_key: string, fallback?: string) => fallback || _key }));
vi.mock('react-i18next', () => ({ useTranslation: () => translations }));
vi.mock('./AgentWorkspaceSection', () => ({ default: () => null }));
afterEach(() => { cleanup(); vi.restoreAllMocks(); });
const t = ((_key: string, fallback?: string) => fallback || _key) as never;
const content = '<h1>Static report</h1><script>parent.postMessage("approval", "*")</script>';

it.each(['report.html', 'REPORT.HTM'])('opens %s as HTML despite legacy preview metadata', name => {
  for (const previewKind of ['text', 'download', undefined]) {
    const artifact = { name, path: `workspace/${name}`, previewKind };
    expect(getEffectiveArtifactPreviewKind(artifact)).toBe('html');
    expect(getArtifactOpenMode(artifact)).toBe('inspector_preview');
  }
});

it('renders a static frame and keeps original source and download available', () => {
  const download = vi.fn();
  render(<ArtifactPreviewPanel preview={{ artifact: { name: 'report.html', path: 'workspace/report.html', previewKind: 'text' }, content }}
    onClose={vi.fn()} onDownload={download} t={t} />);
  const frame = screen.getByTitle('report.html');
  expect(frame.tagName).toBe('IFRAME');
  expect(frame.getAttribute('sandbox')).toBe('');
  expect(frame.getAttribute('src')).toBeNull();
  const srcdoc = frame.getAttribute('srcdoc')!;
  expect(srcdoc).toContain("default-src 'none'");
  expect(srcdoc).toContain("connect-src 'none'");
  expect(srcdoc).toContain("script-src 'none'");
  expect(srcdoc).toContain('x-dns-prefetch-control');
  expect(srcdoc.indexOf('Content-Security-Policy')).toBeLessThan(srcdoc.indexOf('<h1>Static report</h1>'));
  expect(srcdoc).not.toContain('<script>');
  fireEvent.click(screen.getByRole('button', { name: 'View source' }));
  expect(screen.getByText(content).tagName).toBe('PRE');
  expect(screen.queryByTitle('report.html')).toBeNull();
  fireEvent.click(screen.getByRole('button', { name: /^Preview$/ }));
  expect(screen.getByTitle('report.html').getAttribute('sandbox')).toBe('');
  fireEvent.click(screen.getByRole('button', { name: 'Download report.html' }));
  expect(download).toHaveBeenCalledOnce();
});

it.each(['link', 'meta', 'base', 'script', 'iframe', 'frame', 'object', 'embed', 'portal', 'template', 'set', 'animate'])('removes %s before creating srcdoc', tag => {
  const markup = `<${tag} href="/probe" src="/probe" srcdoc="<link rel=prerender href=/nested>" ${tag === 'template' ? 'shadowrootmode="open"' : ''}></${tag}>`;
  const output = htmlPreviewDocument(`<h1>Keep</h1>${markup}<noscript>${markup}</noscript>`)!;
  const parsed = new DOMParser().parseFromString(output, 'text/html');
  expect(parsed.querySelector('h1')?.textContent).toBe('Keep');
  // Only the two trusted policy/DNS meta elements may remain.
  if (tag === 'meta') expect(parsed.querySelectorAll('meta')).toHaveLength(2);
  else expect(parsed.querySelector(tag)).toBeNull();
});

it('removes encoded prerender, speculation rules and declarative shadow documents', () => {
  const output = htmlPreviewDocument(`<noscript><link rel="prere&#110;der" href="/encoded"></noscript>
    <iframe srcdoc="<link rel=&quot;prerender&quot; href=&quot;/nested&quot;>"></iframe>
    <script type="speculationrules">{"prerender":[{"urls":["/speculation"]}]}</script>
    <div><template shadowrootmode="open"><link rel="prerender" href="/shadow"></template></div>`)!;
  const parsed = new DOMParser().parseFromString(output, 'text/html');
  expect(parsed.querySelector('link,iframe,script,template')).toBeNull();
});

it('keeps HTML/area/SVG link text and addresses as titles but removes every navigation attribute', () => {
  const output = htmlPreviewDocument(`<a href="/html?x=1&amp;y=2" ping="/ping" target="_blank">HTML</a>
    <map><area href="/area" ping="/ping" target="_top" alt="Area"></map>
    <svg xmlns:xlink="http://www.w3.org/1999/xlink"><a xlink:href="/svg" href="/svg2" ping="/ping" target="_blank"><text>SVG</text></a>
    <a xlink:href="/legacy"><text>Legacy</text></a></svg>`)!;
  const parsed = new DOMParser().parseFromString(output, 'text/html');
  const links = [...parsed.querySelectorAll('a,area')];
  expect(links.map(link => link.getAttribute('title'))).toEqual(['/html?x=1&y=2', '/area', '/svg2', '/legacy']);
  expect(parsed.body.textContent).toContain('HTML');
  expect(parsed.body.textContent).toContain('SVG');
  for (const link of links) {
    for (const attr of ['href', 'xlink:href', 'ping', 'target']) expect(link.hasAttribute(attr)).toBe(false);
  }
});

it('removes SVG animations that can restore a link after initial href removal', () => {
  const output = htmlPreviewDocument('<svg><a href="/original"><set attributeName="href" to="/set"/><animate attributeName="href" values="/animate" dur="1s"/><text>Link</text></a></svg>')!;
  const parsed = new DOMParser().parseFromString(output, 'text/html');
  expect(parsed.querySelector('set,animate')).toBeNull();
  expect(parsed.querySelector('a')?.getAttribute('href')).toBeNull();
  expect(parsed.querySelector('a')?.getAttribute('title')).toBe('/original');
  expect(parsed.querySelector('text')?.textContent).toBe('Link');
});

it.each([
  '<math><mtext><table><mglyph><style><!--</style><img title="--><link rel=prerender href=/mutation>">',
  '<svg><foreignObject><noscript><link rel=prerender href=/nested></noscript></foreignObject></svg>',
  '<noscript><p title="</noscript><link rel=prerender href=/escaped>">text</p></noscript>',
])('stays inert after another browser parse: %s', content => {
  const output = htmlPreviewDocument(content)!;
  expect(output).not.toBeNull();
  expect(htmlPreviewDocument(output)).toBe(output);
  const reparsed = new DOMParser().parseFromString(output, 'text/html');
  expect(reparsed.querySelector('link,base,script,iframe,frame,object,embed,portal,template,a[href],area[href]')).toBeNull();
});

it('cleans a reparse mutation on the second pass', () => {
  const parse = DOMParser.prototype.parseFromString;
  let calls = 0;
  vi.spyOn(DOMParser.prototype, 'parseFromString').mockImplementation(function (this: DOMParser, text, type) {
    const doc = parse.call(this, text, type);
    if (++calls === 2) doc.body.appendChild(doc.createElement('iframe'));
    return doc;
  });
  const output = htmlPreviewDocument('<h1>Recovered</h1>')!;
  expect(output).toContain('Recovered');
  expect(output).not.toContain('<iframe');
});

it('shows original source instead of a frame when removed nodes keep reappearing', () => {
  const parse = DOMParser.prototype.parseFromString;
  vi.spyOn(DOMParser.prototype, 'parseFromString').mockImplementation(function (this: DOMParser, text, type) {
    const doc = parse.call(this, text, type);
    doc.body.appendChild(doc.createElement('link'));
    return doc;
  });
  render(<ArtifactPreviewPanel preview={{ artifact: { name: 'unstable.html', path: 'workspace/unstable.html' }, content }}
    onClose={vi.fn()} onDownload={vi.fn()} t={t} />);
  expect(screen.queryByTitle('unstable.html')).toBeNull();
  expect(screen.getByText(content).tagName).toBe('PRE');
  expect(screen.getByText('Preview unavailable. Source is shown; you can also download it.')).toBeTruthy();
  expect(screen.getByRole('button', { name: 'Download unstable.html' })).toBeTruthy();
});

it.each([
  { name: 'chart.svg', path: 'workspace/chart.svg' },
  { name: 'CHART.SVG', path: 'workspace/CHART.SVG' },
  { name: 'chart', path: 'workspace/chart.svg' },
  { name: 'chart', path: 'workspace/chart', mimeType: 'image/svg+xml' },
])('does not offer a new tab for SVG $name but keeps image and download', artifact => {
  const { name } = artifact;
  render(<ArtifactPreviewPanel preview={{ artifact: { ...artifact, previewKind: 'image' }, url: 'blob:svg' }}
    onClose={vi.fn()} onDownload={vi.fn()} t={t} />);
  expect(screen.queryByTitle('Open in new tab')).toBeNull();
  expect(screen.getByRole('img', { name }).getAttribute('src')).toBe('blob:svg');
  expect(screen.getByRole('button', { name: `Download ${name}` })).toBeTruthy();
});

it('keeps new-tab behavior for other images', () => {
  render(<ArtifactPreviewPanel preview={{ artifact: { name: 'chart.png', path: 'workspace/chart.png' }, url: 'blob:png' }}
    onClose={vi.fn()} t={t} />);
  expect(screen.getByTitle('Open in new tab').getAttribute('href')).toBe('blob:png');
});

it.each(['en', 'zh'])('labels HTML result rows as preview in %s, including legacy download metadata', async lng => {
  const i18n = createInstance();
  await i18n.init({ lng, resources: { en: { translation: en }, zh: { translation: zh } } });
  vi.spyOn(translations, 't').mockImplementation(i18n.t as typeof translations.t);
  render(<ArtifactCards artifacts={[{ name: 'report.html', path: 'workspace/report.html', previewKind: 'download', size: 123 }]}
    agentId="agent" onOpenArtifact={vi.fn()} />);
  const meta = screen.getByTestId('chat-artifact-row-open').querySelector('.chat-artifact-meta');
  expect(meta?.textContent).toBe(`${lng === 'zh' ? '预览' : 'Preview'} · 123 B`);
});

it('shows source and a size note instead of executing a very large document', () => {
  const large = '<h1>Large report</h1>' + 'x'.repeat(1_000_000);
  render(<ArtifactPreviewPanel preview={{ artifact: { name: 'large.htm', path: 'workspace/large.htm' }, content: large }}
    onClose={vi.fn()} onDownload={vi.fn()} t={t} />);
  expect(screen.queryByTitle('large.htm')).toBeNull();
  expect(screen.getByText('This file is large. Source is shown; you can also download it.')).toBeTruthy();
  expect(screen.getByText(large).tagName).toBe('PRE');
  expect(screen.getByRole('button', { name: 'Download large.htm' })).toBeTruthy();
});
