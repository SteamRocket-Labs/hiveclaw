import { useCallback, useEffect, useRef, useState } from 'react';
import { useTranslation } from 'react-i18next';

import { customApiConnectorsApi, type CustomApiConnector } from '../../api/domains/customApiConnectors';
import { requestAppConfirm, showAppToast } from '../../components/AppDialogs';
import type { WorkspaceToolsViewProps } from './workspaceToolsModel';

const INITIAL_FORM = {
  connector_name: '', action_name: '', description: '', base_url: '', method: 'GET', path: '/',
  auth_scheme: 'api_key', auth_location: 'header', auth_name: 'X-API-Key', secret_value: '',
  parameters_schema: '{\n  "type": "object",\n  "properties": {}\n}', headers: '{}', query: '{}',
  body_template: '', test_arguments: '{}', is_default: false,
};

function parseJsonField(value: string, fallback: unknown): unknown {
  const trimmed = value.trim();
  return trimmed ? JSON.parse(trimmed) : fallback;
}

export default function WorkspaceCustomApiView({ selectedTenantId }: WorkspaceToolsViewProps) {
  const { t } = useTranslation();
  const [connectors, setConnectors] = useState<CustomApiConnector[]>([]);
  const [loaded, setLoaded] = useState(false);
  const [busy, setBusy] = useState<string | null>(null);
  const [results, setResults] = useState<Record<string, string>>({});
  const [form, setForm] = useState(INITIAL_FORM);
  const requestVersion = useRef(0);

  const load = useCallback(async () => {
    const version = ++requestVersion.current;
    setLoaded(false);
    try {
      const data = await customApiConnectorsApi.list();
      if (version === requestVersion.current) setConnectors(data);
    } catch {
      if (version === requestVersion.current) setConnectors([]);
    } finally {
      if (version === requestVersion.current) setLoaded(true);
    }
  }, [selectedTenantId]);

  useEffect(() => {
    void load();
    return () => { requestVersion.current += 1; };
  }, [load]);

  const patchForm = (patch: Partial<typeof INITIAL_FORM>) => setForm((current) => ({ ...current, ...patch }));

  const createConnector = async () => {
    setBusy('create');
    try {
      await customApiConnectorsApi.create({
        connector_name: form.connector_name,
        action_name: form.action_name,
        description: form.description,
        base_url: form.base_url,
        method: form.method,
        path: form.path,
        auth_scheme: form.auth_scheme,
        auth_location: form.auth_location,
        auth_name: form.auth_name || null,
        secret_value: form.secret_value || null,
        parameters_schema: parseJsonField(form.parameters_schema, { type: 'object', properties: {} }) as Record<string, unknown>,
        headers: parseJsonField(form.headers, {}) as Record<string, unknown>,
        query: parseJsonField(form.query, {}) as Record<string, unknown>,
        body_template: parseJsonField(form.body_template, null),
        is_default: form.is_default,
        enabled: true,
      });
      patchForm({ action_name: '', description: '', path: '/', secret_value: '', body_template: '', test_arguments: '{}' });
      await load();
    } catch (error) {
      showAppToast(error instanceof Error ? error.message : String(error), 'error');
    } finally {
      setBusy(null);
    }
  };

  return (
    <div>
      <p className="ws-tools-hint">
        {t('enterprise.tools.customApiHint', 'Tenant-governed HTTP API actions. Credentials are stored server-side and are never exposed to agents.')}
      </p>
      <div className="card ws-tools-connector-form">
        <div className="ws-tools-grid-2">
          <input className="form-input" value={form.connector_name} onChange={(event) => patchForm({ connector_name: event.target.value })} placeholder={t('enterprise.tools.connectorName', 'Connector name')} />
          <input className="form-input" value={form.action_name} onChange={(event) => patchForm({ action_name: event.target.value })} placeholder={t('enterprise.tools.actionName', 'Action name')} />
          <input className="form-input" value={form.base_url} onChange={(event) => patchForm({ base_url: event.target.value })} placeholder="https://api.example.com" />
          <div className="ws-tools-grid-100">
            <select className="form-input" value={form.method} onChange={(event) => patchForm({ method: event.target.value })}>
              {['GET', 'POST', 'PUT', 'PATCH', 'DELETE'].map((method) => <option key={method}>{method}</option>)}
            </select>
            <input className="form-input" value={form.path} onChange={(event) => patchForm({ path: event.target.value })} placeholder="/v1/action/{id}" />
          </div>
          <select className="form-input" value={form.auth_scheme} onChange={(event) => patchForm({ auth_scheme: event.target.value })}>
            <option value="none">{t('enterprise.tools.authNone', 'No auth')}</option>
            <option value="api_key">{t('enterprise.tools.authApiKey', 'API key')}</option>
            <option value="bearer">{t('enterprise.tools.authBearer', 'Bearer token')}</option>
            <option value="basic">{t('enterprise.tools.authBasic', 'Basic auth')}</option>
          </select>
          <div className="ws-tools-grid-100">
            <select className="form-input" value={form.auth_location} onChange={(event) => patchForm({ auth_location: event.target.value })}>
              <option value="header">{t('enterprise.tools.header', 'Header')}</option>
              <option value="query">{t('enterprise.tools.query', 'Query')}</option>
            </select>
            <input className="form-input" value={form.auth_name} onChange={(event) => patchForm({ auth_name: event.target.value })} placeholder="X-API-Key" />
          </div>
          <input className="form-input" type="password" value={form.secret_value} onChange={(event) => patchForm({ secret_value: event.target.value })} placeholder={t('enterprise.tools.secretValue', 'Credential value')} />
          <input className="form-input" value={form.description} onChange={(event) => patchForm({ description: event.target.value })} placeholder={t('enterprise.tools.description', 'Description')} />
        </div>
        <div className="ws-tools-grid-2 ws-tools-mt-10">
          <textarea className="form-input" value={form.parameters_schema} onChange={(event) => patchForm({ parameters_schema: event.target.value })} rows={5} placeholder="parameters_schema JSON" />
          <textarea className="form-input" value={form.body_template} onChange={(event) => patchForm({ body_template: event.target.value })} rows={5} placeholder={t('enterprise.tools.bodyTemplateJson', 'Body template JSON, optional')} />
          <textarea className="form-input" value={form.headers} onChange={(event) => patchForm({ headers: event.target.value })} rows={3} placeholder={t('enterprise.tools.headersJson', 'Headers JSON')} />
          <textarea className="form-input" value={form.query} onChange={(event) => patchForm({ query: event.target.value })} rows={3} placeholder={t('enterprise.tools.queryJson', 'Query JSON')} />
        </div>
        <div className="ws-tools-row-between ws-tools-mt-10">
          <label className="ws-tools-check-label">
            <input type="checkbox" checked={form.is_default} onChange={(event) => patchForm({ is_default: event.target.checked })} />
            {t('enterprise.tools.enableForAllAgents', 'Enable for all agents')}
          </label>
          <button className="btn btn-primary" disabled={!form.connector_name.trim() || !form.action_name.trim() || !form.base_url.trim() || busy === 'create'} onClick={createConnector}>
            {t('enterprise.tools.createConnector', 'Create Connector')}
          </button>
        </div>
      </div>

      {!loaded ? <div className="ws-tools-empty">{t('common.loading', 'Loading...')}</div> : connectors.length === 0 ? (
        <div className="ws-tools-empty">{t('enterprise.tools.noCustomApis', 'No custom API connectors')}</div>
      ) : (
        <div className="ws-tools-list">
          {connectors.map((connector) => (
            <div key={connector.id} className="card ws-tools-card-pad">
              <div className="ws-tools-split">
                <div className="ws-tools-min0">
                  <div className="ws-tools-title-13">{connector.display_name}</div>
                  <div className="ws-tools-sub">{connector.name}</div>
                  {connector.description ? <div className="ws-tools-desc">{connector.description}</div> : null}
                </div>
                <div className="ws-tools-cell-shrink">
                  <span className={connector.enabled ? 'ws-tools-state-on' : 'ws-tools-state-off'}>{connector.enabled ? t('enterprise.tools.enabled', 'Enabled') : t('enterprise.tools.disabled', 'Disabled')}</span>
                  <button className="btn btn-ghost ws-tools-danger-text" disabled={busy === connector.id} onClick={async () => {
                    const confirmed = await requestAppConfirm({
                      title: t('enterprise.tools.deleteConnector', 'Delete connector'),
                      message: t('enterprise.tools.deleteConnectorConfirm', { name: connector.display_name, defaultValue: `Delete ${connector.display_name}?` }),
                      confirmLabel: t('common.delete', 'Delete'), danger: true,
                    });
                    if (!confirmed) return;
                    setBusy(connector.id);
                    try { await customApiConnectorsApi.delete(connector.id); await load(); } finally { setBusy(null); }
                  }}>{t('enterprise.tools.delete', 'Delete')}</button>
                </div>
              </div>
              <div className="ws-tools-row-8 ws-tools-mt-10">
                <input className="form-input" value={form.test_arguments} onChange={(event) => patchForm({ test_arguments: event.target.value })} placeholder={t('enterprise.tools.testArgumentsJson', 'Test arguments JSON')} />
                <button className="btn btn-ghost" disabled={busy === `test:${connector.id}`} onClick={async () => {
                  setBusy(`test:${connector.id}`);
                  try {
                    const result = await customApiConnectorsApi.test(connector.id, parseJsonField(form.test_arguments, {}) as Record<string, unknown>);
                    setResults((current) => ({ ...current, [connector.id]: result.result }));
                  } finally { setBusy(null); }
                }}>{t('enterprise.tools.testConnector', 'Test')}</button>
              </div>
              {results[connector.id] ? <pre className="ws-tools-pre">{results[connector.id]}</pre> : null}
            </div>
          ))}
        </div>
      )}
    </div>
  );
}
