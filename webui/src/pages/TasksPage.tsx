import { useCallback, useEffect, useState } from "react";
import { apiFetch, TaskInfo, TaskListResp } from "../api";

// iter-56: 任务列表页 — 状态筛选 + WS task_updated 自动刷新
const STATUS_FILTERS: { value: string; label: string }[] = [
  { value: "", label: "全部" },
  { value: "running", label: "运行中" },
  { value: "planning", label: "规划中" },
  { value: "monitoring", label: "监控中" },
  { value: "completed", label: "已完成" },
  { value: "failed", label: "失败" },
  { value: "paused", label: "已暂停" },
];

const STATUS_COLOR: Record<string, string> = {
  running: "#4caf50",
  planning: "#2196f3",
  monitoring: "#9c27b0",
  completed: "#8bc34a",
  failed: "#f44336",
  paused: "#ff9800",
  pending: "#9e9e9e",
  canceled: "#607d8b",
  interrupted: "#e91e63",
};

function taskIdOf(t: TaskInfo): string {
  return String(t.task_id ?? t.id ?? "");
}

export default function TasksPage() {
  const [tasks, setTasks] = useState<TaskInfo[]>([]);
  const [filter, setFilter] = useState("");
  const [error, setError] = useState("");
  const [loading, setLoading] = useState(false);

  const load = useCallback(async () => {
    setLoading(true);
    try {
      const q = filter ? `?status=${filter}` : "";
      const resp = await apiFetch<TaskListResp>(`/api/tasks${q}`);
      setTasks(resp.tasks);
      setError("");
    } catch (e) {
      setError(String(e));
    } finally {
      setLoading(false);
    }
  }, [filter]);

  useEffect(() => {
    load();
  }, [load]);

  // WS: task_updated 事件 → 刷新列表 (仅列表页, DAG 编辑页独立处理)
  useEffect(() => {
    const proto = location.protocol === "https:" ? "wss" : "ws";
    const ws = new WebSocket(`${proto}://${location.host}/ws`);
    let reloadTimer: ReturnType<typeof setTimeout> | null = null;
    ws.onmessage = (e) => {
      try {
        const msg = JSON.parse(e.data);
        if (msg.type === "task_updated" || msg.event === "task_updated") {
          // 事件可能密集: 300ms 防抖合并刷新
          if (reloadTimer) clearTimeout(reloadTimer);
          reloadTimer = setTimeout(() => load(), 300);
        }
      } catch {
        /* 非 JSON 消息忽略 */
      }
    };
    return () => {
      if (reloadTimer) clearTimeout(reloadTimer);
      ws.close();
    };
  }, [load]);

  return (
    <div className="page">
      <div className="page-head">
        <h2>任务列表</h2>
        <div className="toolbar">
          <select
            value={filter}
            onChange={(e) => setFilter(e.target.value)}
            className="select"
          >
            {STATUS_FILTERS.map((f) => (
              <option key={f.value} value={f.value}>
                {f.label}
              </option>
            ))}
          </select>
          <button className="btn" onClick={load} disabled={loading}>
            {loading ? "刷新中..." : "刷新"}
          </button>
        </div>
      </div>
      {error && <div className="error-banner">加载失败: {error}</div>}
      <table className="table">
        <thead>
          <tr>
            <th>任务 ID</th>
            <th>名称</th>
            <th>状态</th>
            <th>优先级</th>
            <th>操作</th>
          </tr>
        </thead>
        <tbody>
          {tasks.map((t) => {
            const tid = taskIdOf(t);
            return (
              <tr key={tid}>
                <td className="mono">{tid.slice(0, 12)}</td>
                <td>{t.name}</td>
                <td>
                  <span
                    className="status-chip"
                    style={{ background: STATUS_COLOR[t.status] ?? "#9e9e9e" }}
                  >
                    {t.status}
                  </span>
                </td>
                <td>{t.priority ?? "-"}</td>
                <td>
                  <a className="link" href={`#/dag/${encodeURIComponent(tid)}`}>
                    DAG
                  </a>
                </td>
              </tr>
            );
          })}
          {tasks.length === 0 && (
            <tr>
              <td colSpan={5} className="hint">
                暂无任务
              </td>
            </tr>
          )}
        </tbody>
      </table>
    </div>
  );
}
