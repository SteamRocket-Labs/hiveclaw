import React from 'react';
import { useTranslation } from 'react-i18next';

import { chatApi } from '../../api/domains/chat';
import { triggerApi } from '../../api/domains/triggers';
import { StructuredToolResultBody } from './AgentChatSection';
import TeamMemorySummaryCard from './TeamMemorySummaryCard';
import { normalizeToolCallResult } from './toolResultEnvelope';

type AgentAwareSectionProps = {
  agentId: string;
  focusContent: string;
  awareTriggers: any[];
  activityLogs: any[];
  reflectionSessions: any[];
  reflectionMessages: Record<string, any[]>;
  expandedFocus: string | null;
  expandedReflection: string | null;
  showAllFocus: boolean;
  showCompletedFocus: boolean;
  showAllTriggers: boolean;
  reflectionPage: number;
  onSetExpandedFocus: React.Dispatch<React.SetStateAction<string | null>>;
  onSetExpandedReflection: React.Dispatch<React.SetStateAction<string | null>>;
  onSetReflectionMessages: React.Dispatch<React.SetStateAction<Record<string, any[]>>>;
  onSetShowAllFocus: React.Dispatch<React.SetStateAction<boolean>>;
  onSetShowCompletedFocus: React.Dispatch<React.SetStateAction<boolean>>;
  onSetShowAllTriggers: React.Dispatch<React.SetStateAction<boolean>>;
  onSetReflectionPage: React.Dispatch<React.SetStateAction<number>>;
  onRefetchTriggers: () => void | Promise<unknown>;
  onLoadReflectionMessages?: (sessionId: string) => Promise<any[] | void>;
};

const REFLECTIONS_PAGE_SIZE = 10;
const SECTION_PAGE_SIZE = 5;

