import { del, get, post, put } from '../core';

export interface CustomApiConnector {
  id: string;
  name: string;
  display_name: string;
  description: string;
  enabled: boolean;
  is_default: boolean;
  parameters_schema: Record<string, unknown>;
  config: Record<string, unknown>;
  masked_secrets: Record<string, unknown>;
  config_schema: Record<string, unknown>;
}

export interface CustomApiConnectorPayload {
  connector_name: string;
  action_name: string;
  description?: string;
  base_url: string;
  method: string;
  path: string;
  auth_scheme: string;
  auth_location?: string;
  auth_name?: string | null;
  secret_value?: string | null;
  parameters_schema?: Record<string, unknown>;
  headers?: Record<string, unknown>;
  query?: Record<string, unknown>;
  body_template?: unknown;
  timeout_seconds?: number;
  enabled?: boolean;
  is_default?: boolean;
}

export const customApiConnectorsApi = {
  list: () => get<CustomApiConnector[]>('/enterprise/custom-api-connectors'),
  create: (data: CustomApiConnectorPayload) => post<CustomApiConnector>('/enterprise/custom-api-connectors', data),
  update: (id: string, data: Partial<CustomApiConnectorPayload>) =>
    put<CustomApiConnector>(`/enterprise/custom-api-connectors/${id}`, data),
  delete: (id: string) => del(`/enterprise/custom-api-connectors/${id}`),
  test: (id: string, argumentsPayload: Record<string, unknown>) =>
    post<{ ok: boolean; result: string }>(`/enterprise/custom-api-connectors/${id}/test`, {
      arguments: argumentsPayload,
    }),
};
