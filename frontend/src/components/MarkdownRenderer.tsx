import React, { useMemo, useState } from 'react';
import ReactMarkdown, { defaultUrlTransform, type Components, type UrlTransform } from 'react-markdown';
import rehypeSanitize from 'rehype-sanitize';
import remarkGfm from 'remark-gfm';

import {
  apiPathFromBrowserUrl,
  downloadAuthenticatedBrowserResource,
} from '../utils/authenticatedResource';
import AuthenticatedImage from './AuthenticatedImage';

const CREDENTIAL_QUERY_KEYS = new Set([
  'access_token',
  'api_key',
  'apikey',
  'auth',
  'authorization',
  'bearer',
  'token',
]);

function containsCredentialQuery(value: string): boolean {
  try {
    const url = new URL(value, 'http://localhost');
    return [...url.searchParams.keys()].some((key) => CREDENTIAL_QUERY_KEYS.has(key.toLowerCase()));
  } catch {
    return true;
  }
}

/**
 * Markdown URL policy: links may use http(s), mailto, or same-origin relative
 * paths. Images are restricted to app-owned resources; raw data URLs and
 * remote tracking pixels are not rendered.
 */
export const safeMarkdownUrl: UrlTransform = (value, _key, node) => {
  const candidate = value.trim();
  if (!candidate || /[\u0000-\u001f\u007f]/.test(candidate) || containsCredentialQuery(candidate)) {
    return null;
  }

  const isImage = node.tagName === 'img';
  if (candidate.startsWith('/')) {
    if (candidate.startsWith('//')) return null;
    if (isImage && !candidate.startsWith('/api/') && !candidate.startsWith('/assets/')) return null;
    return candidate;
  }

  let protocol = '';
  try {
    protocol = new URL(candidate).protocol.toLowerCase();
  } catch {
    // Plain relative paths are valid links, but not valid image sources.
    return isImage ? null : defaultUrlTransform(candidate);
  }

  if (isImage) return null;
  if (!['http:', 'https:', 'mailto:'].includes(protocol)) return null;
  return defaultUrlTransform(candidate);
};

function filenameFromUrl(value: string): string {
  try {
    const url = new URL(value, 'http://localhost');
    return decodeURIComponent(url.pathname.split('/').filter(Boolean).pop() || 'download');
  } catch {
    return 'download';
  }
}

function MarkdownImage({ src, alt }: { src?: string; alt?: string }) {
  if (!src) return null;
  return (
    <AuthenticatedImage
      src={src}
      alt={alt || ''}
      className="md-img"
      pendingClassName="md-img-pending"
      loading="lazy"
      decoding="async"
    />
  );
}

function MarkdownLink({
  href,
  children,
  ...props
}: React.AnchorHTMLAttributes<HTMLAnchorElement>) {
  const [error, setError] = useState<string | null>(null);
  const authenticated = Boolean(href && apiPathFromBrowserUrl(href));
  return (
    <>
      <a
        {...props}
        href={href}
        target="_blank"
        rel="noopener noreferrer"
        className="md-link"
        onClick={authenticated && href ? (event) => {
          event.preventDefault();
          setError(null);
          void downloadAuthenticatedBrowserResource(href, filenameFromUrl(href)).catch((reason) => {
            setError(reason instanceof Error ? reason.message : String(reason));
          });
        } : undefined}
      >
        {children}
      </a>
      {error ? <span className="md-link-error" role="alert">{error}</span> : null}
    </>
  );
}

const markdownComponents: Components = {
  h1: (props) => <h1 {...props} className="md-h md-h1" />,
  h2: (props) => <h2 {...props} className="md-h md-h2" />,
  h3: (props) => <h3 {...props} className="md-h md-h3" />,
  h4: (props) => <h4 {...props} className="md-h md-h4" />,
  h5: (props) => <h5 {...props} className="md-h md-h5" />,
  h6: (props) => <h6 {...props} className="md-h md-h6" />,
  blockquote: (props) => <blockquote {...props} className="md-quote" />,
  table: (props) => <table {...props} className="md-table" />,
  ul: (props) => <ul {...props} className="md-list" />,
  ol: (props) => <ol {...props} className="md-list" />,
  hr: (props) => <hr {...props} className="md-hr" />,
  pre: (props) => <pre {...props} className="md-pre" />,
  code: ({ className, ...props }) => (
    <code {...props} className={[className, 'md-code'].filter(Boolean).join(' ')} />
  ),
  a: ({ href, children, ...props }) => <MarkdownLink {...props} href={href}>{children}</MarkdownLink>,
  img: ({ src, alt }) => <MarkdownImage src={typeof src === 'string' ? src : undefined} alt={alt} />,
};

interface MarkdownRendererProps {
  content: string;
  style?: React.CSSProperties;
  className?: string;
}

export const MarkdownRenderer = React.memo(function MarkdownRenderer({
  content,
  style,
  className,
}: MarkdownRendererProps) {
  const classes = useMemo(() => (className ? `md-content ${className}` : 'md-content'), [className]);
  return (
    <div className={classes} style={style}>
      <ReactMarkdown
        skipHtml
        remarkPlugins={[remarkGfm]}
        rehypePlugins={[rehypeSanitize]}
        urlTransform={safeMarkdownUrl}
        components={markdownComponents}
      >
        {content}
      </ReactMarkdown>
    </div>
  );
});

export default MarkdownRenderer;
