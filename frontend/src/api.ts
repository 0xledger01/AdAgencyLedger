export type Role = "CLIENT" | "SALES" | "RISK" | "FINANCE" | "OPS" | "ADMIN";

export interface Session {
  token: string;
  user_id: number;
  username: string;
  display_name: string;
  role: Role;
  role_label: string;
  client_id: number | null;
  is_primary: boolean;
  /** 后端 rbac.MATRIX 反查出来的权限码；按钮判定必须用它，不要硬编码角色数组 */
  perms?: string[];
  /** 权限码 → 中文权限名；界面只展示中文，判定仍用 perms */
  perm_labels?: Record<string, string>;
}

/**
 * 权限判定：与后端 require_perm 取自同一份矩阵。
 * 以前 6 个页面各写一份 ["FINANCE","ADMIN"].includes(role)，已经和后端对不上
 * （开户审批后端是 OPS，前端却给 RISK/FINANCE），所以统一收口到这里。
 */
export function hasPerm(s: Session | null | undefined, perm: string): boolean {
  return !!s?.perms?.includes(perm);
}

const KEY = "adsys.session";
const PORTAL_KEY = "adsys.portal";

/**
 * 两端的会话存储位置不同：
 * - 内部后台（desk）放 localStorage，员工 12 小时内免登录；
 * - 客户门户（portal）放 sessionStorage，关掉浏览器就失效。
 *   门户是外部客户入口，共用电脑上不能留下「下一个人打开地址就是我」的票。
 */
function storageOf(kind: "desk" | "portal"): Storage {
  return kind === "desk" ? window.localStorage : window.sessionStorage;
}

function keyOf(kind: "desk" | "portal"): string {
  return kind === "desk" ? KEY : PORTAL_KEY;
}

export function getSession(kind: "desk" | "portal"): Session | null {
  // 旧版本把门户票写在 localStorage 里；升级后读到就直接清掉，避免残票继续生效。
  if (kind === "portal") window.localStorage.removeItem(PORTAL_KEY);
  const raw = storageOf(kind).getItem(keyOf(kind));
  if (!raw) return null;
  try {
    return JSON.parse(raw) as Session;
  } catch {
    return null;
  }
}

export function setSession(kind: "desk" | "portal", s: Session | null) {
  const storage = storageOf(kind);
  const k = keyOf(kind);
  if (s) storage.setItem(k, JSON.stringify(s));
  else storage.removeItem(k);
  // 门户票只允许存在 sessionStorage；localStorage 里的旧票一律清掉。
  if (kind === "portal") window.localStorage.removeItem(PORTAL_KEY);
}

export class ApiError extends Error {
  status: number;
  code?: string;
  constructor(message: string, status: number, code?: string) {
    super(message);
    this.status = status;
    this.code = code;
  }
}

/** 统一解析后端错误体：detail 原文透出，401 顺手清登录态。request / upload 共用。 */
async function toApiError(kind: "desk" | "portal", resp: Response): Promise<ApiError> {
  const ctype = resp.headers.get("Content-Type") || "";
  let detail = `请求失败（${resp.status}）`;
  let code: string | undefined;
  if (ctype.includes("json")) {
    const body = await resp.json().catch(() => null);
    if (body?.detail) detail = typeof body.detail === "string" ? body.detail : JSON.stringify(body.detail);
    code = body?.code;
  }
  if (resp.status === 401) setSession(kind, null);
  return new ApiError(detail, resp.status, code);
}

async function request<T>(
  kind: "desk" | "portal",
  path: string,
  init: RequestInit = {}
): Promise<T> {
  const s = getSession(kind);
  const headers: Record<string, string> = { ...(init.headers as any) };
  if (init.body) headers["Content-Type"] = "application/json";
  if (s?.token) headers["Authorization"] = `Bearer ${s.token}`;

  const resp = await fetch(path, { ...init, headers });
  const ctype = resp.headers.get("Content-Type") || "";
  if (!resp.ok) throw await toApiError(kind, resp);
  if (ctype.includes("json")) return (await resp.json()) as T;
  return (await resp.blob()) as unknown as T;
}

export interface UploadResult {
  id: number;
  key: string;
  url: string;
  original_name: string;
  content_type: string;
  size: number;
  sha256: string;
  deduped: boolean;
}

