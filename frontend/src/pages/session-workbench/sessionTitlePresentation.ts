const BRANCH_MODES = new Set([
  'branch',
  'fork',
  'rewind',
  'edit',
  'insert_before',
  'insert_after',
  'reply',
  'regenerate',
  'side_question',
]);

const LEGACY_BRANCH_TITLE_SUFFIX = /\s+\((?:branch|fork|edit|insert|reply|regenerate|btw|clear)\)\s*$/i;

/**
 * Sessions titled before B2 stored the runtime's JSON encoding of the input
 * parts (`[{"display_content":…`). Recovery is display-only: parse the stored
 * array and show the first text part's display text — the member's own words —
 * with no data migration.
 */
function recoverJsonArrayTitle(rawTitle: string): string | null {
  const trimmed = rawTitle.trim();
  if (!trimmed.startsWith('[{') && !trimmed.startsWith('{"')) return null;
  try {
    const parsed: unknown = JSON.parse(trimmed);
    const parts = Array.isArray(parsed) ? parsed : [parsed];
    for (const part of parts) {
      if (!part || typeof part !== 'object' || Array.isArray(part)) continue;
      const record = part as Record<string, unknown>;
      if (record.type && record.type !== 'text') continue;
      const display = typeof record.display_content === 'string'
        ? record.display_content
        : typeof record.text === 'string' && parts.length === 1 ? record.text : '';
      const cleaned = display.replace(/^\[📎\s*([^\]]+)\]\s*/, '$1 ').replace(/\s+/g, ' ').trim();
      if (cleaned) return cleaned;
    }
  } catch {
    return null;
  }
  return null;
}

function recordValue(value: unknown): Record<string, unknown> {
  return value && typeof value === 'object' && !Array.isArray(value)
    ? value as Record<string, unknown>
    : {};
}

export function sessionBranchMode(session: Record<string, unknown> | null | undefined): string {
  const source = recordValue(session);
  const branch = recordValue(source.branch);
  const metadata = recordValue(source.transcript_metadata_json || source.metadata_json || source.metadata);
  return String(
    source.branch_mode
      || branch.branch_mode
      || branch.mode
      || metadata.branch_mode
      || metadata.mode
      || '',
  ).trim().toLowerCase();
}

export function hasSessionBranchIdentity(session: Record<string, unknown> | null | undefined): boolean {
  const source = recordValue(session);
  if (BRANCH_MODES.has(sessionBranchMode(source))) return true;
  const id = source.id == null ? '' : String(source.id);
  const parentId = source.parent_session_id == null ? '' : String(source.parent_session_id);
  const rootId = source.root_session_id == null ? '' : String(source.root_session_id);
  return Boolean(parentId && parentId !== id) || Boolean(rootId && rootId !== id && parentId);
}

export function sessionTitleForUser(
  session: Record<string, unknown> | null | undefined,
  fallback: string,
): string {
  const source = recordValue(session);
  const rawTitle = String(source.title || source.name || '').trim() || fallback;
  if (!hasSessionBranchIdentity(source)) {
    const recovered = recoverJsonArrayTitle(rawTitle);
    return recovered || rawTitle;
  }

  let title = rawTitle;
  while (LEGACY_BRANCH_TITLE_SUFFIX.test(title)) {
    title = title.replace(LEGACY_BRANCH_TITLE_SUFFIX, '').trim();
  }
  title = recoverJsonArrayTitle(title) || title;
  return title || fallback;
}
