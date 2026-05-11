import { get, post } from '../core';

export type OfficeKind = 'docx' | 'xlsx' | 'pptx';
export type OfficeEditorMode = 'edit' | 'view';

export interface OfficeDocumentCreateInput {
  path: string;
  kind: OfficeKind;
  template_path?: string;
}

export interface OfficeDocumentCreateResponse {
  status: 'ok';
  path: string;
  kind: OfficeKind;
  size: number;
}

export interface OfficeForceSaveResponse {
  status: 'ok';
  result: {
    error: number;
    key?: string;
  };
}

export interface OnlyOfficeEnabledConfig {
  enabled: true;
  documentServerUrl: string;
  config: Record<string, unknown>;
}

export interface OnlyOfficeDisabledConfig {
  enabled: false;
  reason: string;
  required_env?: string[];
}

export type OfficeEditorConfig = OnlyOfficeEnabledConfig | OnlyOfficeDisabledConfig;

export const officeApi = {
  createDocument: (agentId: string, data: OfficeDocumentCreateInput) =>
    post<OfficeDocumentCreateResponse>(`/agents/${agentId}/office/documents`, data),

  getEditorConfig: (agentId: string, path: string, mode: OfficeEditorMode = 'edit') =>
    get<OfficeEditorConfig>(
      `/agents/${agentId}/office/editor-config?path=${encodeURIComponent(path)}&mode=${encodeURIComponent(mode)}`,
    ),

  forceSave: (agentId: string, path: string) =>
    post<OfficeForceSaveResponse>(`/agents/${agentId}/office/force-save`, { path }),
};