/**
 * 上传文件到 /api/files。
 * 注意：绝对不要手动设 Content-Type —— 必须让浏览器自己带 multipart boundary。
 */
export async function upload(
  kind: "desk" | "portal",
  file: File,
  bizType: string
): Promise<UploadResult> {
  const s = getSession(kind);
  const fd = new FormData();
  fd.append("file", file);
  fd.append("biz_type", bizType);
  const headers: Record<string, string> = {};
  if (s?.token) headers["Authorization"] = `Bearer ${s.token}`;

  const resp = await fetch("/api/files", { method: "POST", headers, body: fd });
  if (!resp.ok) throw await toApiError(kind, resp);
  return (await resp.json()) as UploadResult;
}

export const api = {
  get: <T>(kind: "desk" | "portal", path: string) => request<T>(kind, path),
  post: <T>(kind: "desk" | "portal", path: string, body?: unknown) =>
    request<T>(kind, path, { method: "POST", body: body === undefined ? undefined : JSON.stringify(body) }),
  del: <T>(kind: "desk" | "portal", path: string) => request<T>(kind, path, { method: "DELETE" }),
};

/** multipart 上传（导入 CSV 等）：不手动设 Content-Type，boundary 由浏览器补。 */
export async function postForm<T>(
  kind: "desk" | "portal",
  path: string,
  fd: FormData
): Promise<T> {
  const s = getSession(kind);
  const headers: Record<string, string> = {};
  if (s?.token) headers["Authorization"] = `Bearer ${s.token}`;
  const resp = await fetch(path, { method: "POST", headers, body: fd });
  if (!resp.ok) throw await toApiError(kind, resp);
  return (await resp.json()) as T;
}

export interface UploadResult {
  id: number;
  key: string;
  url: string;
  original_name: string;
  content_type: string;
  size: number;
  sha256: string;
  deduped: boolean;
}

/**
 * 文件读取必须带 Bearer 头，而 `<img src>` / `window.open` 都带不了 ——
 * 直接填 url 会 401。统一 fetch 成 blob 再转 object URL，同一路径只拉一次。
 */
const fileUrlCache = new Map<string, string>();

export async function fileBlobUrl(kind: "desk" | "portal", path: string): Promise<string> {
  const cacheKey = `${kind}:${path}`;
  const hit = fileUrlCache.get(cacheKey);
  if (hit) return hit;
  const blob = await request<Blob>(kind, path);
  const url = URL.createObjectURL(blob);
  fileUrlCache.set(cacheKey, url);
  return url;
}

export async function download(kind: "desk" | "portal", path: string, filename: string) {
  const blob = await request<Blob>(kind, path);
  const url = URL.createObjectURL(blob);
  const a = document.createElement("a");
  a.href = url;
  a.download = filename;
  a.click();
  URL.revokeObjectURL(url);
}

export function qs(params: Record<string, string | number | undefined | null>): string {
  const p = new URLSearchParams();
  for (const [k, v] of Object.entries(params)) {
    if (v !== undefined && v !== null && v !== "") p.set(k, String(v));
  }
  const s = p.toString();
  return s ? `?${s}` : "";
}

export function money(v: string | number | null | undefined): string {
  if (v === null || v === undefined || v === "") return "—";
  const n = Number(v);
  if (Number.isNaN(n)) return String(v);
  return n.toLocaleString("zh-CN", { minimumFractionDigits: 2, maximumFractionDigits: 4 });
}

/**
 * 用 /api/auth/me 刷新会话（拿到最新 perms）。
 * token 过期时 401 → request 里已清 session，这里返回 null，由 App 回登录页。
 */
export async function refreshSession(kind: "desk" | "portal"): Promise<Session | null> {
  const cur = getSession(kind);
  if (!cur?.token) return null;
  try {
    const me = await request<Omit<Session, "token">>(kind, "/api/auth/me");
    const next: Session = {
      ...me,
      token: cur.token,
      perms: me.perms ?? [],
      perm_labels: me.perm_labels ?? {},
    };
    setSession(kind, next);
    return next;
  } catch {
    return null;
  }
}

/** 启动时校验一次登录态；有效则刷新 perms，无效返回 null。 */
export async function loadSession(kind: "desk" | "portal"): Promise<Session | null> {
  const cur = getSession(kind);
  if (!cur) return null;
  return (await refreshSession(kind)) ?? null;
}
