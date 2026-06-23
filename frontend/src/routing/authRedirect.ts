export const protectedLoginRedirect = (currentPath: string) => {
  return `/login?next=${encodeURIComponent(currentPath || '/')}`;
};

export const safePostLoginRedirect = (next: string | null | undefined) => {
  const target = (next || '').trim();
  if (!target.startsWith('/') || target.startsWith('//')) return '/';
  return target;
};
