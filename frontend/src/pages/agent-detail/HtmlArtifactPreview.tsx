import { useMemo, useState } from 'react';
import type { useTranslation } from 'react-i18next';

export const HTML_PREVIEW_MAX_LENGTH = 1_000_000;

export function htmlPreviewDocument(content: string): string | null {
  // SVG set/animate can restore a link destination even with scripts disabled.
  const removed = 'link,meta,base,script,iframe,frame,object,embed,portal,template,set,animate';
  const navigation = 'a[href],a[xlink\\:href],a[ping],a[target],area[href],area[xlink\\:href],area[ping],area[target]';
  const parser = new DOMParser();
  let document = parser.parseFromString(content, 'text/html');
  const policy = "default-src 'none'; connect-src 'none'; img-src data:; style-src 'unsafe-inline' data:; script-src 'none'; base-uri 'none'; form-action 'none'; object-src 'none'; frame-src 'none'";
  for (let pass = 0; pass < 2; pass++) {
    document.querySelectorAll(removed).forEach(element => element.remove());
    document.querySelectorAll('a,area').forEach(link => {
      const address = link.getAttribute('href') || link.getAttribute('xlink:href');
      if (address) link.setAttribute('title', address);
      for (const attribute of ['href', 'xlink:href', 'ping', 'target']) link.removeAttribute(attribute);
    });
    const html = document.documentElement.outerHTML;
    document = parser.parseFromString(html, 'text/html');
    if (!document.querySelector(`${removed},${navigation}`)) {
      return `<!doctype html><meta http-equiv="Content-Security-Policy" content="${policy}"><meta http-equiv="x-dns-prefetch-control" content="off">${html}`;
    }
  }
  return null;
}

export default function HtmlArtifactPreview({ content, name, t }: {
  content: string;
  name: string;
  t: ReturnType<typeof useTranslation>['t'];
}) {
  const [showSource, setShowSource] = useState(false);
  const tooLarge = content.length > HTML_PREVIEW_MAX_LENGTH;
  const document = useMemo(() => tooLarge ? null : htmlPreviewDocument(content), [content, tooLarge]);
  return <div>
    <div style={{ display: 'flex', alignItems: 'center', gap: '8px', marginBottom: '10px' }}>
      <span style={{ flex: 1, fontSize: '12px', color: 'var(--text-tertiary)' }}>
        {tooLarge
          ? t('agent.chat.artifacts.htmlTooLarge', 'This file is large. Source is shown; you can also download it.')
          : document === null
            ? t('agent.chat.artifacts.htmlUnavailable', 'Preview unavailable. Source is shown; you can also download it.')
            : t('agent.chat.artifacts.staticHtml', 'Static preview. Page scripts do not run.')}
      </span>
      {document !== null && <button type="button" className="btn btn-secondary btn-sm" onClick={() => setShowSource(!showSource)}>
        {showSource ? t('agent.chat.artifacts.preview', 'Preview') : t('agent.chat.artifacts.viewSource', 'View source')}
      </button>}
    </div>
    {document === null || showSource ? <pre style={{ margin: 0, fontSize: '11px', lineHeight: 1.55, whiteSpace: 'pre-wrap', wordBreak: 'break-word', color: 'var(--text-secondary)' }}>
      {content}
    </pre> : <iframe title={name} sandbox="" referrerPolicy="no-referrer" srcDoc={document}
      style={{ width: '100%', height: '62vh', border: '1px solid var(--border-subtle)', borderRadius: '6px', background: '#fff' }} />}
  </div>;
}
