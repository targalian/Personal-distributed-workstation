// Shared HTTP domain types.
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

