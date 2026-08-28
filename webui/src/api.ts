// iter-56: SPA API 封装 — 认证模式与旧版 dashboard.html 一致
// (GET /api/station/auth-token → localStorage, 后续请求 Bearer 注入)
// iter-58 (F5.2): 用户个人 token (lan_mesh_user_token) 优先于
// mesh token; auth-token 端点回显角色 → 前端按角色禁用越权操作

let _meshToken: string = localStorage.getItem("lan_mesh_token") || "";
let _userToken: string = localStorage.getItem("lan_mesh_user_token") || "";
// 初始角色取缓存; 无缓存时置空 (未登录) — 服务端回显为准,
// 不回退 boss 避免多用户模式下未登录误显示为 boss
let _role: string = localStorage.getItem("lan_mesh_role") || "";
let _authPromise: Promise<void> | null = null;

export function getRole(): string {
  return _role;
}

/** 用户登录 (输入个人 token), 重置引导态并重新拉取角色。 */
export function loginUser(token: string): void {
  _userToken = token.trim();
  _authPromise = null;  // 重置: 下次 ensureMeshToken 重新拉取角色
  if (_userToken) {
    localStorage.setItem("lan_mesh_user_token", _userToken);
  } else {
    localStorage.removeItem("lan_mesh_user_token");
  }
}

/** 退出用户登录 (回到未登录态)。 */
export function logoutUser(): void {
  loginUser("");
}

/** 首次加载引导获取 mesh_token/角色 (认证启用时服务端返回)。

    共享 in-flight Promise: 并发调用方 await 同一请求, 完成后
    getRole() 读到最新回显值 (修复 iter-58 竞态 — 子组件先触发
    请求时父组件曾读到更新前的旧角色导致徽章定格)。
 */
export function ensureMeshToken(): Promise<void> {
  if (_authPromise) return _authPromise;
  _authPromise = (async () => {
    try {
      const headers: Record<string, string> = {};
      if (_userToken) headers["Authorization"] = `Bearer ${_userToken}`;
      const r = await fetch("/api/station/auth-token", { headers });
      const d = await r.json();
      if (d.auth_enabled && d.mesh_token) {
        _meshToken = d.mesh_token;
        localStorage.setItem("lan_mesh_token", _meshToken);
      }
      // iter-58: 服务端回显角色 (用户 token → 角色; 多用户模式未登录 → 空);
      // 空角色保留 (SPA 展示「未登录」), 不回退 boss 防误导
      _role = String(d.role ?? "");
      localStorage.setItem("lan_mesh_role", _role);
    } catch {
      /* 认证未启用或网络异常, 忽略 */
    }
  })();
  return _authPromise;
}

/** 统一 API 请求封装: 认证启用时自动携带 Bearer token。 */
export async function apiFetch<T = unknown>(
  url: string,
  opts: RequestInit = {},
): Promise<T> {
  await ensureMeshToken();
  const headers: Record<string, string> = {
    ...(opts.headers as Record<string, string>),
  };
  // iter-58: 用户 token 优先 (角色授权), 无用户登录时用 mesh token
  const token = _userToken || _meshToken;
  if (token) headers["Authorization"] = `Bearer ${token}`;
  if (opts.body && !headers["Content-Type"]) {
    headers["Content-Type"] = "application/json";
  }
  const r = await fetch(url, { ...opts, headers });
  if (!r.ok) {
    let detail = r.statusText;
    try {
      const body = await r.json();
      if (body.detail) detail = String(body.detail);
    } catch {
      /* 非 JSON 响应 */
    }
    throw new Error(`${r.status} ${detail}`);
  }
  return (await r.json()) as T;
}

// ── 类型定义 (与 lan_mesh 后端协议对齐) ──────────────────────

export interface HealthInfo {
  status: string;
  uptime_secs: number;
  components: Record<string, string>;
  resources: Record<string, number>;
  workload: Record<string, number>;
}

export interface TaskInfo {
  task_id?: string;
  id?: string;
  name: string;
  description?: string;
  status: string;
  priority?: number | string;
  pm_id?: string;
  created_at?: number;
  mode?: string;
  [k: string]: unknown;
}

export interface GraphNode {
  id: string;
  name: string;
  status: string;
  skill?: string;
  description?: string;
  x?: number;
  y?: number;
  [k: string]: unknown;
}

export interface GraphEdge {
  source: string;
  target: string;
  condition?: string;
  description?: string;
  [k: string]: unknown;
}

export interface TaskGraph {
  nodes: GraphNode[];
  edges: GraphEdge[];
}

export interface TaskListResp {
  tasks: TaskInfo[];
  total: number;
}
