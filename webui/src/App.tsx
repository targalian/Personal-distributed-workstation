import { useEffect, useState } from "react";
import StationPage from "./pages/StationPage";
import TasksPage from "./pages/TasksPage";
import DagEditorPage from "./pages/DagEditorPage";

// iter-56: hash 路由 (服务端 /spa 静态托管无 SSR 路由, hash 免服务端配置)
type Route = "station" | "tasks" | "dag";

function parseHash(): { route: Route; taskId?: string } {
  const h = window.location.hash.replace(/^#\/?/, "");
  const [first, second] = h.split("/");
  if (first === "tasks") return { route: "tasks" };
  // dag 无 taskId 时仍进入编辑器 (任务 ID 可手输/从列表跳转带参)
  if (first === "dag")
    return { route: "dag", taskId: second ? decodeURIComponent(second) : "" };
  return { route: "station" };
}

const NAV: { key: Route; label: string; icon: string }[] = [
  { key: "station", label: "Station", icon: "🏢" },
  { key: "tasks", label: "任务", icon: "📋" },
  { key: "dag", label: "DAG 编辑", icon: "🕸️" },
];

/** 顶栏 WS 连接状态 (与旧版 dashboard 一致的 /ws 通道)。 */
function WsStatus() {
  const [state, setState] = useState<"connecting" | "online" | "offline">(
    "connecting",
  );

  useEffect(() => {
    let ws: WebSocket | null = null;
    let closed = false;
    const proto = location.protocol === "https:" ? "wss" : "ws";
    ws = new WebSocket(`${proto}://${location.host}/ws`);
    ws.onopen = () => !closed && setState("online");
    ws.onclose = () => !closed && setState("offline");
    ws.onerror = () => {
      ws?.close();
    };
    return () => {
      closed = true;
      ws?.close();
    };
  }, []);

  const label =
    state === "online" ? "已连接" : state === "connecting" ? "连接中" : "离线";
  const cls = state === "online" ? "ws-dot" : "ws-dot offline";
  return (
    <span className="ws-status">
      <span className={cls} />
      WS {label}
    </span>
  );
}

export default function App() {
  const [route, setRoute] = useState(parseHash());

  useEffect(() => {
    const onHash = () => setRoute(parseHash());
    window.addEventListener("hashchange", onHash);
    return () => window.removeEventListener("hashchange", onHash);
  }, []);

  return (
    <div className="app">
      <header className="topbar">
        <span className="brand">LAN Mesh</span>
        <nav className="nav">
          {NAV.map((n) => (
            <a
              key={n.key}
              href={`#/${n.key}`}
              className={route.route === n.key ? "nav-item active" : "nav-item"}
            >
              <span>{n.icon}</span>
              {n.label}
            </a>
          ))}
        </nav>
        <WsStatus />
      </header>
      <main className="content">
        {route.route === "station" && <StationPage />}
        {route.route === "tasks" && <TasksPage />}
        {route.route === "dag" && (
          <DagEditorPage taskId={route.taskId ?? ""} />
        )}
      </main>
    </div>
  );
}
