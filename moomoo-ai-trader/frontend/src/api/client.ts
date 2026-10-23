/**
 * 与本机后端通信（同源）。
 * - 所有请求带 Cookie（credentials: same-origin）。
 * - 非 GET 请求带 x-csrf-token（值取自 Cookie mt_csrf，双重提交）。
 * - 401：跳转登录页。
 * - 428：弹窗要求再次输入本机网页密码，验证成功后自动重试原请求一次。
 */

export class ApiError extends Error {
  status: number;
  detail: string;
  constructor(status: number, detail: string) {
    super(detail);
    this.status = status;
    this.detail = detail;
  }
}

interface Handlers {
  onUnauthorized?: () => void;
  requestReauth?: () => Promise<boolean>;
}

export const apiHandlers: Handlers = {};

export function getCookie(name: string): string {
  try {
    const parts = document.cookie ? document.cookie.split('; ') : [];
    for (const p of parts) {
      const idx = p.indexOf('=');
      if (idx > 0 && p.slice(0, idx) === name) return decodeURIComponent(p.slice(idx + 1));
    }
  } catch {
    /* ignore */
  }
  return '';
}

function detailOf(body: unknown, status: number): string {
  if (body && typeof body === 'object' && 'detail' in body) {
    const d = (body as { detail: unknown }).detail;
    if (typeof d === 'string') return d;
    if (Array.isArray(d)) {
      return (
        '请求内容不符合要求：' +
        d
          .map((x) => {
            if (x && typeof x === 'object') {
              const o = x as { loc?: unknown[]; msg?: string };
              const loc = Array.isArray(o.loc) ? o.loc.filter((l) => l !== 'body').join('.') : '';
              return `${loc ? loc + '：' : ''}${o.msg ?? ''}`;
            }
            return String(x);
          })
          .join('；')
      );
    }
  }
  if (status === 404) return '未找到（404）';
  if (status === 503) return '服务暂不可用（503）';
  if (status >= 500) return `后端出错（${status}），请查看后端日志`;
  return `请求失败（${status}）`;
}

let reauthInFlight: Promise<boolean> | null = null;

async function request<T>(method: string, path: string, body?: unknown, isRetry = false): Promise<T> {
  const headers: Record<string, string> = { Accept: 'application/json' };
  if (body !== undefined) headers['Content-Type'] = 'application/json';
  if (method !== 'GET') headers['x-csrf-token'] = getCookie('mt_csrf');
  let res: Response;
  try {
    res = await fetch(path, {
      method,
      credentials: 'same-origin',
      headers,
      body: body !== undefined ? JSON.stringify(body) : undefined,
    });
  } catch {
    throw new ApiError(0, '无法连接本机后端（网页服务可能已停止，请运行启动脚本）');
  }
  const ctype = res.headers.get('content-type') || '';
  let parsed: unknown = null;
  if (ctype.includes('application/json')) {
    try {
      parsed = await res.json();
    } catch {
      parsed = null;
    }
  } else {
    parsed = await res.text();
  }
  if (res.ok) return parsed as T;

  const detail = detailOf(parsed, res.status);
  if (res.status === 401 && (!path.startsWith('/api/auth/') || detail === '请先登录')) {
    apiHandlers.onUnauthorized?.();
    throw new ApiError(401, detail || '请先登录');
  }
  if (res.status === 428 && !isRetry && apiHandlers.requestReauth) {
    if (!reauthInFlight) {
      reauthInFlight = apiHandlers.requestReauth().finally(() => {
        reauthInFlight = null;
      });
    }
    const ok = await reauthInFlight;
    if (ok) return request<T>(method, path, body, true);
    throw new ApiError(428, '已取消再次验证，操作未执行');
  }
  throw new ApiError(res.status, detail);
}

export const api = {
  get: <T>(path: string) => request<T>('GET', path),
  post: <T>(path: string, body?: unknown) => request<T>('POST', path, body ?? {}),
  del: <T>(path: string) => request<T>('DELETE', path),
  /** 直接提交再次验证（不触发 428 循环）。 */
  reauth: (password: string) => request<{ ok: boolean }>('POST', '/api/auth/reauth', { password }, true),
};

export function errText(e: unknown): string {
  if (e instanceof ApiError) return e.detail;
  if (e instanceof Error) return e.message;
  return String(e);
}

export function qs(params: Record<string, string | number | boolean | null | undefined>): string {
  const u = new URLSearchParams();
  for (const [k, v] of Object.entries(params)) {
    if (v === null || v === undefined || v === '') continue;
    u.set(k, String(v));
  }
  const s = u.toString();
  return s ? '?' + s : '';
}
