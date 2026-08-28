import { useEffect, useState } from "react";
import { apiFetch, HealthInfo } from "../api";

// iter-56: Station 总览页 — 健康状态轮询 (5s) + 组件/资源/负载三卡片
function fmtUptime(secs: number): string {
  const h = Math.floor(secs / 3600);
  const m = Math.floor((secs % 3600) / 60);
  const s = Math.floor(secs % 60);
  if (h > 0) return `${h}h ${m}m`;
  if (m > 0) return `${m}m ${s}s`;
  return `${s}s`;
}

export default function StationPage() {
  const [health, setHealth] = useState<HealthInfo | null>(null);
  const [error, setError] = useState("");

  useEffect(() => {
    let stop = false;
    const load = async () => {
      try {
        const h = await apiFetch<HealthInfo>("/health");
        if (!stop) {
          setHealth(h);
          setError("");
        }
      } catch (e) {
        if (!stop) setError(String(e));
      }
    };
    load();
    const t = setInterval(load, 5000);
    return () => {
      stop = true;
      clearInterval(t);
    };
  }, []);

  return (
    <div className="page">
      <div className="page-head">
        <h2>Station 总览</h2>
        {health && (
          <span className={`badge ${health.status === "healthy" ? "ok" : "warn"}`}>
            {health.status}
          </span>
        )}
      </div>
      {error && <div className="error-banner">健康检查失败: {error}</div>}
      {!health && !error && <div className="hint">加载中...</div>}
      {health && (
        <>
          <div className="cards">
            <div className="card">
              <h3>组件状态</h3>
              <ul className="kv">
                {Object.entries(health.components).map(([k, v]) => (
                  <li key={k}>
                    <span className="k">{k}</span>
                    <span className={`v st-${v}`}>{v}</span>
                  </li>
                ))}
              </ul>
            </div>
            <div className="card">
              <h3>资源</h3>
              <ul className="kv">
                <li>
                  <span className="k">内存</span>
                  <span className="v">{health.resources.memory_mb} MB</span>
                </li>
                <li>
                  <span className="k">CPU</span>
                  <span className="v">{health.resources.cpu_percent}%</span>
                </li>
                <li>
                  <span className="k">磁盘</span>
                  <span className="v">{health.resources.disk_percent}%</span>
                </li>
                <li>
                  <span className="k">线程</span>
                  <span className="v">{health.resources.threads}</span>
                </li>
                <li>
                  <span className="k">运行时长</span>
                  <span className="v">{fmtUptime(health.uptime_secs)}</span>
                </li>
              </ul>
            </div>
            <div className="card">
              <h3>工作负载</h3>
              <ul className="kv">
                <li>
                  <span className="k">活跃任务</span>
                  <span className="v">{health.workload.active_tasks}</span>
                </li>
                <li>
                  <span className="k">活跃 PM</span>
                  <span className="v">{health.workload.active_pms}</span>
                </li>
                <li>
                  <span className="k">WS 客户端</span>
                  <span className="v">{health.workload.ws_clients}</span>
                </li>
              </ul>
            </div>
          </div>
          <div className="hint">
            这是 React SPA 首版 (iter-56 补强#4 F5.1) — 任务与 DAG 编辑见顶部
            导航; 其余功能 Tab 请回旧版仪表盘。
          </div>
        </>
      )}
    </div>
  );
}
