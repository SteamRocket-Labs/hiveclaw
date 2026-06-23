export interface ParsedSlashCommand {
  name: string;
  args: Record<string, unknown>;
}

const COMMAND_NAME_RE = /^[A-Za-z0-9_-]+$/;

export function slashCommandQuery(input: string): string | null {
  const trimmedStart = input.trimStart();
  if (!trimmedStart.startsWith('/')) return null;
  return trimmedStart.slice(1).trimStart();
}

export function parseSlashCommandInput(input: string): ParsedSlashCommand | null {
  const query = slashCommandQuery(input);
  if (query == null || !query.trim()) return null;

  const commandMatch = query.match(/^([A-Za-z0-9_-]+)(?:\s+([\s\S]*))?$/);
  if (!commandMatch) return null;

  const name = commandMatch[1];
  if (!COMMAND_NAME_RE.test(name)) return null;

  const argsText = (commandMatch[2] || '').trim();
  if (!argsText) return { name, args: {} };

  try {
    const parsed = JSON.parse(argsText);
    if (!parsed || typeof parsed !== 'object' || Array.isArray(parsed)) {
      throw new Error('Slash command args must be a JSON object.');
    }
    return { name, args: parsed as Record<string, unknown> };
  } catch (error) {
    if (error instanceof Error && error.message === 'Slash command args must be a JSON object.') {
      throw error;
    }
    throw new Error('Invalid slash command JSON.');
  }
}
