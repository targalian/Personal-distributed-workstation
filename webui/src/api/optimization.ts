// Optimization API fallback and persistence remain domain-owned.
import { rawFetch } from "./client";
import type { OptItem, OptSummary, OptPriority, OptStatus, OptSource } from "./types";

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
  return rawFetch(url, opts);
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
