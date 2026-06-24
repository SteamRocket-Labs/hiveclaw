import React from 'react';
import { IconBolt, IconTerminal2 } from '@tabler/icons-react';
import { useQuery } from '@tanstack/react-query';
import { useTranslation } from 'react-i18next';

import { ccParityApi, type CommandIndexEntry } from '../../api/domains/ccParity';
import { defaultCommandArguments, filterCommandIndex } from './CommandPalette';
import { slashCommandQuery } from './slashCommand';

interface SlashCommandMenuProps {
  agentId?: string | null;
  sessionId?: string | null;
  inputValue: string;
  disabled?: boolean;
  onPickCommand: (command: CommandIndexEntry, template: string) => void;
}

function templateForCommand(command: CommandIndexEntry): string {
  const args = defaultCommandArguments(command).trim();
  if (!args || args === '{}') return `/${command.name}`;
  return `/${command.name} ${args}`;
}

export default function SlashCommandMenu({
  agentId,
  sessionId,
  inputValue,
  disabled = false,
  onPickCommand,
}: SlashCommandMenuProps) {
  const { t } = useTranslation();
  const query = slashCommandQuery(inputValue);
  const active = query !== null;

  const commandsQuery = useQuery({
    queryKey: ['slash-command-menu', agentId, sessionId],
    enabled: Boolean(agentId && sessionId && active && !disabled),
    queryFn: () => ccParityApi.listCommands(String(agentId), { surface: 'user', includeOptionalPacks: true }),
    staleTime: 60_000,
  });

  if (!active) return null;

  const filteredCommands = filterCommandIndex(commandsQuery.data, query ?? '');
  if (!commandsQuery.isLoading && filteredCommands.length === 0) return null;

  return (
    <div
      data-testid="slash-command-menu"
      style={{
        position: 'absolute',
        left: '52px',
        right: '52px',
        bottom: 'calc(100% + 8px)',
        zIndex: 12,
        border: '1px solid var(--border-subtle)',
        borderRadius: '10px',
        background: 'var(--bg-primary)',
        boxShadow: '0 12px 36px rgba(15, 23, 42, 0.12)',
        overflow: 'hidden',
      }}
    >
      <div
        style={{
          display: 'flex',
          alignItems: 'center',
          gap: '8px',
          padding: '8px 10px',
          borderBottom: '1px solid var(--border-subtle)',
          color: 'var(--text-secondary)',
          fontSize: '12px',
        }}
      >
        <IconTerminal2 size={16} stroke={1.8} />
        <strong style={{ color: 'var(--text-primary)' }}>{t('agent.chat.slashCommands.title', 'Commands')}</strong>
        <span style={{ color: 'var(--text-tertiary)' }}>
          {t('agent.chat.slashCommands.hint', 'Pick one or type JSON / natural args')}
        </span>
      </div>
      <div style={{ maxHeight: '360px', overflowY: 'auto', padding: '6px' }}>
        {commandsQuery.isLoading ? (
          <div style={{ padding: '10px 12px', color: 'var(--text-tertiary)', fontSize: '12px' }}>
            {t('common.loading', 'Loading')}
          </div>
        ) : (
          filteredCommands.map((command) => (
            <button
              key={command.name}
              type="button"
              onClick={() => onPickCommand(command, templateForCommand(command))}
              style={{
                width: '100%',
                display: 'grid',
                gridTemplateColumns: 'minmax(150px, 0.4fr) minmax(0, 1fr)',
                gap: '10px',
                alignItems: 'center',
                padding: '8px 10px',
                border: '0',
                borderRadius: '7px',
                background: 'transparent',
                color: 'var(--text-primary)',
                cursor: 'pointer',
                textAlign: 'left',
              }}
            >
              <span style={{ display: 'inline-flex', alignItems: 'center', gap: '7px', minWidth: 0 }}>
                <IconBolt size={14} stroke={1.8} color="var(--accent-primary)" />
                <span
                  style={{
                    overflow: 'hidden',
                    textOverflow: 'ellipsis',
                    whiteSpace: 'nowrap',
                    fontFamily: 'var(--font-mono, ui-monospace, SFMono-Regular, Menlo, monospace)',
                    fontSize: '12px',
                    fontWeight: 700,
                  }}
                >
                  /{command.name}
                </span>
              </span>
              <span style={{ minWidth: 0, display: 'grid', gap: '2px' }}>
                <span
                  style={{
                    overflow: 'hidden',
                    textOverflow: 'ellipsis',
                    whiteSpace: 'nowrap',
                    color: 'var(--text-secondary)',
                    fontSize: '12px',
                  }}
                >
                  {command.description || command.category}
                </span>
                <span style={{ color: 'var(--text-tertiary)', fontSize: '10px', textTransform: 'uppercase' }}>
                  {command.category}
                </span>
              </span>
            </button>
          ))
        )}
      </div>
    </div>
  );
}