export default function AgentAwareSection({
  agentId,
  focusContent,
  awareTriggers,
  activityLogs,
  reflectionSessions,
  reflectionMessages,
  expandedFocus,
  expandedReflection,
  showAllFocus,
  showCompletedFocus,
  showAllTriggers,
  reflectionPage,
  onSetExpandedFocus,
  onSetExpandedReflection,
  onSetReflectionMessages,
  onSetShowAllFocus,
  onSetShowCompletedFocus,
  onSetShowAllTriggers,
  onSetReflectionPage,
  onRefetchTriggers,
  onLoadReflectionMessages,
}: AgentAwareSectionProps) {
  const { t, i18n } = useTranslation();

  const lines = (focusContent || '').split('\n');
  const focusItems: { id: string; name: string; description: string; done: boolean; inProgress: boolean }[] = [];
  let currentItem: { id: string; name: string; description: string; done: boolean; inProgress: boolean } | null = null;

  for (const line of lines) {
    const match = line.match(/^\s*-\s*\[([ x/])\]\s*(.+)/i);
    if (match) {
      if (currentItem) focusItems.push(currentItem);
      const marker = match[1];
      const fullText = match[2].trim();
      const colonIndex = fullText.indexOf(':');
      const itemName = colonIndex > 0 ? fullText.substring(0, colonIndex).trim() : fullText;
      const itemDescription = colonIndex > 0 ? fullText.substring(colonIndex + 1).trim() : '';
      currentItem = {
        id: itemName,
        name: itemName,
        description: itemDescription,
        done: marker.toLowerCase() === 'x',
        inProgress: marker === '/',
      };
    } else if (currentItem && line.trim() && /^\s{2,}/.test(line)) {
      currentItem.description = currentItem.description ? `${currentItem.description} ${line.trim()}` : line.trim();
    }
  }
  if (currentItem) focusItems.push(currentItem);

  const triggerToHuman = (trigger: any): string => {
    if (trigger.type === 'cron' && trigger.config?.expr) {
      const expression = trigger.config.expr;
      const parts = expression.split(' ');
      if (parts.length >= 5) {
        const [minute, hour, , , dayOfWeek] = parts;
        const timeText = `${String(hour).padStart(2, '0')}:${String(minute).padStart(2, '0')}`;
        if (dayOfWeek === '*' && minute !== '*' && hour !== '*') return `Every day at ${timeText}`;
        if (dayOfWeek === '1-5' && minute !== '*' && hour !== '*') return `Weekdays at ${timeText}`;
        if (dayOfWeek === '0' || dayOfWeek === '7') return `Sundays at ${timeText}`;
        if (hour === '*' && minute === '0') {
          if (dayOfWeek === '1-5') return 'Every hour on weekdays';
          return 'Every hour';
        }
        if (hour === '*' && minute !== '*') return `Every hour at :${String(minute).padStart(2, '0')}`;
      }
      return `Cron: ${expression}`;
    }
    if (trigger.type === 'once' && trigger.config?.at) {
      try {
        return `Once at ${new Date(trigger.config.at).toLocaleString(undefined, { month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit' })}`;
      } catch {
        return `Once at ${trigger.config.at}`;
      }
    }
    if (trigger.type === 'interval' && trigger.config?.minutes) {
      const minutes = trigger.config.minutes;
      return minutes >= 60 ? `Every ${minutes / 60}h` : `Every ${minutes} min`;
    }
    if (trigger.type === 'poll') return `Poll: ${trigger.config?.url?.substring(0, 40) || 'URL'}`;
    if (trigger.type === 'on_message') return `On message from ${trigger.config?.from_agent_name || trigger.config?.from_user_name || 'unknown'}`;
    if (trigger.type === 'webhook') return `Webhook${trigger.config?.token ? ` (${trigger.config.token.substring(0, 6)}...)` : ''}`;
    return trigger.type;
  };

  const triggersByFocus: Record<string, any[]> = {};
  const standaloneTriggers: any[] = [];
  for (const trigger of awareTriggers) {
    if (trigger.focus_ref && focusItems.some((item) => item.name === trigger.focus_ref)) {
      if (!triggersByFocus[trigger.focus_ref]) triggersByFocus[trigger.focus_ref] = [];
      triggersByFocus[trigger.focus_ref].push(trigger);
    } else {
      standaloneTriggers.push(trigger);
    }
  }

  const triggerLogsByFocus: Record<string, any[]> = {};
  const triggerNameToFocus: Record<string, string> = {};
  for (const trigger of awareTriggers) {
    if (trigger.focus_ref) triggerNameToFocus[trigger.name] = trigger.focus_ref;
  }
  const triggerRelatedLogs = activityLogs.filter(
    (log: any) =>
      log.action_type === 'trigger_fired' ||
      log.action_type === 'trigger_created' ||
      log.action_type === 'trigger_updated' ||
      log.action_type === 'trigger_cancelled' ||
      log.summary?.includes('trigger'),
  );

  for (const log of triggerRelatedLogs) {
    let matched = false;
    for (const [triggerName, focusName] of Object.entries(triggerNameToFocus)) {
      if (log.summary?.includes(triggerName) || log.detail?.tool === triggerName) {
        if (!triggerLogsByFocus[focusName]) triggerLogsByFocus[focusName] = [];
        triggerLogsByFocus[focusName].push(log);
        matched = true;
        break;
      }
    }
    if (!matched) {
      if (!triggerLogsByFocus.__unmatched__) triggerLogsByFocus.__unmatched__ = [];
      triggerLogsByFocus.__unmatched__.push(log);
    }
  }

  const hasFocusItems = focusItems.length > 0;
  const hasStandalone = standaloneTriggers.length > 0;
  const activeFocusItems = focusItems.filter((item) => !item.done);
  const completedFocusItems = focusItems.filter((item) => item.done);
  const visibleActiveFocus = showAllFocus ? activeFocusItems : activeFocusItems.slice(0, SECTION_PAGE_SIZE);
  const hiddenActiveCount = activeFocusItems.length - visibleActiveFocus.length;

  const loadReflectionMessages = async (sessionId: string) => {
    if (reflectionMessages[sessionId]) return;
    try {
      const data = onLoadReflectionMessages ? await onLoadReflectionMessages(sessionId) : await chatApi.getSessionMessages(String(agentId), String(sessionId));
      if (data) {
        onSetReflectionMessages((previous) => ({ ...previous, [sessionId]: data }));
      }
    } catch {
      // Ignore reflection fetch failures in the UI shell.
    }
  };

  const renderFocusItem = (item: (typeof focusItems)[number]) => {
    const isExpanded = expandedFocus === item.id;
    const itemTriggers = triggersByFocus[item.name] || [];
    const itemLogs = triggerLogsByFocus[item.name] || [];
    const displayTitle = item.description || item.name;
    const displaySubtitle = item.description ? item.name : null;

    return (
      <div
        key={item.id}
        style={{
          borderRadius: '8px',
          border: '1px solid var(--border-subtle)',
          overflow: 'hidden',
          marginBottom: '6px',
          background: 'var(--bg-primary)',
        }}
      >
        <div
          onClick={() => onSetExpandedFocus(isExpanded ? null : item.id)}
          style={{
            padding: '12px 16px',
            display: 'flex',
            alignItems: 'flex-start',
            gap: '12px',
            cursor: 'pointer',
            transition: 'background 0.15s',
          }}
          onMouseEnter={(event) => (event.currentTarget.style.background = 'var(--bg-secondary)')}
          onMouseLeave={(event) => (event.currentTarget.style.background = 'transparent')}
        >
          <div
            style={{
              width: '8px',
              height: '8px',
              borderRadius: '50%',
              marginTop: '5px',
              flexShrink: 0,
              background: item.done ? 'var(--success, #10b981)' : item.inProgress ? 'var(--accent-primary)' : 'var(--border-subtle)',
            }}
          />
          <div style={{ flex: 1, minWidth: 0 }}>
            <div
              style={{
                fontSize: '13px',
                fontWeight: 500,
                lineHeight: '20px',
                textDecoration: item.done ? 'line-through' : 'none',
                color: item.done ? 'var(--text-tertiary)' : 'var(--text-primary)',
              }}
            >
              {displayTitle}
            </div>
            {displaySubtitle && (
              <div style={{ fontSize: '11px', color: 'var(--text-tertiary)', fontFamily: 'monospace', marginTop: '2px' }}>{displaySubtitle}</div>
            )}
          </div>
          {itemTriggers.length > 0 && (
            <span
              style={{
                fontSize: '11px',
                color: 'var(--text-tertiary)',
                padding: '2px 8px',
                borderRadius: '10px',
                background: 'var(--bg-secondary)',
                whiteSpace: 'nowrap',
              }}
            >
              {itemTriggers.length} trigger{itemTriggers.length > 1 ? 's' : ''}
            </span>
          )}
          <span
            style={{
              fontSize: '11px',
              color: 'var(--text-tertiary)',
              transform: isExpanded ? 'rotate(90deg)' : 'rotate(0deg)',
              transition: 'transform 0.15s',
              marginTop: '4px',
            }}
          >
            &#9654;
          </span>
        </div>

        {isExpanded && (
          <div style={{ padding: '0 16px 12px 36px', borderTop: '1px solid var(--border-subtle)' }}>
            {itemTriggers.length > 0 && (
              <div style={{ marginTop: '12px' }}>
                {itemTriggers.map((trigger: any) => (
                  <div
                    key={trigger.id}
                    style={{
                      display: 'flex',
                      alignItems: 'center',
                      gap: '10px',
                      padding: '8px 12px',
                      marginBottom: '4px',
                      borderRadius: '6px',
                      background: 'var(--bg-secondary)',
                      opacity: trigger.is_enabled ? 1 : 0.5,
                    }}
                  >
                    <div style={{ flex: 1 }}>
                      <div style={{ fontSize: '12px', fontWeight: 500, color: 'var(--text-primary)' }}>{triggerToHuman(trigger)}</div>
                      {trigger.reason && <div style={{ fontSize: '11px', color: 'var(--text-tertiary)', marginTop: '2px' }}>{trigger.reason}</div>}
                      <div style={{ fontSize: '10px', color: 'var(--text-tertiary)', marginTop: '2px', fontFamily: 'monospace' }}>
                        {trigger.type === 'cron' ? trigger.config?.expr : ''}{' '}
                      </div>
                    </div>
                    <span style={{ fontSize: '11px', color: 'var(--text-tertiary)', whiteSpace: 'nowrap' }}>{t('agent.aware.fired', { count: trigger.fire_count })}</span>
                    {!trigger.is_enabled && <span style={{ fontSize: '10px', color: 'var(--text-tertiary)' }}>{t('agent.aware.disabled')}</span>}
                    <div style={{ display: 'flex', gap: '4px' }}>
                      <button
                        className="btn btn-ghost"
                        style={{ padding: '2px 6px', fontSize: '11px' }}
                        onClick={async (event) => {
                          event.stopPropagation();
                          await triggerApi.update(agentId, trigger.id, { is_enabled: !trigger.is_enabled });
                          await onRefetchTriggers();
                        }}
                      >
                        {trigger.is_enabled ? t('agent.aware.disable') : t('agent.aware.enable')}
                      </button>
                      <button
                        className="btn btn-ghost"
                        style={{ padding: '2px 6px', fontSize: '11px', color: 'var(--error)' }}
                        onClick={async (event) => {
                          event.stopPropagation();
                          if (confirm(t('agent.aware.deleteTriggerConfirm', { name: trigger.name }))) {
                            await triggerApi.delete(agentId, trigger.id);
                            await onRefetchTriggers();
                          }
                        }}
                      >
                        {t('common.delete', 'Delete')}
                      </button>
                    </div>
                  </div>
                ))}
              </div>
            )}

            {itemLogs.length > 0 && (
              <div style={{ marginTop: '12px' }}>
                <div style={{ fontSize: '11px', fontWeight: 600, color: 'var(--text-tertiary)', marginBottom: '6px' }}>{t('agent.aware.reflections')}</div>
                <div style={{ display: 'flex', flexDirection: 'column', gap: '4px' }}>
                  {itemLogs.slice(0, 10).map((log: any) => (
                    <div
                      key={log.id}
                      style={{
                        padding: '6px 12px',
                        borderRadius: '6px',
                        background: 'var(--bg-secondary)',
                        borderLeft: '2px solid var(--border-subtle)',
                      }}
                    >
                      <div style={{ display: 'flex', alignItems: 'center', gap: '8px', marginBottom: '2px' }}>
                        <span
                          style={{
                            fontSize: '10px',
                            padding: '1px 5px',
                            borderRadius: '3px',
                            background: log.action_type === 'trigger_fired' ? 'rgba(var(--accent-primary-rgb, 99,102,241), 0.1)' : 'var(--bg-tertiary, #e5e7eb)',
                            color: log.action_type === 'trigger_fired' ? 'var(--accent-primary)' : 'var(--text-tertiary)',
                            fontWeight: 500,
                          }}
                        >
                          {log.action_type?.replace('trigger_', '')}
                        </span>
                        <span style={{ fontSize: '10px', color: 'var(--text-tertiary)' }}>
                          {new Date(log.created_at).toLocaleString(undefined, { month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit' })}
                        </span>
                      </div>
                      <div style={{ fontSize: '12px', color: 'var(--text-secondary)', whiteSpace: 'pre-wrap' }}>{log.summary}</div>
                    </div>
                  ))}
                </div>
              </div>
            )}

            {itemTriggers.length === 0 && itemLogs.length === 0 && <div style={{ padding: '12px 0', fontSize: '12px', color: 'var(--text-tertiary)' }}>{t('agent.aware.noTriggers')}</div>}
          </div>
        )}
      </div>
    );
  };

  const totalPages = Math.ceil(reflectionSessions.length / REFLECTIONS_PAGE_SIZE);
  const pageStart = reflectionPage * REFLECTIONS_PAGE_SIZE;
  const visibleSessions = reflectionSessions.slice(pageStart, pageStart + REFLECTIONS_PAGE_SIZE);

  return (
    <div style={{ display: 'flex', flexDirection: 'column', gap: '2px' }}>
      <TeamMemorySummaryCard agentId={agentId} section="aware" />

      <div className="card" style={{ marginBottom: '16px', padding: '16px' }}>
        <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: '12px' }}>
          <div>
            <h4 style={{ margin: 0, fontSize: '14px', fontWeight: 600 }}>{t('agent.aware.focus')}</h4>
            <span style={{ fontSize: '12px', color: 'var(--text-tertiary)' }}>{t('agent.aware.focusDesc')}</span>
          </div>
          {hasFocusItems && (
            <span style={{ fontSize: '11px', color: 'var(--text-tertiary)' }}>
              {activeFocusItems.length} active{completedFocusItems.length > 0 ? ` · ${completedFocusItems.length} done` : ''}
            </span>
          )}
        </div>

        {visibleActiveFocus.map(renderFocusItem)}

        {hiddenActiveCount > 0 && (
          <button className="btn btn-ghost" style={{ width: '100%', fontSize: '12px', color: 'var(--text-tertiary)', padding: '8px', marginTop: '4px' }} onClick={() => onSetShowAllFocus(true)}>
            {t('agent.aware.showMore', { count: hiddenActiveCount })}
          </button>
        )}
        {showAllFocus && activeFocusItems.length > SECTION_PAGE_SIZE && (
          <button
            className="btn btn-ghost"
            style={{ width: '100%', fontSize: '12px', color: 'var(--text-tertiary)', padding: '8px', marginTop: '4px' }}
            onClick={(event) => {
              onSetShowAllFocus(false);
              event.currentTarget.closest('.card')?.scrollIntoView({ behavior: 'smooth', block: 'start' });
            }}
          >
            {t('agent.aware.showLess')}
          </button>
        )}

        {completedFocusItems.length > 0 && (
          <>
            <button
              className="btn btn-ghost"
              style={{
                width: '100%',
                fontSize: '12px',
                color: 'var(--text-tertiary)',
                padding: '8px',
                marginTop: '8px',
                borderTop: '1px solid var(--border-subtle)',
                borderRadius: 0,
              }}
              onClick={() => onSetShowCompletedFocus(!showCompletedFocus)}
            >
              {showCompletedFocus ? t('agent.aware.hideCompleted') : t('agent.aware.showCompleted', { count: completedFocusItems.length })}
            </button>
            {showCompletedFocus && completedFocusItems.map(renderFocusItem)}
          </>
        )}

        {!hasFocusItems && (
          <div
            style={{
              padding: '24px',
              textAlign: 'center',
              color: 'var(--text-tertiary)',
              border: '1px dashed var(--border-subtle)',
              borderRadius: '8px',
            }}
          >
            {t('agent.aware.focusEmpty')}
          </div>
        )}
      </div>

      {hasStandalone && (
        <div className="card" style={{ marginBottom: '16px', padding: '16px' }}>
          <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: '12px' }}>
            <div>
              <h4 style={{ margin: 0, fontSize: '14px', fontWeight: 600 }}>{t('agent.aware.standaloneTriggers')}</h4>
            </div>
            <span style={{ fontSize: '11px', color: 'var(--text-tertiary)' }}>
              {standaloneTriggers.length} trigger{standaloneTriggers.length > 1 ? 's' : ''}
            </span>
          </div>
          <div style={{ display: 'flex', flexDirection: 'column', gap: '4px' }}>
            {[...standaloneTriggers]
              .sort((a: any, b: any) => (b.is_enabled ? 1 : 0) - (a.is_enabled ? 1 : 0))
              .slice(0, showAllTriggers ? undefined : SECTION_PAGE_SIZE)
              .map((trigger: any) => (
                <div
                  key={trigger.id}
                  style={{
                    padding: '10px 14px',
                    borderRadius: '8px',
                    border: '1px solid var(--border-subtle)',
                    display: 'flex',
                    alignItems: 'center',
                    gap: '10px',
                    opacity: trigger.is_enabled ? 1 : 0.5,
                    background: 'var(--bg-primary)',
                  }}
                >
                  <div style={{ flex: 1 }}>
                    <div style={{ fontSize: '13px', fontWeight: 500 }}>{triggerToHuman(trigger)}</div>
                    {trigger.reason && <div style={{ fontSize: '11px', color: 'var(--text-tertiary)', marginTop: '2px' }}>{trigger.reason}</div>}
                    <div style={{ fontSize: '10px', color: 'var(--text-tertiary)', fontFamily: 'monospace', marginTop: '2px' }}>
                      {trigger.name}
                      {trigger.type === 'cron' ? ` · ${trigger.config?.expr}` : ''}
                    </div>
                  </div>
                  <span style={{ fontSize: '11px', color: 'var(--text-tertiary)', whiteSpace: 'nowrap' }}>{t('agent.aware.fired', { count: trigger.fire_count })}</span>
                  {!trigger.is_enabled && <span style={{ fontSize: '10px', color: 'var(--text-tertiary)' }}>{t('agent.aware.disabled')}</span>}
                  <div style={{ display: 'flex', gap: '4px' }}>
                    <button
                      className="btn btn-ghost"
                      style={{ padding: '2px 6px', fontSize: '11px' }}
                      onClick={async () => {
                        await triggerApi.update(agentId, trigger.id, { is_enabled: !trigger.is_enabled });
                        await onRefetchTriggers();
                      }}
                    >
                      {trigger.is_enabled ? t('agent.aware.disable') : t('agent.aware.enable')}
                    </button>
                    <button
                      className="btn btn-ghost"
                      style={{ padding: '2px 6px', fontSize: '11px', color: 'var(--error)' }}
                      onClick={async () => {
                        if (confirm(t('agent.aware.deleteTriggerConfirm', { name: trigger.name }))) {
                          await triggerApi.delete(agentId, trigger.id);
                          await onRefetchTriggers();
                        }
                      }}
                    >
                      {t('common.delete', 'Delete')}
                    </button>
                  </div>
                </div>
              ))}
          </div>
          {standaloneTriggers.length > SECTION_PAGE_SIZE && (
            <button
              className="btn btn-ghost"
              style={{ width: '100%', fontSize: '12px', color: 'var(--text-tertiary)', padding: '8px', marginTop: '4px' }}
              onClick={(event) => {
                const collapse = showAllTriggers;
                onSetShowAllTriggers(!showAllTriggers);
                if (collapse) event.currentTarget.closest('.card')?.scrollIntoView({ behavior: 'smooth', block: 'start' });
              }}
            >
              {showAllTriggers
                ? i18n.language?.startsWith('zh')
                  ? '收起'
                  : 'Show less'
                : i18n.language?.startsWith('zh')
                  ? `显示更多 ${standaloneTriggers.length - SECTION_PAGE_SIZE} 项...`
                  : `Show ${standaloneTriggers.length - SECTION_PAGE_SIZE} more...`}
            </button>
          )}
        </div>
      )}

      {focusContent && (
        <details style={{ marginTop: '4px', marginBottom: '16px' }}>
          <summary style={{ fontSize: '11px', color: 'var(--text-tertiary)', cursor: 'pointer' }}>{t('agent.aware.viewRawMarkdown')}</summary>
          <pre style={{ fontSize: '11px', marginTop: '8px', padding: '12px', background: 'var(--bg-secondary)', borderRadius: '6px', whiteSpace: 'pre-wrap', maxHeight: '300px', overflow: 'auto' }}>{focusContent}</pre>
        </details>
      )}

      {reflectionSessions.length > 0 && (
        <div className="card" style={{ padding: '16px' }}>
          <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: '12px' }}>
            <div>
              <h4 style={{ margin: 0, fontSize: '14px', fontWeight: 600 }}>{t('agent.aware.reflections')}</h4>
              <span style={{ fontSize: '12px', color: 'var(--text-tertiary)' }}>{t('agent.aware.reflectionsDesc')}</span>
            </div>
            <span style={{ fontSize: '11px', color: 'var(--text-tertiary)' }}>
              {reflectionSessions.length} session{reflectionSessions.length > 1 ? 's' : ''}
            </span>
          </div>
          <div style={{ display: 'flex', flexDirection: 'column', gap: '4px' }}>
            {visibleSessions.map((session: any) => {
              const isExpanded = expandedReflection === session.id;
              const messages = reflectionMessages[session.id] || [];
              return (
                <div key={session.id} style={{ borderRadius: '8px', border: '1px solid var(--border-subtle)', overflow: 'hidden', background: 'var(--bg-primary)' }}>
                  <div
                    onClick={async () => {
                      if (isExpanded) {
                        onSetExpandedReflection(null);
                        return;
                      }
                      onSetExpandedReflection(session.id);
                      await loadReflectionMessages(session.id);
                    }}
                    style={{ padding: '10px 16px', display: 'flex', alignItems: 'center', gap: '10px', cursor: 'pointer', transition: 'background 0.15s' }}
                    onMouseEnter={(event) => (event.currentTarget.style.background = 'var(--bg-secondary)')}
                    onMouseLeave={(event) => (event.currentTarget.style.background = 'transparent')}
                  >
                    <div style={{ width: '6px', height: '6px', borderRadius: '50%', background: 'var(--accent-primary)', flexShrink: 0 }} />
                    <div style={{ flex: 1, minWidth: 0 }}>
                      <div style={{ fontSize: '12px', fontWeight: 500, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>
                        {(session.title || 'Trigger execution').replace(/^🤖\s*/, '')}
                      </div>
                      <div style={{ fontSize: '10px', color: 'var(--text-tertiary)', marginTop: '1px' }}>
                        {new Date(session.created_at).toLocaleString(undefined, { month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit' })}
                        {session.message_count > 0 && ` · ${session.message_count} msg`}
                      </div>
                    </div>
                    <span
                      style={{
                        fontSize: '11px',
                        color: 'var(--text-tertiary)',
                        transform: isExpanded ? 'rotate(90deg)' : 'rotate(0deg)',
                        transition: 'transform 0.15s',
                      }}
                    >
                      &#9654;
                    </span>
                  </div>
                  {isExpanded && (
                    <div style={{ padding: '0 16px 12px', borderTop: '1px solid var(--border-subtle)' }}>
                      {messages.length === 0 ? (
                        <div style={{ padding: '12px 0', fontSize: '12px', color: 'var(--text-tertiary)' }}>Loading...</div>
                      ) : (
                        <div style={{ display: 'flex', flexDirection: 'column', gap: '6px', marginTop: '8px' }}>
                          {messages.map((message: any, messageIndex: number) => {
                            if (message.role === 'tool_call') {
                              const toolName = message.toolName || (() => { try { return JSON.parse(message.content || '{}').name; } catch { return ''; } })() || 'tool';
                              const toolArgs = message.toolArgs || (() => { try { return JSON.parse(message.content || '{}').args; } catch { return {}; } })();
                              const toolResult = message.toolResult || '';
                              const normalizedResult = normalizeToolCallResult(toolName, toolResult);
                              const argsText = typeof toolArgs === 'string' ? toolArgs : JSON.stringify(toolArgs || {}, null, 2);
                              const resultText = normalizedResult.raw;
                              const hasDetail = argsText.length > 60 || resultText;
                              const ContainerTag = hasDetail ? 'details' : 'div';
                              const HeaderTag = hasDetail ? 'summary' : 'div';
                              return (
                                <ContainerTag key={messageIndex} style={{ borderRadius: '6px', background: 'var(--bg-secondary)', overflow: 'hidden' }}>
                                  <HeaderTag
                                    style={{
                                      padding: '5px 10px',
                                      fontSize: '11px',
                                      cursor: hasDetail ? 'pointer' : 'default',
                                      display: 'flex',
                                      alignItems: 'center',
                                      gap: '8px',
                                      listStyle: 'none',
                                      WebkitAppearance: 'none',
                                    } as any}
                                  >
                                    {hasDetail && <span style={{ fontSize: '8px', color: 'var(--text-tertiary)', flexShrink: 0 }}>&#9654;</span>}
                                    <span
                                      style={{
                                        fontWeight: 600,
                                        fontSize: '10px',
                                        color: 'var(--text-primary)',
                                        padding: '1px 6px',
                                        borderRadius: '3px',
                                        background: 'var(--bg-tertiary, rgba(0,0,0,0.06))',
                                        flexShrink: 0,
                                        fontFamily: 'monospace',
                                      }}
                                    >
                                      {toolName}
                                    </span>
                                    <span
                                      style={{
                                        color: 'var(--text-tertiary)',
                                        fontFamily: 'monospace',
                                        fontSize: '10px',
                                        overflow: 'hidden',
                                        textOverflow: 'ellipsis',
                                        whiteSpace: 'nowrap',
                                      }}
                                    >
                                      {argsText.replace(/\n/g, ' ').substring(0, 60)}
                                      {argsText.length > 60 ? '...' : ''}
                                    </span>
                                  </HeaderTag>
                                  {hasDetail && (
                                    <div
                                      style={{
                                        padding: '8px 10px',
                                        borderTop: '1px solid var(--border-subtle)',
                                        fontFamily: 'monospace',
                                        fontSize: '10px',
                                        lineHeight: 1.5,
                                        whiteSpace: 'pre-wrap',
                                        maxHeight: '200px',
                                        overflow: 'auto',
                                        color: 'var(--text-secondary)',
                                      }}
                                    >
                                      {argsText}
                                      {resultText && (
                                        <>
                                          <div style={{ borderTop: '1px dashed var(--border-subtle)', margin: '6px 0', opacity: 0.5 }} />
                                          {normalizedResult.toolMeta ? (
                                            <StructuredToolResultBody
                                              toolName={toolName}
                                              toolMeta={normalizedResult.toolMeta}
                                              toolResult={normalizedResult.displayResult}
                                              toolRawResult={normalizedResult.raw}
                                            />
                                          ) : (
                                            <>
                                              <span style={{ color: 'var(--text-tertiary)' }}>→ </span>
                                              {resultText.substring(0, 500)}
                                            </>
                                          )}
                                        </>
                                      )}
                                    </div>
                                  )}
                                </ContainerTag>
                              );
                            }

                            if (message.role === 'tool_result') {
                              const toolName = message.toolName || (() => { try { return JSON.parse(message.content || '{}').name; } catch { return ''; } })() || 'result';
                              const toolResult = message.toolResult || message.content || '';
                              const normalizedResult = normalizeToolCallResult(toolName, toolResult);
                              const resultText = normalizedResult.raw;
                              if (!resultText) return null;
                              return (
                                <details key={messageIndex} style={{ borderRadius: '6px', background: 'var(--bg-secondary)', overflow: 'hidden' }}>
                                  <summary
                                    style={{
                                      padding: '5px 10px',
                                      fontSize: '11px',
                                      cursor: 'pointer',
                                      display: 'flex',
                                      alignItems: 'center',
                                      gap: '8px',
                                      listStyle: 'none',
                                      WebkitAppearance: 'none',
                                    } as any}
                                  >
                                    <span style={{ fontSize: '8px', color: 'var(--text-tertiary)', flexShrink: 0 }}>&#9654;</span>
                                    <span
                                      style={{
                                        fontWeight: 600,
                                        fontSize: '10px',
                                        color: 'var(--text-primary)',
                                        padding: '1px 6px',
                                        borderRadius: '3px',
                                        background: 'var(--bg-tertiary, rgba(0,0,0,0.06))',
                                        flexShrink: 0,
                                        fontFamily: 'monospace',
                                      }}
                                    >
                                      {toolName}
                                    </span>
                                    <span
                                      style={{
                                        color: 'var(--text-tertiary)',
                                        fontFamily: 'monospace',
                                        fontSize: '10px',
                                        overflow: 'hidden',
                                        textOverflow: 'ellipsis',
                                        whiteSpace: 'nowrap',
                                      }}
                                    >
                                      → {resultText.replace(/\n/g, ' ').substring(0, 80)}
                                    </span>
                                  </summary>
                                  <div
                                    style={{
                                      padding: '8px 10px',
                                      borderTop: '1px solid var(--border-subtle)',
                                      fontFamily: 'monospace',
                                      fontSize: '10px',
                                      lineHeight: 1.5,
                                      whiteSpace: 'pre-wrap',
                                      maxHeight: '200px',
                                      overflow: 'auto',
                                      color: 'var(--text-secondary)',
                                    }}
                                  >
                                    {normalizedResult.toolMeta ? (
                                      <StructuredToolResultBody
                                        toolName={toolName}
                                        toolMeta={normalizedResult.toolMeta}
                                        toolResult={normalizedResult.displayResult}
                                        toolRawResult={normalizedResult.raw}
                                      />
                                    ) : (
                                      resultText.substring(0, 1000)
                                    )}
                                  </div>
                                </details>
                              );
                            }

                            if (message.role === 'assistant') {
                              return (
                                <div
                                  key={messageIndex}
                                  style={{
                                    padding: '8px 10px',
                                    borderRadius: '6px',
                                    background: 'var(--bg-secondary)',
                                    fontSize: '12px',
                                    color: 'var(--text-primary)',
                                    whiteSpace: 'pre-wrap',
                                    lineHeight: '1.5',
                                    maxHeight: '200px',
                                    overflow: 'auto',
                                  }}
                                >
                                  {message.content}
                                </div>
                              );
                            }

                            if (message.role === 'user') {
                              return (
                                <div
                                  key={messageIndex}
                                  style={{
                                    padding: '6px 10px',
                                    borderRadius: '6px',
                                    background: 'var(--bg-secondary)',
                                    borderLeft: '2px solid var(--border-subtle)',
                                    fontSize: '11px',
                                    color: 'var(--text-secondary)',
                                    whiteSpace: 'pre-wrap',
                                    maxHeight: '100px',
                                    overflow: 'auto',
                                  }}
                                >
                                  {(message.content || '').substring(0, 300)}
                                </div>
                              );
                            }

                            return null;
                          })}
                        </div>
                      )}
                    </div>
                  )}
                </div>
              );
            })}
          </div>
          {totalPages > 1 && (
            <div style={{ display: 'flex', justifyContent: 'center', alignItems: 'center', gap: '8px', marginTop: '12px', paddingTop: '8px', borderTop: '1px solid var(--border-subtle)' }}>
              <button
                className="btn btn-ghost"
                style={{ fontSize: '12px', padding: '4px 10px', opacity: reflectionPage === 0 ? 0.3 : 1 }}
                disabled={reflectionPage === 0}
                onClick={() => {
                  onSetReflectionPage((previous) => Math.max(0, previous - 1));
                  onSetExpandedReflection(null);
                }}
              >
                {i18n.language?.startsWith('zh') ? '上一页' : 'Prev'}
              </button>
              <span style={{ fontSize: '11px', color: 'var(--text-tertiary)', fontVariantNumeric: 'tabular-nums' }}>
                {reflectionPage + 1} / {totalPages}
              </span>
              <button
                className="btn btn-ghost"
                style={{ fontSize: '12px', padding: '4px 10px', opacity: reflectionPage >= totalPages - 1 ? 0.3 : 1 }}
                disabled={reflectionPage >= totalPages - 1}
                onClick={() => {
                  onSetReflectionPage((previous) => Math.min(totalPages - 1, previous + 1));
                  onSetExpandedReflection(null);
                }}
              >
                {i18n.language?.startsWith('zh') ? '下一页' : 'Next'}
              </button>
            </div>
          )}
        </div>
      )}
    </div>
  );
}
