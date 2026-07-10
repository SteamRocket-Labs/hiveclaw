import React from 'react';
import { useTranslation } from 'react-i18next';
import { IconBraces, IconX } from '@tabler/icons-react';

import type { ThreadItem } from '../../api/domains/threadItems.generated';
import './ThreadItemWorkbench.css';

function InspectorRow({ label, value }: { label: string; value: React.ReactNode }) {
  if (value === null || value === undefined || value === '') return null;
  return (
    <div className="thread-item-inspector-row">
      <dt>{label}</dt>
      <dd>{value}</dd>
    </div>
  );
}

export function ThreadItemInspector({ item, onClose }: { item: ThreadItem | null; onClose?: () => void }) {
  const { t } = useTranslation();
  if (!item) {
    return (
      <aside className="thread-item-inspector is-empty" data-testid="thread-item-inspector" aria-label={t('sessionWorkbench.threadItem.inspector', 'Thread item inspector')}>
        <IconBraces size={18} aria-hidden="true" />
        <p>{t('sessionWorkbench.threadItem.inspectorEmpty', 'Select a timeline item to inspect its evidence and runtime links.')}</p>
      </aside>
    );
  }

  return (
    <aside className="thread-item-inspector" data-testid="thread-item-inspector" aria-label={t('sessionWorkbench.threadItem.inspector', 'Thread item inspector')}>
      <header>
        <div>
          <span>{t('sessionWorkbench.threadItem.inspector', 'Thread item inspector')}</span>
          <strong>{item.item_type}</strong>
        </div>
        {onClose && (
          <button type="button" onClick={onClose} aria-label={t('common.close', 'Close')}>
            <IconX size={15} aria-hidden="true" />
          </button>
        )}
      </header>
      <dl className="thread-item-inspector-rows">
        <InspectorRow label="schema" value={item.schema} />
        <InspectorRow label="id" value={item.id} />
        <InspectorRow label="status" value={item.item_status} />
        <InspectorRow label="sequence" value={item.sequence} />
        <InspectorRow label="thread" value={item.thread_id} />
        <InspectorRow label="turn" value={item.turn_id} />
        <InspectorRow label="run" value={item.run_id} />
        <InspectorRow label="causation" value={item.causation_id} />
        <InspectorRow label="correlation" value={item.correlation_id} />
        <InspectorRow label="visibility" value={item.visibility_scope} />
      </dl>
      {item.evidence_refs && item.evidence_refs.length > 0 && (
        <details className="thread-item-inspector-json">
          <summary>{t('sessionWorkbench.threadItem.evidenceRefs', 'Evidence references')}</summary>
          <pre>{JSON.stringify(item.evidence_refs, null, 2)}</pre>
        </details>
      )}
      <details className="thread-item-inspector-json">
        <summary>{t('sessionWorkbench.threadItem.typedData', 'Typed data')}</summary>
        <pre>{JSON.stringify(item.item_data, null, 2)}</pre>
      </details>
      <details className="thread-item-inspector-json">
        <summary>{t('sessionWorkbench.threadItem.evidenceMetadata', 'Evidence metadata')}</summary>
        <pre>{JSON.stringify(item.metadata || {}, null, 2)}</pre>
      </details>
    </aside>
  );
}
