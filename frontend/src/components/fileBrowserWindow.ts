export const FILE_LIST_PAGE_SIZE = 200;

export function visibleFileWindow<T>(items: readonly T[], visibleLimit: number): T[] {
  const boundedLimit = Number.isFinite(visibleLimit)
    ? Math.max(0, Math.floor(visibleLimit))
    : items.length;
  return items.slice(0, boundedLimit);
}
