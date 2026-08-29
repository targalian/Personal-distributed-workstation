import { useCallback, useEffect, useState } from "react";
import StationPage from "./pages/StationPage";
import TasksPage from "./pages/TasksPage";
import DagEditorPage from "./pages/DagEditorPage";
import UsersPage from "./pages/UsersPage";
import { ensureMeshToken, getRole, loginUser, logoutUser } from "./api";

// iter-56: hash 路由 (服务端 /spa 静态托管无 SSR 路由, hash 免服务端配置)
type Route = "station" | "tasks" | "dag" | "users";

function parseHash(): { route: Route; taskId?: string } {
  const h = window.location.hash.replace(/^#\/?/, "");
  const [first, second] = h.split("/");
  if (first === "tasks") return { route: "tasks" };
  // dag 无 taskId 时仍进入编辑器 (任务 ID 可手输/从列表跳转带参)
  if (first === "dag")
    return { route: "dag", taskId: second ? decodeURIComponent(second) : "" };
  // iter-63: 用户管理页
  if (first === "users") return { route: "users" };
  return { route: "station" };
}

const NAV: { key: Route; label: string; icon: string }[] = [
  { key: "station", label: "Station", icon: "🏢" },
  { key: "tasks", label: "任务", icon: "📋" },
  { key: "dag", label: "DAG 编辑", icon: "🕸️" },
  { key: "users", label: "用户", icon: "👥" },
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
  // iter-58 (F5.2): 顶栏角色徽章 (boss/operator/viewer/未登录) + 身份切换面板
  const [role, setRole] = useState(getRole());
  const [authOpen, setAuthOpen] = useState(false);
  const [tokenInput, setTokenInput] = useState("");

  const refreshRole = useCallback(() => {
    ensureMeshToken().then(() => setRole(getRole()));
  }, []);

  useEffect(() => {
    const onHash = () => setRoute(parseHash());
    window.addEventListener("hashchange", onHash);
    return () => window.removeEventListener("hashchange", onHash);
  }, []);

  useEffect(() => {
    refreshRole();
  }, [refreshRole]);

  // 切换身份: 存用户 token 后重新拉取角色 (服务端回显)
  const applyToken = () => {
    loginUser(tokenInput);
    setTokenInput("");
    setAuthOpen(false);
    refreshRole();
  };

  // 退出登录: 回到未登录态 (多用户模式下仅剩引导端点可用)
  const logout = () => {
    logoutUser();
    setAuthOpen(false);
    refreshRole();
  };

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
        <div className="auth-chip">
          <button
            className={`role-badge role-${role || "anon"}`}
            title={role ? `当前角色: ${role}` : "点击登录 (输入个人 token)"}
            onClick={() => setAuthOpen((o) => !o)}
          >
            {role || "未登录"}
          </button>
          {authOpen && (
            <div className="auth-pop">
              <input
                className="input"
                type="password"
                placeholder="个人 token"
                value={tokenInput}
                onChange={(e) => setTokenInput(e.target.value)}
                onKeyDown={(e) => e.key === "Enter" && applyToken()}
              />
              <button className="btn" onClick={applyToken}>
                切换
              </button>
              <button className="btn" onClick={logout}>
                退出
              </button>
            </div>
          )}
        </div>
      </header>
      <main className="content">
        {route.route === "station" && <StationPage />}
        {route.route === "tasks" && <TasksPage />}
        {route.route === "dag" && (
          // key=role: 身份切换后重挂载, 刷新 viewer 只读状态
          <DagEditorPage key={role} taskId={route.taskId ?? ""} />
        )}
        {route.route === "users" && <UsersPage key={role} />}
      </main>
    </div>
  );
}
