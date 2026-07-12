import React, { useEffect, useState } from 'react';

import { apiPathFromBrowserUrl, fetchAuthenticatedBrowserResource } from '../utils/authenticatedResource';

type AuthenticatedImageProps = React.ImgHTMLAttributes<HTMLImageElement> & {
  pendingClassName?: string;
};

/** Loads same-origin `/api/*` images with the normal Authorization headers. */
export default function AuthenticatedImage({
  src,
  alt = '',
  pendingClassName,
  ...props
}: AuthenticatedImageProps) {
  const [objectUrl, setObjectUrl] = useState<string | null>(null);
  const authenticated = Boolean(src && apiPathFromBrowserUrl(src));

  useEffect(() => {
    if (!src || !authenticated) return undefined;
    let active = true;
    let nextUrl: string | null = null;
    void fetchAuthenticatedBrowserResource(src).then((blob) => {
      if (!active) return;
      nextUrl = URL.createObjectURL(blob);
      setObjectUrl(nextUrl);
    }).catch(() => {
      if (active) setObjectUrl(null);
    });
    return () => {
      active = false;
      if (nextUrl) URL.revokeObjectURL(nextUrl);
    };
  }, [authenticated, src]);

  if (!src) return null;
  if (authenticated && !objectUrl) {
    return <span className={pendingClassName} role="status" aria-label={alt || 'Loading image'} />;
  }
  return <img {...props} src={authenticated ? objectUrl || undefined : src} alt={alt} />;
}
