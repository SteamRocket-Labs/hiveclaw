import React from 'react';
import { useTranslation } from 'react-i18next';
import { IconPlayerPlay, IconTerminal2, IconX } from '@tabler/icons-react';
import { useQuery } from '@tanstack/react-query';

import { ccParityApi, type CommandDefinition, type CommandIndexEntry, type ExecuteCommandResult } from '../../api/domains/ccParity';
import { parseSlashCommandArgs } from './slashCommand';
import './CommandPalette.css';

export function filterCommandIndex(commands: CommandIndexEntry[] | undefined, query: string): CommandIndexEntry[] {
  const normalized = query.trim().toLowerCase();
  const list = commands ?? [];
  if (!normalized) return list;
  return list.filter((command) => {
    const haystack = [
      command.name,
      command.canonical_name,
      command.category,
      command.source,
      command.execution_mode,
      command.description,
      ...(command.aliases ?? []),
    ]
      .join(' ')
      .toLowerCase();
    return haystack.includes(normalized);
  });
}

export function defaultCommandArguments(command: CommandDefinition | CommandIndexEntry | null): string {
  void command;
  return '';
}

function parseCommandArgumentText(commandName: string, argsText: string): Record<string, unknown> {
  const trimmed = argsText.trim();
  if (!trimmed) return {};
  if (!trimmed.startsWith('{')) return parseSlashCommandArgs(commandName, trimmed);
  const parsed = JSON.parse(trimmed);
  if (!parsed || typeof parsed !== 'object' || Array.isArray(parsed)) {
    throw new Error('Command arguments must be a JSON object.');
  }
  return parsed as Record<string, unknown>;
}

interface CommandPaletteProps {
  agentId?: string | null;
  sessionId?: string | null;
  disabled?: boolean;
  initialOpen?: boolean;
  onCommandExecuted?: (payload: ExecuteCommandResult) => void;
}

export default function CommandPalette({
  agentId,
  sessionId,
  disabled = false,
  initialOpen = false,
  onCommandExecuted,
}: CommandPaletteProps) {
  const { t } = useTranslation();
  const [open, setOpen] = React.useState(initialOpen);
  const [query, setQuery] = React.useState('');
  const [selectedName, setSelectedName] = React.useState<string | null>(null);
  const [argsText, setArgsText] = React.useState('');
  const [busy, setBusy] = React.useState(false);
  const [error, setError] = React.useState<string | null>(null);
  const [resultText, setResultText] = React.useState<string | null>(null);
  const canLoad = Boolean(agentId);
  const canExecute = Boolean(agentId && selectedName && sessionId && !disabled);

  const commandsQuery = useQuery({
    queryKey: ['command-palette', agentId],
    enabled: canLoad,
    queryFn: () => ccParityApi.listCommands(String(agentId), { surface: 'user', includeOptionalPacks: true }),
    staleTime: 60_000,
  });

  const filteredCommands = filterCommandIndex(commandsQuery.data, query);
  const selected = filteredCommands.find((command) => command.name === selectedName) ?? filteredCommands[0] ?? null;

  React.useEffect(() => {
    if (!selected) return;
    if (selectedName !== selected.name) {
      setSelectedName(selected.name);
      setArgsText(defaultCommandArguments(selected));
    }
  }, [selected, selectedName]);

  const schemaQuery = useQuery({
    queryKey: ['command-schema', agentId, selectedName],
    enabled: Boolean(agentId && selectedName),
    queryFn: () => ccParityApi.getCommand(String(agentId), String(selectedName), { includeOptionalPacks: true }),
    staleTime: 60_000,
  });

  const execute = async () => {
    if (!agentId || !selectedName || !sessionId) return;
    setBusy(true);
    setError(null);
    setResultText(null);
    try {
      const parsed = parseCommandArgumentText(selectedName, argsText);
      const response = await ccParityApi.executeCommand(agentId, selectedName, {
        arguments: parsed,
        session_id: sessionId,
      });
      setResultText(JSON.stringify(response.result, null, 2));
      onCommandExecuted?.(response);
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="command-palette">
      <div className="command-palette-bar">
        <button
          type="button"
          onClick={() => setOpen((value) => !value)}
          disabled={!canLoad}
          title={t('agent.chat.commands.open', 'Commands')}
          className={`command-palette-toggle${open ? ' active' : ''}`}
        >
          <IconTerminal2 size={17} stroke={1.8} />
        </button>
        <div className="command-palette-label">
          <div className="command-palette-label-title">
            {t('agent.chat.commands.title', 'Commands')}
          </div>
          <div className="command-palette-label-sub">
            {selected ? `${selected.name} · ${selected.category}` : t('agent.chat.commands.empty', 'No commands available')}
          </div>
        </div>
        {open && (
          <button
            type="button"
            onClick={() => setOpen(false)}
            title={t('common.close', 'Close')}
            className="command-palette-close"
          >
            <IconX size={15} stroke={1.8} />
          </button>
        )}
      </div>
      {open && (
        <div className="command-palette-panel">
          <input
            value={query}
            onChange={(event) => setQuery(event.target.value)}
            placeholder={t('agent.chat.commands.search', 'Search commands')}
            className="command-palette-search"
          />
          <div className="command-palette-chips">
            {filteredCommands.map((command) => (
              <button
                key={command.name}
                type="button"
                onClick={() => {
                  setSelectedName(command.name);
                  setArgsText(defaultCommandArguments(command));
                  setError(null);
                  setResultText(null);
                }}
                className={`command-palette-chip${command.name === selected?.name ? ' active' : ''}`}
              >
                {command.name}
              </button>
            ))}
          </div>
          <textarea
            value={argsText}
            onChange={(event) => setArgsText(event.target.value)}
            spellCheck={false}
            rows={4}
            aria-label={t('agent.chat.commands.arguments', 'Command arguments')}
            placeholder={t('agent.chat.commands.argumentsPlaceholder', 'Type natural arguments, or paste JSON only when needed')}
            className="command-palette-textarea"
          />
          <div className="command-palette-exec-row">
            <button
              type="button"
              onClick={execute}
              disabled={!canExecute || busy}
              className="command-palette-exec"
            >
              <IconPlayerPlay size={14} stroke={1.8} />
              {busy ? t('agent.chat.commands.running', 'Running') : t('agent.chat.commands.execute', 'Execute')}
            </button>
            <span className="command-palette-mode">
              {schemaQuery.data?.execution_mode ?? selected?.execution_mode ?? 'metadata'}
            </span>
          </div>
          {commandsQuery.isLoading && (
            <div className="command-palette-hint">
              {t('agent.chat.commands.loading', 'Loading commands...')}
            </div>
          )}
          {error && <pre className="command-palette-error">{error}</pre>}
          {resultText && (
            <pre className="command-palette-result">
              {resultText}
            </pre>
          )}
        </div>
      )}
    </div>
  );
}
