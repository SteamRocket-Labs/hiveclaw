/**
 * XHR-based file upload with progress callback.
 * Cannot be done with fetch() — XHR is the only browser API with upload progress.
 */

const API_BASE = '/api';

export function uploadFileWithProgress(
  url: string,
  file: File,
  onProgress?: (percent: number) => void,
  extraFields?: Record<string, string>,
  timeoutMs: number = 120_000,
): { promise: Promise<any>; abort: () => void } {
  const xhr = new XMLHttpRequest();
  const promise = new Promise<any>((resolve, reject) => {
    const token = localStorage.getItem('token');
    const formData = new FormData();
    formData.append('file', file);
    if (extraFields) {
      for (const [k, v] of Object.entries(extraFields)) {
        formData.append(k, v);
      }
    }

    xhr.open('POST', `${API_BASE}${url}`);
    if (token) xhr.setRequestHeader('Authorization', `Bearer ${token}`);
    const tenantId = localStorage.getItem('current_tenant_id');
    if (tenantId) xhr.setRequestHeader('X-Tenant-Id', tenantId);
    xhr.timeout = timeoutMs;

    if (onProgress) {
      xhr.upload.onprogress = (e) => {
        if (e.lengthComputable) onProgress(Math.round((e.loaded / e.total) * 100));
      };
    }

    xhr.onload = () => {
      if (xhr.status >= 200 && xhr.status < 300) {
        try { resolve(JSON.parse(xhr.responseText)); }
        catch { resolve(xhr.responseText); }
      } else {
        reject(new Error(`Upload failed: ${xhr.status}`));
      }
    };
    xhr.onerror = () => reject(new Error('Upload network error'));
    xhr.ontimeout = () => reject(new Error('Upload timeout'));
    xhr.send(formData);
  });

  return { promise, abort: () => xhr.abort() };
}
