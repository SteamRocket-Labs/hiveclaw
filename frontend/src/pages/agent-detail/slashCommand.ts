export interface ParsedSlashCommand {
  name: string;
  args: Record<string, unknown>;
}

const COMMAND_NAME_RE = /^[A-Za-z0-9_-]+$/;

function parseNaturalArgs(name: string, argsText: string): Record<string, unknown> {
  if (name === 'goal' || name === 'goal_start' || name === 'advanced_plan') {
    return { objective: argsText };
  }
  if (name === 'load_skill') {
    return { name: argsText };
  }
  if (name === 'task' || name === 'task_create') {
    const delegatedWithColon = argsText.match(/^delegate\s+([^:]+):\s*([\s\S]+)$/i);
    if (delegatedWithColon) {
      return {
        kind: 'delegation',
        agent_name: delegatedWithColon[1].trim(),
        message: delegatedWithColon[2].trim(),
      };
    }
    const delegatedPlain = argsText.match(/^delegate\s+(\S+)\s+([\s\S]+)$/i);
    if (delegatedPlain) {
      return {
        kind: 'delegation',
        agent_name: delegatedPlain[1].trim(),
        message: delegatedPlain[2].trim(),
      };
    }
    return { subject: argsText };
  }
  if (name === 'task_output') {
    return { runtime_task_id: argsText };
  }
  if (name === 'task_stop') {
    return { runtime_task_id: argsText };
  }
  if (name === 'task_get' || name === 'task_update') {
    return { task_id: argsText };
  }
  if (name === 'resume') {
    return { query: argsText };
  }
  if (name === 'btw') {
    return { question: argsText };
  }
  if (name === 'turn_steer' || name === 'steer') {
    return { content: argsText };
  }
  if (name === 'interrupt') {
    return { run_id: argsText };
  }
  if (name === 'rename') {
    return { title: argsText };
  }
  if (name === 'tag') {
    return { tags: [argsText] };
  }
  return { input: argsText };
}

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

  if (!argsText.startsWith('{')) {
    return { name, args: parseNaturalArgs(name, argsText) };
  }

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
