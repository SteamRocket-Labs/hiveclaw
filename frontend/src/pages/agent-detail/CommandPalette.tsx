import React from 'react';
import { useTranslation } from 'react-i18next';
import { IconPlayerPlay, IconTerminal2, IconX } from '@tabler/icons-react';
import { useQuery } from '@tanstack/react-query';

import { ccParityApi, type CommandDefinition, type CommandIndexEntry, type ExecuteCommandResult } from '../../api/domains/ccParity';

export function filterCommandIndex(commands: CommandIndexEntry[] | undefined, query: string): CommandIndexEntry[] {
  const normalized = query.trim().toLowerCase();
  const list = commands ?? [];
  if (!normalized) return list;
  return list.filter((command) => {
    const haystack = [
      command.name,
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
  if (!command) return '{}';
  if (command.name === 'goal_start') return '{\n  "objective": ""\n}';
  if (command.name === 'advanced_plan') return '{\n  "objective": ""\n}';
  if (command.name === 'team_create') return '{\n  "name": "",\n  "members": []\n}';
  if (command.name === 'verify_plan') return '{\n  "plan_json": {},\n  "evidence_refs": []\n}';
  return '{}';
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
  const [argsText, setArgsText] = React.useState('{}');
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
      const parsed = argsText.trim() ? JSON.parse(argsText) : {};
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
    <div style={{ borderTop: '1px solid var(--border-subtle)', background: 'var(--bg-primary)' }}>
      <div style={{ display: 'flex', alignItems: 'center', gap: '8px', padding: '6px 12px' }}>
        <button
          type="button"
          onClick={() => setOpen((value) => !value)}
          disabled={!canLoad}
          title={t('agent.chat.commands.open', 'Commands')}
          style={{
            width: '36px',
            height: '32px',
            borderRadius: '6px',
            border: '1px solid var(--border-subtle)',
            background: open ? 'var(--bg-elevated)' : 'var(--bg-secondary)',
            color: 'var(--text-secondary)',
            display: 'inline-flex',
            alignItems: 'center',
            justifyContent: 'center',
            cursor: canLoad ? 'pointer' : 'not-allowed',
            flexShrink: 0,
          }}
        >
          <IconTerminal2 size={17} stroke={1.8} />
        </button>
        <div style={{ minWidth: 0, flex: 1 }}>
          <div style={{ fontSize: '11px', fontWeight: 700, color: 'var(--text-secondary)' }}>
            {t('agent.chat.commands.title', 'Commands')}
          </div>
          <div style={{ fontSize: '10px', color: 'var(--text-tertiary)', overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>
            {selected ? `${selected.name} · ${selected.category}` : t('agent.chat.commands.empty', 'No commands available')}
          </div>
        </div>
        {open && (
          <button
            type="button"
            onClick={() => setOpen(false)}
            title={t('common.close', 'Close')}
            style={{
              width: '28px',
              height: '28px',
              borderRadius: '6px',
              border: '1px solid var(--border-subtle)',
              background: 'transparent',
              color: 'var(--text-tertiary)',
              display: 'inline-flex',
              alignItems: 'center',
              justifyContent: 'center',
              cursor: 'pointer',
            }}
          >
            <IconX size={15} stroke={1.8} />
          </button>
        )}
      </div>
      {open && (
        <div style={{ padding: '0 12px 10px', display: 'grid', gap: '8px' }}>
          <input
            value={query}
            onChange={(event) => setQuery(event.target.value)}
            placeholder={t('agent.chat.commands.search', 'Search commands')}
            style={{
              height: '32px',
              borderRadius: '6px',
              border: '1px solid var(--border-subtle)',
              background: 'var(--bg-secondary)',
              color: 'var(--text-primary)',
              padding: '0 10px',
              fontSize: '12px',
            }}
          />
          <div style={{ display: 'flex', gap: '6px', overflowX: 'auto', paddingBottom: '2px' }}>
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
                style={{
                  border: command.name === selected?.name ? '1px solid var(--accent-primary)' : '1px solid var(--border-subtle)',
                  background: command.name === selected?.name ? 'rgba(16,185,129,0.10)' : 'var(--bg-secondary)',
                  color: command.name === selected?.name ? 'var(--accent-primary)' : 'var(--text-secondary)',
                  borderRadius: '6px',
                  padding: '5px 8px',
                  fontSize: '11px',
                  whiteSpace: 'nowrap',
                  cursor: 'pointer',
                }}
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
            style={{
              width: '100%',
              resize: 'vertical',
              minHeight: '84px',
              borderRadius: '6px',
              border: '1px solid var(--border-subtle)',
              background: 'var(--bg-secondary)',
              color: 'var(--text-primary)',
              padding: '8px 10px',
              fontSize: '12px',
              fontFamily: 'var(--font-mono, ui-monospace, SFMono-Regular, Menlo, monospace)',
              lineHeight: 1.5,
            }}
          />
          <div style={{ display: 'flex', alignItems: 'center', gap: '8px' }}>
            <button
              type="button"
              onClick={execute}
              disabled={!canExecute || busy}
              style={{
                height: '32px',
                borderRadius: '6px',
                border: '1px solid var(--border-subtle)',
                background: canExecute && !busy ? 'var(--accent-primary)' : 'var(--bg-secondary)',
                color: canExecute && !busy ? 'white' : 'var(--text-tertiary)',
                display: 'inline-flex',
                alignItems: 'center',
                justifyContent: 'center',
                gap: '6px',
                padding: '0 10px',
                fontSize: '12px',
                cursor: canExecute && !busy ? 'pointer' : 'not-allowed',
              }}
            >
              <IconPlayerPlay size={14} stroke={1.8} />
              {busy ? t('agent.chat.commands.running', 'Running') : t('agent.chat.commands.execute', 'Execute')}
            </button>
            <span style={{ fontSize: '10px', color: 'var(--text-tertiary)' }}>
              {schemaQuery.data?.execution_mode ?? selected?.execution_mode ?? 'metadata'}
            </span>
          </div>
          {commandsQuery.isLoading && (
            <div style={{ fontSize: '11px', color: 'var(--text-tertiary)' }}>
              {t('agent.chat.commands.loading', 'Loading commands...')}
            </div>
          )}
          {error && <pre style={{ margin: 0, color: 'var(--danger-text)', fontSize: '11px', whiteSpace: 'pre-wrap' }}>{error}</pre>}
          {resultText && (
            <pre
              style={{
                margin: 0,
                maxHeight: '160px',
                overflow: 'auto',
                borderRadius: '6px',
                border: '1px solid var(--border-subtle)',
                background: 'var(--bg-secondary)',
                color: 'var(--text-secondary)',
                padding: '8px',
                fontSize: '11px',
                whiteSpace: 'pre-wrap',
              }}
            >
              {resultText}
            </pre>
          )}
        </div>
      )}
    </div>
  );
}
