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
  if (item.audience !== 'operator' || !item.operator_details) {
    return (
      <aside className="thread-item-inspector is-empty" data-testid="thread-item-inspector" aria-label={t('sessionWorkbench.threadItem.inspector', 'Thread item inspector')}>
        <IconBraces size={18} aria-hidden="true" />
        <p>{t('sessionWorkbench.threadItem.operatorOnly', 'Technical details are available only in Operator View.')}</p>
      </aside>
    );
  }
  const details = item.operator_details;
  const links = details.links || {};
  const link = (key: string) => typeof links[key] === 'string' ? String(links[key]) : null;
  const evidenceRefs = details.evidence_refs || [];

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
        <InspectorRow label="id" value={link('id')} />
        <InspectorRow label="status" value={item.item_status} />
        <InspectorRow label="sequence" value={item.sequence} />
        <InspectorRow label="thread" value={link('session_id')} />
        <InspectorRow label="turn" value={link('turn_id')} />
        <InspectorRow label="run" value={link('run_id')} />
        <InspectorRow label="causation" value={link('causation_id')} />
        <InspectorRow label="correlation" value={link('correlation_id')} />
        <InspectorRow label="visibility" value={item.visibility_scope} />
      </dl>
      {evidenceRefs.length > 0 && (
        <details className="thread-item-inspector-json">
          <summary>{t('sessionWorkbench.threadItem.evidenceRefs', 'Evidence references')}</summary>
          <pre>{JSON.stringify(evidenceRefs, null, 2)}</pre>
        </details>
      )}
      <details className="thread-item-inspector-json">
        <summary>{t('sessionWorkbench.threadItem.typedData', 'Typed data')}</summary>
        <pre>{JSON.stringify(details.item_data, null, 2)}</pre>
      </details>
      <details className="thread-item-inspector-json">
        <summary>{t('sessionWorkbench.threadItem.evidenceMetadata', 'Evidence metadata')}</summary>
        <pre>{JSON.stringify(details.metadata, null, 2)}</pre>
      </details>
    </aside>
  );
}
