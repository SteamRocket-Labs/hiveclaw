export const TRANSLATION_CALLEE_RULES = [
  {
    source: 'src/pages/admin-companies/PlatformFeatureFlagsSection.tsx',
    callee: 'translate',
    reason: 'featureFlagAudienceSummary receives the react-i18next translator from its production component.',
  },
];

export const DYNAMIC_KEY_RULES = [
  {
    source: 'src/components/ChannelConfig.tsx',
    expression: '`${prefix}${i + 1}`',
    reason: 'The typed channel guide registry supplies the numbered setup-step keys.',
  },
  {
    source: 'src/components/ChannelConfig.tsx',
    expression: 'noteKey',
    reason: 'The typed channel guide registry supplies the note translation key.',
  },
  {
    source: 'src/components/ChannelConfig.tsx',
    expression: 'field.label',
    reason: 'The typed channel field registry owns label translation keys.',
  },
  {
    source: 'src/components/ChannelConfig.tsx',
    expression: 'field.placeholder',
    reason: 'The typed channel field registry owns placeholder translation keys.',
  },
  {
    source: 'src/components/ChannelConfig.tsx',
    expression: 'option.labelKey',
    reason: 'The typed channel option registry pairs every key with a reviewed fallback label.',
  },
  {
    source: 'src/components/ChannelConfig.tsx',
    expression: 'ch.nameKey',
    reason: 'The typed channel catalog pairs every channel name key with a reviewed fallback.',
  },
  {
    source: 'src/components/ChannelConfig.tsx',
    expression: 'currentConfigFeishuPlatformOption.labelKey',
    reason: 'The finite Feishu platform option registry owns this label key and fallback.',
  },
  {
    source: 'src/pages/AgentDetail.tsx',
    expression: 'area.labelKey',
    reason: 'The finite Agent detail area registry owns its label keys and fallbacks.',
  },
  {
    source: 'src/pages/ControlPlane.tsx',
    expression: '`controlPlane.card.${card.title}`',
    reason: 'Legacy control-plane cards intentionally retain their reviewed title as fallback.',
  },
  {
    source: 'src/pages/ControlPlane.tsx',
    expression: '`controlPlane.card.${card.title}.desc`',
    reason: 'Legacy control-plane cards intentionally retain their reviewed description as fallback.',
  },
  {
    source: 'src/pages/admin-companies/PlatformFeatureFlagsSection.tsx',
    expression: 'key',
    reason: 'The typed rollout-audience presenter supplies a reviewed fallback for every runtime key.',
  },
  {
    source: 'src/pages/agent-detail/AgentActionPolicyCard.tsx',
    expression: '`agent.settings.actionPolicy.actions.${row.id}.title`',
    reason: 'The exact action-policy row registry owns the finite action title keys and fallbacks.',
  },
  {
    source: 'src/pages/agent-detail/AgentActionPolicyCard.tsx',
    expression: '`agent.settings.actionPolicy.actions.${row.id}.description`',
    reason: 'The exact action-policy row registry owns the finite action description keys and fallbacks.',
  },
  {
    source: 'src/pages/agent-detail/AgentActionPolicyCard.tsx',
    expression: '`agent.settings.actionPolicy.zones.${option.value}`',
    reason: 'The typed policy-zone option registry owns every zone label and fallback.',
  },
  {
    source: 'src/pages/agent-detail/AgentActionPolicyCard.tsx',
    expression: '`agent.settings.actionPolicy.zones.${draft[row.id]}`',
    reason: 'The action-policy draft is constrained to the same finite typed zone registry.',
  },
  {
    source: 'src/pages/agent-detail/AgentExtensionsSection.tsx',
    expression: 'subview.labelKey',
    reason: 'The finite capability-subview registry owns label keys and fallbacks.',
  },
  {
    source: 'src/pages/agent-detail/AgentKnowledgeSection.tsx',
    expression: '`agent.knowledge.dreamRuntime.${status.runtime_status}`',
    reason: 'Unknown provider runtime states deliberately fall back to the typed status string.',
  },
  {
    source: 'src/pages/agent-detail/RunDisclosureBlock.tsx',
    expression: 'translated[0]',
    reason: 'TOOL_TITLE_KEYS is the finite reviewed translation-key and fallback registry.',
  },
  {
    source: 'src/pages/agent-detail/TeamMemorySummaryCard.tsx',
    expression: 'titleKey',
    reason: 'The section discriminator is finite and owns paired Team Memory title keys.',
  },
  {
    source: 'src/pages/agent-detail/TeamMemorySummaryCard.tsx',
    expression: 'descKey',
    reason: 'The section discriminator is finite and owns paired Team Memory description keys.',
  },
  {
    source: 'src/pages/agent-detail/TeamMemorySummaryCard.tsx',
    expression: 'countKey',
    reason: 'The section discriminator is finite and owns paired Team Memory count keys.',
  },
  {
    source: 'src/pages/agent-detail/TeamMemorySummaryCard.tsx',
    expression: 'searchPlaceholderKey',
    reason: 'The section discriminator is finite and owns paired Team Memory search keys.',
  },
  {
    source: 'src/pages/agent-detail/TeamMemorySummaryCard.tsx',
    expression: 'emptyKey',
    reason: 'The section discriminator is finite and owns paired Team Memory empty-state keys.',
  },
  {
    source: 'src/pages/agent-detail/TeamMemorySummaryCard.tsx',
    expression: 'detailKey',
    reason: 'The section discriminator is finite and owns paired Team Memory detail keys.',
  },
  {
    source: 'src/pages/agent-detail/TeamMemorySummaryCard.tsx',
    expression: 'updatedKey',
    reason: 'The section discriminator is finite and owns paired Team Memory timestamp keys.',
  },
  {
    source: 'src/pages/layout/AppSidebar.tsx',
    expression: 'item.labelKey',
    reason: 'The finite sidebar item registry owns every label key and fallback.',
  },
  {
    source: 'src/pages/session-workbench/SessionGoalPanel.tsx',
    expression: '`sessionGoal.status.${goal.status}`',
    reason: 'Unknown server goal states deliberately retain their typed status fallback.',
  },
  {
    source: 'src/pages/workspace/WorkspaceExtensionCatalogSection.tsx',
    expression: 'option.labelKey',
    reason: 'The finite extension-filter registry owns every option label key and fallback.',
  },
  {
    source: 'src/pages/workspace/WorkspaceExtensionsSection.tsx',
    expression: 'subview.labelKey',
    reason: 'The finite workspace capability-subview registry owns label keys and fallbacks.',
  },
  {
    source: 'src/surfaces/shared/SurfaceLayout.tsx',
    expression: 'headingKey',
    reason: 'Each shared surface supplies a reviewed heading key and fallback pair.',
  },
  {
    source: 'src/surfaces/shared/SurfaceLayout.tsx',
    expression: 'item.labelKey',
    reason: 'Each shared surface navigation item supplies a reviewed key and fallback pair.',
  },
  {
    source: 'src/pages/agent-detail/AgentWorkflowsSection.tsx',
    expression: 'DEFINITION_STATUS_KEYS[record.status] ?? record.status',
    reason: 'DEFINITION_STATUS_KEYS is the exhaustive Workflow definition-status translation map.',
  },
  {
    source: 'src/pages/agent-detail/AgentWorkflowsSection.tsx',
    expression: 'VISIBILITY_KEYS[record.visibility_scope] ?? record.visibility_scope',
    reason: 'VISIBILITY_KEYS is the exhaustive Workflow visibility translation map.',
  },
  {
    source: 'src/pages/agent-detail/AgentWorkflowsSection.tsx',
    expression: 'RUN_STATUS_KEYS[run.status] ?? run.status',
    reason: 'RUN_STATUS_KEYS is the exhaustive Workflow run-status translation map.',
  },
  {
    source: 'src/pages/agent-detail/SessionRuntimePanel.tsx',
    expression: 'labelKey',
    reason: 'The runtime wait-state presenter owns the finite label-key mapping.',
  },
];
