import { get, post, put, del } from '../core';

type ClawhubInstallInput = string | { slug: string };
type UrlImportInput = string | { url: string };
type SkillImportInput = string | { skill_id: string };
type SkillSettingsInput =
  | string
  | {
      github_token?: string | null;
      clawhub_key?: string | null;
    };

const clawhubBody = (input: ClawhubInstallInput) =>
  typeof input === 'string' ? { slug: input } : input;

const urlBody = (input: UrlImportInput) =>
  typeof input === 'string' ? { url: input } : input;

const skillImportBody = (input: SkillImportInput) =>
  typeof input === 'string' ? { skill_id: input } : input;

const githubTokenBody = (input: SkillSettingsInput) =>
  typeof input === 'string' ? { github_token: input } : input;

const clawhubKeyBody = (input: SkillSettingsInput) =>
  typeof input === 'string' ? { clawhub_key: input } : input;

export const skillApi = {
  list: () => get<any[]>('/skills/'),
  get: (id: string) => get<any>(`/skills/${id}`),
  create: (data: any) => post<any>('/skills/', data),
  update: (id: string, data: any) => put<any>(`/skills/${id}`, data),
  delete: (id: string) => del(`/skills/${id}`),
  browse: {
    list: (path: string) => get<any[]>(`/skills/browse/list?path=${encodeURIComponent(path)}`),
    read: (path: string) => get<{ content: string }>(`/skills/browse/read?path=${encodeURIComponent(path)}`),
    write: (path: string, content: string) => put<any>('/skills/browse/write', { path, content }),
    delete: (path: string) => del(`/skills/browse/delete?path=${encodeURIComponent(path)}`),
  },
  clawhub: {
    search: (q: string) => get<any[]>(`/skills/clawhub/search?q=${encodeURIComponent(q)}`),
    detail: (slug: string) => get<any>(`/skills/clawhub/detail/${slug}`),
    install: (data: ClawhubInstallInput) => post<any>('/skills/clawhub/install', clawhubBody(data)),
  },
  importFromUrl: (data: UrlImportInput) => post<any>('/skills/import-from-url', urlBody(data)),
  agentImport: {
    fromSkill: (agentId: string, data: SkillImportInput) =>
      post<any>(`/agents/${agentId}/files/import-skill`, skillImportBody(data)),
    fromClawhub: (agentId: string, data: ClawhubInstallInput) =>
      post<any>(`/agents/${agentId}/files/import-from-clawhub`, clawhubBody(data)),
    fromUrl: (agentId: string, data: UrlImportInput) =>
      post<any>(`/agents/${agentId}/files/import-from-url`, urlBody(data)),
  },
  importPreview: (data: UrlImportInput) => post<any>('/skills/import-from-url/preview', urlBody(data)),
  previewUrl: (data: UrlImportInput) => post<any>('/skills/import-from-url/preview', urlBody(data)),
  settings: {
    getToken: () => get<any>('/skills/settings/token'),
    setToken: (data: SkillSettingsInput) => put<any>('/skills/settings/token', githubTokenBody(data)),
    setClawhubKey: (data: SkillSettingsInput) => put<any>('/skills/settings/token', clawhubKeyBody(data)),
  },
};
