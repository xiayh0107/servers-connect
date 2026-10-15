let csrf = '';
export function setCsrf(value: string) { csrf = value; }

export class ApiError extends Error {
  constructor(public status: number, public code: string, message: string, public existing?: Record<string, unknown>) {
    super(message); this.name = 'ApiError';
  }
}

function request(path: string, options: RequestInit = {}) {
  if (!path.startsWith('/') || path.startsWith('//')) throw new Error('API paths must be local');
  const headers = new Headers(options.headers);
  const method = (options.method || 'GET').toUpperCase();
  if (!['GET', 'HEAD'].includes(method)) headers.set('X-CSRF-Token', csrf);
  if (options.body && !(options.body instanceof FormData) && !headers.has('Content-Type')) headers.set('Content-Type', 'application/json');
  return fetch(`/api/v1${path}`, {...options, method, headers, credentials: 'same-origin', cache: 'no-store'});
}

async function checked(response: Response) {
  if (response.ok) return response;
  let body: Record<string, unknown> = {};
  try { body = await response.json(); } catch { /* Never include raw server output. */ }
  const detail = typeof body.detail === 'string' ? body.detail : undefined;
  const message = typeof body.message === 'string' ? body.message : detail || `Request failed (${response.status})`;
  const code = String(body.error_code || body.error || (response.status === 401 ? 'session_expired' : 'request_failed'));
  if (response.status === 401) window.dispatchEvent(new Event('ssm-session-expired'));
  throw new ApiError(response.status, code, message, body.existing as Record<string, unknown> | undefined);
}

export async function api<T>(path: string, options: RequestInit = {}): Promise<T> {
  const response = await checked(await request(path, options));
  return response.status === 204 ? undefined as T : await response.json() as T;
}

export function json(method: string, body?: unknown): RequestInit {
  return {method, ...(body === undefined ? {} : {body: JSON.stringify(body)})};
}

export async function download(path: string, body: unknown): Promise<{blob: Blob; filename: string}> {
  const response = await checked(await request(path, json('POST', body)));
  const disposition = response.headers.get('Content-Disposition') || '';
  const encoded = disposition.match(/filename\*=UTF-8''([^;]+)/i)?.[1];
  let filename = disposition.match(/filename="([^"]+)"/i)?.[1] || 'download';
  if (encoded) {try {filename = decodeURIComponent(encoded);} catch { /* keep safe fallback */ }}
  filename = filename.split(/[\\/]/).at(-1)?.replace(/[\x00-\x1f\x7f]/g, '') || 'download';
  return {blob: await response.blob(), filename};
}

export async function stream(path: string, body: unknown, onEvent: (event: Record<string, unknown>) => void, signal: AbortSignal) {
  const response = await checked(await request(path, {...json('POST', body), signal}));
  if (!response.body) throw new Error('Streaming response unavailable');
  const reader = response.body.getReader(), decoder = new TextDecoder();
  let pending = '', terminal = false;
  const consume = (line: string) => {
    if (!line.trim()) return;
    const event = JSON.parse(line) as Record<string, unknown>;
    if (event.type === 'result') terminal = true;
    onEvent(event);
  };
  try {
    while (true) {
      const {value, done} = await reader.read();
      pending += decoder.decode(value, {stream: !done});
      if (pending.length > 65536) throw new Error('Invalid diagnostic response');
      const lines = pending.split('\n'); pending = lines.pop() || ''; lines.forEach(consume);
      if (done) {consume(pending); break;}
    }
    if (!terminal) throw new Error('Connection ended before the test completed');
  } finally {await reader.cancel().catch(() => {}); reader.releaseLock();}
}

export const segment = encodeURIComponent;
