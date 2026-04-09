import { get, post, put, del } from '../core';

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
    install: (data: any) => post<any>('/skills/clawhub/install', data),
  },
  importFromUrl: (data: any) => post<any>('/skills/import-from-url', data),
  agentImport: {
    fromSkill: (agentId: string, data: any) => post<any>(`/agents/${agentId}/files/import-skill`, data),
    fromClawhub: (agentId: string, data: any) => post<any>(`/agents/${agentId}/files/import-from-clawhub`, data),
    fromUrl: (agentId: string, data: any) => post<any>(`/agents/${agentId}/files/import-from-url`, data),
  },
  importPreview: (data: any) => post<any>('/skills/import-from-url/preview', data),
  previewUrl: (data: any) => post<any>('/skills/import-from-url/preview', data),
  settings: {
    getToken: () => get<any>('/skills/settings/token'),
    setToken: (data: any) => put<any>('/skills/settings/token', data),
    setClawhubKey: (data: any) => put<any>('/skills/settings/token', data),
  },
};
