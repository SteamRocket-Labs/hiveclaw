import type React from 'react';
import {
  IconDatabaseExport,
  IconEdit,
  IconFilePlus,
  IconFileSearch,
  IconListSearch,
  IconShieldCheck,
} from '@tabler/icons-react';

type ToolIconInput = {
  name?: string | null;
  category?: string | null;
  icon?: React.ReactNode;
};

type ToolIconDefinition = {
  label: string;
  tone: 'office';
  marker: string;
  icon: React.ComponentType<{ size?: number; stroke?: number }>;
};

const OFFICE_ICONS: Record<string, ToolIconDefinition> = {
  office_document_apply: {
    label: 'Apply Office operations',
    tone: 'office',
    marker: 'office-apply',
    icon: IconEdit,
  },
  office_document_create: {
    label: 'Create Office document',
    tone: 'office',
    marker: 'office-create',
    icon: IconFilePlus,
  },
  office_document_dump: {
    label: 'Dump Office document',
    tone: 'office',
    marker: 'office-dump',
    icon: IconDatabaseExport,
  },
  office_document_query: {
    label: 'Query Office document',
    tone: 'office',
    marker: 'office-query',
    icon: IconListSearch,
  },
  office_document_validate: {
    label: 'Validate Office document',
    tone: 'office',
    marker: 'office-validate',
    icon: IconShieldCheck,
  },
  office_document_view: {
    label: 'View Office document',
    tone: 'office',
    marker: 'office-view',
    icon: IconFileSearch,
  },
};

const frameBaseStyle: React.CSSProperties = {
  width: '24px',
  minWidth: '24px',
  height: '24px',
  display: 'inline-flex',
  alignItems: 'center',
  justifyContent: 'center',
  borderRadius: '6px',
};

const toneStyle: Record<ToolIconDefinition['tone'], React.CSSProperties> = {
  office: {
    color: '#2563eb',
    background: 'rgba(37, 99, 235, 0.08)',
  },
};

function definitionFor(tool: ToolIconInput): ToolIconDefinition | null {
  const name = tool.name || '';
  if (name in OFFICE_ICONS) return OFFICE_ICONS[name];
  return null;
}

export default function ToolIcon({ tool }: { tool: ToolIconInput }) {
  const definition = definitionFor(tool);
  if (definition) {
    const Icon = definition.icon;
    return (
      <span
        aria-label={definition.label}
        data-tool-icon={definition.marker}
        role="img"
        style={{ ...frameBaseStyle, ...toneStyle[definition.tone] }}
        title={definition.label}
      >
        <Icon size={17} stroke={1.8} />
      </span>
    );
  }

  return (
    <span
      aria-hidden="true"
      style={{ ...frameBaseStyle, color: 'var(--text-secondary)', fontSize: '18px' }}
    >
      {tool.icon}
    </span>
  );
}
