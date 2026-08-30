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

// ── 工作站优化 (常驻自我优化工作流) ──────────────────────
// API 契约 (后端由 Codex 实现):
//   GET  /api/workstation-optimization/summary
//   GET  /api/workstation-optimization/items
//   POST /api/workstation-optimization/items  {source,title,description,priority}
//   POST /api/workstation-optimization/items/{id}/decision  {decision,reply}
// 后端未实现 (404/405/501) 时回退 localStorage mock,
// 与旧版 dashboard 共用同一键 (两处 UI 数据互通)。

export type OptSource = "boss" | "bottleneck" | "agent";
export type OptPriority = "low" | "normal" | "high" | "urgent";
export type OptStatus =
  | "candidate"
  | "waiting_boss"
  | "queued"
  | "running"
  | "completed"
  | "rejected";

export interface OptItem {
  id: string;
  source: OptSource;
  title: string;
  description: string;
  priority: OptPriority;
  status: OptStatus;
  created_at: number;
  updated_at: number;
  decision_reply: string;
  [k: string]: unknown;
}

export interface OptSummary {
  guardian_running: boolean;
  queue_count: number;
  waiting_boss_count: number;
  running_item: OptItem | null;
  recent_completed: OptItem | null;
}

const OPT_LS_KEY = "lan_mesh_opt_items_v1";

function optSeedItems(): OptItem[] {
  const now = Date.now() / 1000;
  return [
    {
      id: "opt-demo-1",
      source: "agent",
      title: "模型路由评分缺少 TTFB 反馈回路",
      description:
        "评分公式未纳入实际首 token 延迟, 长期可能选型偏慢模型。建议迭代加入 TTFB 反馈因子。",
      priority: "normal",
      status: "waiting_boss",
      created_at: now - 7200,
      updated_at: now - 7200,
      decision_reply: "",
    },
    {
      id: "opt-demo-2",
      source: "bottleneck",
      title: "任务并发 3+ 时 SQLite 写入阻塞",
      description:
        "开发任务运行中遇到: 多 PM 并发写进度时出现 database is locked, 建议改 WAL 模式或写入队列。",
      priority: "high",
      status: "waiting_boss",
      created_at: now - 3600,
      updated_at: now - 3600,
      decision_reply: "",
    },
    {
      id: "opt-demo-3",
      source: "boss",
      title: "仪表盘首屏加载超过 2 秒",
      description:
        "Boss 要求: 优化静态资源加载与缓存策略, 首屏目标 1s 内。",
      priority: "normal",
      status: "queued",
      created_at: now - 1800,
      updated_at: now - 1800,
      decision_reply: "",
    },
    {
      id: "opt-demo-4",
      source: "agent",
      title: "心跳 3s 间隔在 10+ 节点时 UDP 风暴",
      description: "已执行完成: 心跳合并为批量上报, 网络包数下降 40%。",
      priority: "low",
      status: "completed",
      created_at: now - 86400,
      updated_at: now - 80000,
      decision_reply: "",
    },
  ];
}

function optLoadLS(): OptItem[] | null {
  try {
    const raw = localStorage.getItem(OPT_LS_KEY);
    if (raw) {
      const arr = JSON.parse(raw);
      if (Array.isArray(arr)) return arr as OptItem[];
    }
  } catch {
    /* 损坏忽略 */
  }
  return null;
}

function optSaveLS(items: OptItem[]): void {
  try {
    localStorage.setItem(OPT_LS_KEY, JSON.stringify(items));
  } catch {
    /* 配额满忽略 */
  }
}

/** 优化模块专用请求 (与 apiFetch 同认证, 但保留原始 status 供 mock 判定)。 */
async function optRawFetch(
  url: string,
  opts: RequestInit = {},
): Promise<Response> {
  await ensureMeshToken();
  const headers: Record<string, string> = {
    ...(opts.headers as Record<string, string>),
  };
  const token = _userToken || _meshToken;
  if (token) headers["Authorization"] = `Bearer ${token}`;
  if (opts.body && !headers["Content-Type"]) {
    headers["Content-Type"] = "application/json";
  }
  return fetch(url, { ...opts, headers });
}

function optIsApiMissing(status: number): boolean {
  return status === 404 || status === 405 || status === 501;
}

/** 拉取优化项列表; 后端未实现时回退 localStorage mock。 */
export async function optGetItems(): Promise<{
  items: OptItem[];
  mock: boolean;
}> {
  try {
    const r = await optRawFetch("/api/workstation-optimization/items");
    if (r.ok) {
      const d = await r.json();
      const arr = Array.isArray(d) ? d : (d.items || []);
      if (Array.isArray(arr)) {
        optSaveLS(arr);
        return { items: arr as OptItem[], mock: false };
      }
    } else if (!optIsApiMissing(r.status)) {
      throw new Error(`HTTP ${r.status}`);
    }
  } catch {
    /* 后端未实现或网络异常 → mock */
  }
  const ls = optLoadLS();
  if (ls) return { items: ls, mock: true }; // 含空数组 (用户清空), 不重新播种
  const seed = optSeedItems();
  optSaveLS(seed);
  return { items: seed, mock: true };
}

/** Boss 决策; 后端未实现时本地 mock 决策 (写回 localStorage)。 */
export async function optDecide(
  id: string,
  decision: "approve" | "reject" | "clarify",
  reply: string,
): Promise<{ ok: boolean; item?: OptItem; error?: string; mock: boolean }> {
  try {
    const r = await optRawFetch(
      `/api/workstation-optimization/items/${encodeURIComponent(id)}/decision`,
      {
        method: "POST",
        body: JSON.stringify({ decision, reply }),
      },
    );
    if (r.ok) {
      const d = await r.json();
      return { ok: true, item: (d.item || d) as OptItem, mock: false };
    }
    if (!optIsApiMissing(r.status)) {
      return { ok: false, error: `HTTP ${r.status}`, mock: false };
    }
  } catch (e) {
    return { ok: false, error: String(e), mock: false };
  }
  const { items } = await optGetItems();
  const it = items.find((x) => x.id === id);
  if (!it) return { ok: false, error: "优化项不存在", mock: true };
  if (decision === "approve") it.status = "queued";
  else if (decision === "reject") it.status = "rejected";
  else it.decision_reply = reply;
  it.updated_at = Date.now() / 1000;
  optSaveLS(items);
  return { ok: true, item: it, mock: true };
}
