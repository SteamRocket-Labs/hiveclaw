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

const ATTACHMENT_MARKER_RE = /^\[📎\s*([^\]]+)\]\s*/;

export type AttachmentMarkerStripping = {
  /** The text after the leading `[📎 file]` markers, whitespace trimmed. */
  text: string;
  /** The last marker's file name, when a marker was present. */
  fileName: string;
};

/** Drop leading `[📎 file]` markers (the chip already shows the file) while
 * preserving the rest of the text. */
export function dropLeadingAttachmentMarkers(value: string): AttachmentMarkerStripping {
  let cleaned = String(value || '');
  let fileName = '';
  for (;;) {
    const marker = cleaned.match(ATTACHMENT_MARKER_RE);
    if (!marker) break;
    fileName = marker[1].trim();
    cleaned = cleaned.slice(marker[0].length);
  }
  return { text: cleaned.trim(), fileName };
}

/** Title projection of the marker strip: the member's sentence on one line,
 * with the file name standing in when no sentence survived (B2 r2). */
export function stripAttachmentMarkers(value: string): string {
  const { text, fileName } = dropLeadingAttachmentMarkers(value);
  const cleaned = text.replace(/\s+/g, ' ').trim();
  return cleaned || (fileName ? `📎 ${fileName}` : '');
}

const JSON_ESCAPES: Record<string, string> = {
  '"': '"', '\\': '\\', '/': '/', b: '\b', f: '\f', n: '\n', r: '\r', t: '\t',
};

/** Decode a possibly-truncated JSON string body: a cut escape (`\`, partial
 * `\uXX`) ends the value instead of corrupting it. */
function decodePartialJsonString(escaped: string): string {
  let out = '';
  for (let i = 0; i < escaped.length; i += 1) {
    const char = escaped[i];
    if (char !== '\\') {
      out += char;
      continue;
    }
    const next = escaped[i + 1];
    if (next === undefined) break;
    if (next === 'u') {
      const hex = escaped.slice(i + 2, i + 6);
      if (!/^[0-9a-fA-F]{4}$/.test(hex)) break;
      out += String.fromCodePoint(parseInt(hex, 16));
      i += 5;
      continue;
    }
    const mapped = JSON_ESCAPES[next];
    if (mapped === undefined) break;
    out += mapped;
    i += 1;
  }
  return out;
}

/** Truncated production prefixes never parse: read the first
 * `display_content` string straight out of the stored prefix, with the
 * attachment's `name` as the file-name fallback. */
function recoverFromJsonPrefix(trimmed: string): string | null {
  const display = trimmed.match(/"display_content":"((?:[^"\\]|\\.)*)/);
  if (display) {
    const cleaned = stripAttachmentMarkers(decodePartialJsonString(display[1]));
    if (cleaned) return cleaned;
  }
  const name = trimmed.match(/"name":"((?:[^"\\]|\\.)*)/);
  if (name) {
    const fileName = decodePartialJsonString(name[1]).trim();
    if (fileName) return `📎 ${fileName}`;
  }
  return null;
}

/**
 * Sessions titled before B2 stored the runtime's JSON encoding of the input
 * parts (`[{"display_content":…`) — and only its first 40 characters, so the
 * stored title is usually a truncated JSON prefix (keys sorted:
 * `display_content` first). Recovery is display-only: read the member's
 * display text out of the stored prefix — full or truncated — with the
 * attachment file name as fallback. Never return raw JSON.
 */
function recoverJsonArrayTitle(rawTitle: string): string | null {
  const trimmed = rawTitle.trim();
  if (!trimmed.startsWith('[{') && !trimmed.startsWith('{"')) return null;
  let parsed: unknown;
  try {
    parsed = JSON.parse(trimmed);
  } catch {
    return recoverFromJsonPrefix(trimmed);
  }
  const parts = Array.isArray(parsed) ? parsed : [parsed];
  for (const part of parts) {
    if (!part || typeof part !== 'object' || Array.isArray(part)) continue;
    const record = part as Record<string, unknown>;
    if (record.type && record.type !== 'text') continue;
    const display = typeof record.display_content === 'string'
      ? record.display_content
      : typeof record.text === 'string' && parts.length === 1 ? record.text : '';
    const cleaned = stripAttachmentMarkers(display);
    if (cleaned) return cleaned;
  }
  return recoverFromJsonPrefix(trimmed);
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
    // Current titles carry the composer's leading attachment markers; the
    // title is the member's sentence, with the file name as fallback (B2 r2).
    const recovered = recoverJsonArrayTitle(rawTitle) ?? stripAttachmentMarkers(rawTitle);
    return recovered || rawTitle;
  }

  let title = rawTitle;
  while (LEGACY_BRANCH_TITLE_SUFFIX.test(title)) {
    title = title.replace(LEGACY_BRANCH_TITLE_SUFFIX, '').trim();
  }
  title = recoverJsonArrayTitle(title) ?? stripAttachmentMarkers(title);
  return title || fallback;
}
