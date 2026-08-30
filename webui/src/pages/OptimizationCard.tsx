import { useCallback, useEffect, useState } from "react";
import { OptItem, optDecide, optGetItems } from "../api";

// 工作站优化常驻状态卡 (iter-72): Station 首页展示优化队列概览 +
// 待 Boss 决策列表; 后端未实现时自动回退 localStorage mock
// (与旧版 dashboard 共用数据键, 两处 UI 语义一致)。

const SRC_LABEL: Record<string, string> = {
  boss: "Boss",
  bottleneck: "瓶颈",
  agent: "Agent",
};
const PRI_LABEL: Record<string, string> = {
  urgent: "紧急",
  high: "高",
  normal: "普通",
  low: "低",
};
const PRI_ORDER: Record<string, number> = { urgent: 0, high: 1, normal: 2, low: 3 };

function byPriority(a: OptItem, b: OptItem): number {
  return (
    (PRI_ORDER[a.priority] ?? 2) - (PRI_ORDER[b.priority] ?? 2) ||
    (a.created_at || 0) - (b.created_at || 0)
  );
}

function OptBadges({ item }: { item: OptItem }) {
  return (
    <span className="opt-badges">
      <span className={`opt-badge opt-src-${item.source}`}>
        {SRC_LABEL[item.source] || item.source}
      </span>
      <span className={`opt-badge opt-pri-${item.priority}`}>
        {PRI_LABEL[item.priority] || item.priority}
      </span>
    </span>
  );
}

export default function OptimizationCard() {
  const [items, setItems] = useState<OptItem[] | null>(null);
  const [mock, setMock] = useState(false);
  const [error, setError] = useState("");
  const [busyId, setBusyId] = useState("");

  const load = useCallback(async () => {
    try {
      const res = await optGetItems();
      setItems(res.items);
      setMock(res.mock);
      setError("");
    } catch (e) {
      setError(String(e));
    }
  }, []);

  useEffect(() => {
    load();
    const t = setInterval(load, 10000); // 10s 轮询 (dashboard mock 守护会推进状态)
    return () => clearInterval(t);
  }, [load]);

  const decide = async (id: string, decision: "approve" | "reject") => {
    setBusyId(id);
    try {
      const res = await optDecide(id, decision, "");
      if (!res.ok) setError(res.error || "决策失败");
      await load();
    } finally {
      setBusyId("");
    }
  };

  const waiting = (items || []).filter((x) => x.status === "waiting_boss");
  const queued = (items || []).filter((x) => x.status === "queued").length;
  const running = (items || []).find((x) => x.status === "running");
  const done = (items || [])
    .filter((x) => x.status === "completed")
    .sort((a, b) => (b.updated_at || 0) - (a.updated_at || 0))[0];

  return (
    <div className="card">
      <h3>
        🛠️ 工作站优化{" "}
        {mock && <span className="badge warn">本地演示数据</span>}
      </h3>
      {error && <div className="error-box">接口错误: {error}</div>}
      {!items && !error && <div className="hint">加载优化队列中...</div>}
      {items && items.length === 0 && (
        <div className="hint">
          优化队列为空 — 可在旧版仪表盘秘书对话中发送「优化工作站: 需求」添加。
        </div>
      )}
      {items && items.length > 0 && (
        <>
          <ul className="kv">
            <li>
              <span className="k">守护</span>
              <span className="v st-active">运行中</span>
            </li>
            <li>
              <span className="k">队列</span>
              <span className="v">{queued}</span>
            </li>
            <li>
              <span className="k">执行中</span>
              <span className="v">{running ? running.title : "-"}</span>
            </li>
            <li>
              <span className="k">待决策</span>
              <span className="v" style={{ color: waiting.length ? "var(--warn)" : undefined }}>
                {waiting.length}
              </span>
            </li>
            <li>
              <span className="k">最近完成</span>
              <span className="v">{done ? done.title : "-"}</span>
            </li>
          </ul>
          {waiting.length > 0 && (
            <div className="opt-waiting-list">
              <div className="opt-waiting-title">
                ⏳ 待 Boss 决策 ({waiting.length})
              </div>
              {waiting.slice().sort(byPriority).slice(0, 5).map((it) => (
                <div className={`opt-row opt-${it.status}`} key={it.id}>
                  <div className="opt-row-main">
                    <div className="opt-row-title">{it.title}</div>
                    <div className="opt-row-meta">
                      <OptBadges item={it} />
                      <span className="dim">{it.description}</span>
                    </div>
                  </div>
                  <div className="opt-row-actions">
                    <button
                      className="btn sm primary"
                      disabled={busyId === it.id}
                      onClick={() => decide(it.id, "approve")}
                    >
                      ✅ 确认
                    </button>
                    <button
                      className="btn sm danger"
                      disabled={busyId === it.id}
                      onClick={() => decide(it.id, "reject")}
                    >
                      🚫 拒绝
                    </button>
                  </div>
                </div>
              ))}
            </div>
          )}
        </>
      )}
      <div className="hint">
        完整队列与补充说明见旧版仪表盘「💬 秘书对话 → 🛠️ 优化」面板。
      </div>
    </div>
  );
}
