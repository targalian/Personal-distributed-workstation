// iter-56: SPA API 封装 — 认证模式与旧版 dashboard.html 一致
// (GET /api/station/auth-token → localStorage, 后续请求 Bearer 注入)

let _meshToken: string = localStorage.getItem("lan_mesh_token") || "";
let _authChecked = false;

/** 首次加载引导获取 mesh_token (认证启用时服务端返回)。 */
export async function ensureMeshToken(): Promise<void> {
  if (_authChecked) return;
  _authChecked = true;
  try {
    const r = await fetch("/api/station/auth-token");
    const d = await r.json();
    if (d.auth_enabled && d.mesh_token) {
      _meshToken = d.mesh_token;
      localStorage.setItem("lan_mesh_token", _meshToken);
    }
  } catch {
    /* 认证未启用或网络异常, 忽略 */
  }
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
  if (_meshToken) headers["Authorization"] = `Bearer ${_meshToken}`;
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
