import type { ReactNode } from 'react';
import { useTranslation } from 'react-i18next';

import type { LLMModel, LLMProviderAuthSession, LLMProviderConfig } from '../../api/domains/enterprise';

export interface WorkspaceProviderConfigForm {
  auth_mode: string;
  api_key: string;
  base_url: string;
  enabled: boolean;
}

interface WorkspaceLlmProvidersSectionProps {
  selectedProvider: string;
  providerConfigs: LLMProviderConfig[];
  models: LLMModel[];
  configForm: WorkspaceProviderConfigForm;
  authSession: LLMProviderAuthSession | null;
  legacyEditor?: ReactNode;
  onSelectProvider: (provider: string) => void;
  onConfigFormChange: (patch: Partial<WorkspaceProviderConfigForm>) => void;
  onSaveConfig: () => void;
  onVerifyConfig: () => void;
  onRefreshModels: () => void;
  onStartAuth: (mode: string) => void;
  onDisconnectAuth: () => void;
  onOpenManualCreate: () => void;
  onEditModel: (model: LLMModel) => void;
  onDeleteModel: (id: string) => void;
  onToggleModel: (id: string, enabled: boolean) => void;
  onSetDefaultModel?: (id: string) => void;
}

export default function WorkspaceLlmProvidersSection({
  selectedProvider,
  providerConfigs,
  models,
  configForm,
  authSession,
  legacyEditor = null,
  onSelectProvider,
  onConfigFormChange,
  onSaveConfig,
  onVerifyConfig,
  onRefreshModels,
  onStartAuth,
  onDisconnectAuth,
  onOpenManualCreate,
  onEditModel,
  onDeleteModel,
  onToggleModel,
  onSetDefaultModel,
}: WorkspaceLlmProvidersSectionProps) {
  const { t } = useTranslation();
  const selectedConfig = providerConfigs.find((provider) => provider.provider === selectedProvider) || providerConfigs[0];
  const providerModels = models.filter((model) => model.provider === selectedConfig?.provider);

  return (
    <div style={{ display: 'grid', gridTemplateColumns: '320px 1fr', gap: '20px' }}>
      <div className="card" style={{ padding: '0' }}>
        <div style={{ padding: '16px', borderBottom: '1px solid var(--border-default)', fontSize: '12px', color: 'var(--text-tertiary)', fontWeight: 700 }}>
          {t('enterprise.llm.providers', 'Providers')}
        </div>
        <div style={{ display: 'flex', flexDirection: 'column', padding: '8px' }}>
          {providerConfigs.map((provider) => (
            <button
              key={provider.provider}
              className="btn btn-ghost"
              style={{
                justifyContent: 'space-between',
                marginBottom: '8px',
                border: provider.provider === selectedConfig?.provider ? '1px solid var(--accent-primary)' : '1px solid transparent',
                background: provider.provider === selectedConfig?.provider ? 'var(--accent-subtle)' : 'transparent',
              }}
              onClick={() => onSelectProvider(provider.provider)}
            >
              <span>{provider.display_name}</span>
              <span style={{ fontSize: '11px', color: 'var(--text-tertiary)' }}>{provider.model_count}</span>
            </button>
          ))}
        </div>
      </div>

      <div style={{ display: 'flex', flexDirection: 'column', gap: '16px' }}>
        {selectedConfig ? (
          <div className="card">
            <div style={{ display: 'flex', justifyContent: 'space-between', gap: '16px', alignItems: 'flex-start', marginBottom: '16px' }}>
              <div>
                <h3 style={{ marginBottom: '6px' }}>{selectedConfig.display_name}</h3>
                <div style={{ color: 'var(--text-tertiary)', fontSize: '13px' }}>
                  {selectedConfig.protocol} · {selectedConfig.source}
                </div>
              </div>
              {selectedConfig.connected ? (
                <div style={{ display: 'flex', gap: '8px', alignItems: 'center' }}>
                  <span className="badge badge-success">{t('enterprise.llm.connected', 'Connected')}</span>
                  <button className="btn btn-secondary" onClick={onDisconnectAuth}>{t('enterprise.llm.disconnect', 'Disconnect')}</button>
                </div>
              ) : null}
            </div>

            <div style={{ display: 'grid', gridTemplateColumns: '1fr', gap: '12px' }}>
              {selectedConfig.supported_auth_modes?.includes('api_key') ? (
                <div className="form-group">
                  <label className="form-label">{t('enterprise.llm.apiKey', 'API Key')}</label>
                  <input
                    className="form-input"
                    type="password"
                    placeholder={selectedConfig.api_key_masked || t('enterprise.llm.apiKeyPlaceholder', 'Paste API key')}
                    value={configForm.api_key}
                    onChange={(event) => onConfigFormChange({ api_key: event.target.value })}
                  />
                  {selectedConfig.api_key_masked ? (
                    <div style={{ marginTop: '4px', fontSize: '11px', color: 'var(--text-tertiary)' }}>
                      {t('enterprise.llm.currentCredential', 'Current credential')}: {selectedConfig.api_key_masked}
                    </div>
                  ) : null}
                </div>
              ) : null}

              <div className="form-group">
                <label className="form-label">{t('enterprise.llm.baseUrl', 'Base URL')}</label>
                <input
                  className="form-input"
                  value={configForm.base_url}
                  placeholder={selectedConfig.default_base_url || ''}
                  onChange={(event) => onConfigFormChange({ base_url: event.target.value })}
                />
              </div>

              <div style={{ display: 'flex', gap: '8px', flexWrap: 'wrap' }}>
                <button className="btn btn-secondary" onClick={onVerifyConfig}>{t('enterprise.llm.verify', 'Verify')}</button>
                <button className="btn btn-primary" onClick={onSaveConfig}>{t('common.save', 'Save')}</button>
                {selectedConfig.supported_auth_modes?.includes('login_bridge') ? (
                  <button className="btn btn-secondary" onClick={() => onStartAuth('login_bridge')}>
                    {selectedConfig.managed_login_label || t('enterprise.llm.webLogin', 'Web Login')}
                  </button>
                ) : null}
                {selectedConfig.supported_auth_modes?.includes('oauth_web') ? (
                  <button className="btn btn-secondary" onClick={() => onStartAuth('oauth_web')}>
                    {t('enterprise.llm.webLogin', 'Web Login')}
                  </button>
                ) : null}
                {selectedConfig.supported_auth_modes?.includes('oauth_device') ? (
                  <button className="btn btn-secondary" onClick={() => onStartAuth('oauth_device')}>
                    {t('enterprise.llm.scanLogin', 'Scan QR Code')}
                  </button>
                ) : null}
              </div>

              {authSession ? (
                <div style={{ borderTop: '1px solid var(--border-default)', paddingTop: '12px' }}>
                  <div style={{ fontSize: '13px', marginBottom: '8px' }}>
                    {t('enterprise.llm.authStatus', 'Auth Status')}: {authSession.status}
                  </div>
                  {authSession.connect_url ? (
                    <a href={authSession.connect_url} target="_blank" rel="noreferrer" style={{ color: 'var(--accent-primary)', fontSize: '13px' }}>
                      {t('enterprise.llm.openLoginPage', 'Open login page')}
                    </a>
                  ) : null}
                  {authSession.qr_url ? (
                    <div style={{ marginTop: '12px' }}>
                      <div style={{ fontSize: '12px', marginBottom: '8px', color: 'var(--text-tertiary)' }}>
                        {t('enterprise.llm.scanLoginHint', 'Scan the QR code to finish login')}
                      </div>
                      <img src={authSession.qr_url} alt="provider-login-qr" style={{ width: '180px', height: '180px', borderRadius: '12px', border: '1px solid var(--border-default)' }} />
                      {authSession.user_code ? (
                        <div style={{ marginTop: '8px', fontFamily: 'var(--font-mono)', fontSize: '12px' }}>{authSession.user_code}</div>
                      ) : null}
                    </div>
                  ) : null}
                </div>
              ) : null}
            </div>
          </div>
        ) : null}

        <div className="card">
          <div style={{ display: 'flex', justifyContent: 'space-between', gap: '12px', alignItems: 'center', marginBottom: '16px' }}>
            <div>
              <h3 style={{ marginBottom: '4px' }}>{t('enterprise.llm.modelList', 'Model List')}</h3>
              <div style={{ color: 'var(--text-tertiary)', fontSize: '12px' }}>{providerModels.length}</div>
            </div>
            <div style={{ display: 'flex', gap: '8px' }}>
              <button className="btn btn-secondary" onClick={onRefreshModels}>{t('enterprise.llm.refresh', 'Refresh')}</button>
              <button className="btn btn-primary" onClick={onOpenManualCreate}>{t('enterprise.llm.addManualModel', 'Add Manual Model')}</button>
            </div>
          </div>

          {providerModels.length === 0 ? (
            <div style={{ color: 'var(--text-tertiary)', fontSize: '13px' }}>{t('enterprise.llm.emptyModels', 'No models yet')}</div>
          ) : (
            <div style={{ display: 'flex', flexDirection: 'column', gap: '12px' }}>
              {providerModels.map((model) => (
                <div key={model.id} className="card" style={{ border: '1px solid var(--border-default)', padding: '12px' }}>
                  <div style={{ display: 'flex', justifyContent: 'space-between', gap: '12px', alignItems: 'flex-start' }}>
                    <div>
                      <div style={{ display: 'flex', gap: '8px', alignItems: 'center', marginBottom: '6px' }}>
                        <strong>{model.label}</strong>
                        {(model.discovered_from_provider || model.provider_config_id) ? (
                          <span className="badge badge-info">{t('enterprise.llm.managed', 'Managed')}</span>
                        ) : null}
                        {model.is_default ? (
                          <span className="badge badge-success">{t('enterprise.llm.default', 'Default')}</span>
                        ) : null}
                      </div>
                      <div style={{ fontSize: '12px', color: 'var(--text-tertiary)' }}>{model.model}</div>
                      <div style={{ fontSize: '12px', color: 'var(--text-tertiary)', marginTop: '4px' }}>
                        temperature: {model.temperature ?? 'default'} · max_output: {model.max_output_tokens ?? 'default'} · max_input: {model.max_input_tokens ?? 'default'}
                      </div>
                    </div>
                    <div style={{ display: 'flex', gap: '8px', flexWrap: 'wrap' }}>
                      <button className="btn btn-secondary" onClick={() => onToggleModel(model.id, !model.enabled)}>
                        {model.enabled ? t('enterprise.llm.disable', 'Disable') : t('enterprise.llm.enable', 'Enable')}
                      </button>
                      {onSetDefaultModel && !model.is_default ? (
                        <button className="btn btn-secondary" onClick={() => onSetDefaultModel(model.id)}>
                          {t('enterprise.llm.setDefault', 'Set as Default')}
                        </button>
                      ) : null}
                      <button className="btn btn-secondary" onClick={() => onEditModel(model)}>{t('common.edit', 'Edit')}</button>
                      <button className="btn btn-danger" onClick={() => onDeleteModel(model.id)}>{t('common.delete', 'Delete')}</button>
                    </div>
                  </div>
                </div>
              ))}
            </div>
          )}

          {legacyEditor}
        </div>
      </div>
    </div>
  );
}
