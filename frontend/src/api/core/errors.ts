/**
 * Typed API error — replaces raw Error throws across the frontend.
 *
 * `data` carries the raw FastAPI `detail` object when the server returns
 * a structured error (e.g. register 409 → {field, code, suggest_login}).
 * Callers that know a specific endpoint's error shape can read it via
 * ApiError.data; others stay on `message` / `detail`.
 */
export class ApiError extends Error {
  status: number;
  detail: string;
  data: unknown;

  constructor(status: number, detail: string, data?: unknown) {
    super(detail);
    this.name = 'ApiError';
    this.status = status;
    this.detail = detail;
    this.data = data;
  }
}
