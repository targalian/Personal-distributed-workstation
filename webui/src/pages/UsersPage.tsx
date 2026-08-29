import { useCallback, useEffect, useState } from "react";
import { apiFetch, getRole } from "../api";

// iter-63 (团队场景深化): 用户管理页 — boss 增删用户/改角色/token 轮换;
// 非 boss 只读 (列表脱敏)。新 token 仅创建/轮换响应返回一次。

interface UserEntry {
  name: string;
  role: string;
  token_tail4?: string;
}

interface UserListResp {
  users: UserEntry[];
  admin_view: boolean;
}

const ROLE_LABEL: Record<string, string> = {
  boss: "boss",
  operator: "operator",
  viewer: "viewer",
};

const ROLE_COLOR: Record<string, string> = {
  boss: "#f44336",
  operator: "#ff9800",
  viewer: "#2196f3",
};

export default function UsersPage() {
  const [users, setUsers] = useState<UserEntry[]>([]);
  const [adminView, setAdminView] = useState(false);
  const [error, setError] = useState("");
  const [loading, setLoading] = useState(false);
  // 新增表单
  const [newName, setNewName] = useState("");
  const [newRole, setNewRole] = useState("viewer");
  // 一次性 token 弹层 (创建/轮换后展示)
  const [freshToken, setFreshToken] = useState("");
  const [freshLabel, setFreshLabel] = useState("");
  const [copied, setCopied] = useState(false);
  // 待确认删除的用户名
  const [confirmDel, setConfirmDel] = useState("");

  const isBoss = getRole() === "boss";

  const load = useCallback(async () => {
    setLoading(true);
    try {
      const resp = await apiFetch<UserListResp>("/api/station/users");
      setUsers(resp.users);
      setAdminView(resp.admin_view);
      setError("");
    } catch (e) {
      setError(String(e));
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    load();
  }, [load]);

  const showToken = (label: string, token: string) => {
    setFreshLabel(label);
    setFreshToken(token);
    setCopied(false);
  };

  const copyToken = async () => {
    try {
      await navigator.clipboard.writeText(freshToken);
      setCopied(true);
    } catch {
      setCopied(false);
    }
  };

  const createUser = async () => {
    if (!newName.trim()) return;
    setError("");
    try {
      const resp = await apiFetch<{ token?: string; error?: string }>(
        "/api/station/users",
        {
          method: "POST",
          body: JSON.stringify({ name: newName.trim(), role: newRole }),
        },
      );
      setNewName("");
      if (resp.token) {
        showToken(`用户 ${newName.trim()} 创建成功, 请立即保存 token`, resp.token);
      }
      load();
    } catch (e) {
      setError(String(e));
    }
  };

  const rotateToken = async (name: string) => {
    setError("");
    try {
      const resp = await apiFetch<{ token?: string }>(
        `/api/station/users/${encodeURIComponent(name)}/rotate-token`,
        { method: "POST" },
      );
      if (resp.token) {
        showToken(`用户 ${name} token 已轮换 (旧 token 立即失效)`, resp.token);
      }
      load();
    } catch (e) {
      setError(String(e));
    }
  };

  const changeRole = async (name: string, role: string) => {
    setError("");
    try {
      await apiFetch(`/api/station/users/${encodeURIComponent(name)}/role`, {
        method: "PUT",
        body: JSON.stringify({ role }),
      });
      load();
    } catch (e) {
      setError(String(e));
    }
  };

  const removeUser = async (name: string) => {
    setError("");
    try {
      await apiFetch(`/api/station/users/${encodeURIComponent(name)}`, {
        method: "DELETE",
      });
      setConfirmDel("");
      load();
    } catch (e) {
      setError(String(e));
    }
  };

  return (
    <div className="page">
      <div className="page-head">
        <h2>👥 用户管理</h2>
        <span className="dim">
          {isBoss ? "boss: 可增删用户 / 改角色 / 轮换 token" : "只读视图 (仅 boss 可管理)"}
        </span>
      </div>

      {error && <div className="error-box">{error}</div>}

      {isBoss && (
        <div className="user-create">
          <input
            className="input"
            placeholder="用户名 (如 alice)"
            value={newName}
            onChange={(e) => setNewName(e.target.value)}
            onKeyDown={(e) => e.key === "Enter" && createUser()}
          />
          <select
            className="input"
            value={newRole}
            onChange={(e) => setNewRole(e.target.value)}
          >
            <option value="viewer">viewer</option>
            <option value="operator">operator</option>
            <option value="boss">boss</option>
          </select>
          <button className="btn" onClick={createUser} disabled={!newName.trim()}>
            ➕ 新增用户
          </button>
        </div>
      )}

      {loading ? (
        <div className="dim">加载中…</div>
      ) : users.length === 0 ? (
        <div className="dim">
          未配置多用户 — 在 config.yaml 的 security.users 下添加用户后重启
          (或由 boss 在上方直接新增)。
        </div>
      ) : (
        <table className="user-table">
          <thead>
            <tr>
              <th>用户名</th>
              <th>角色</th>
              {adminView && <th>token 尾 4 位</th>}
              {isBoss && <th>操作</th>}
            </tr>
          </thead>
          <tbody>
            {users.map((u) => (
              <tr key={u.name}>
                <td>{u.name}</td>
                <td>
                  {isBoss ? (
                    <select
                      className="input sm"
                      value={u.role}
                      onChange={(e) => changeRole(u.name, e.target.value)}
                    >
                      <option value="viewer">viewer</option>
                      <option value="operator">operator</option>
                      <option value="boss">boss</option>
                    </select>
                  ) : (
                    <span
                      className="role-chip"
                      style={{ borderColor: ROLE_COLOR[u.role] ?? "#888", color: ROLE_COLOR[u.role] ?? "#888" }}
                    >
                      {ROLE_LABEL[u.role] ?? u.role}
                    </span>
                  )}
                </td>
                {adminView && <td className="mono">{u.token_tail4 || "—"}</td>}
                {isBoss && (
                  <td>
                    <button className="btn sm" onClick={() => rotateToken(u.name)}>
                      🔄 轮换
                    </button>
                    {confirmDel === u.name ? (
                      <>
                        <button className="btn sm danger" onClick={() => removeUser(u.name)}>
                          确认移除
                        </button>
                        <button className="btn sm" onClick={() => setConfirmDel("")}>
                          取消
                        </button>
                      </>
                    ) : (
                      <button className="btn sm danger" onClick={() => setConfirmDel(u.name)}>
                        🗑 移除
                      </button>
                    )}
                  </td>
                )}
              </tr>
            ))}
          </tbody>
        </table>
      )}

      {freshToken && (
        <div className="overlay" onClick={() => setFreshToken("")}>
          <div className="modal token-modal" onClick={(e) => e.stopPropagation()}>
            <h3>🔑 {freshLabel}</h3>
            <div className="token-box mono">{freshToken}</div>
            <div className="token-actions">
              <button className="btn" onClick={copyToken}>
                {copied ? "✅ 已复制" : "📋 复制"}
              </button>
              <button className="btn" onClick={() => setFreshToken("")}>
                我已保存, 关闭
              </button>
            </div>
            <div className="dim">⚠️ 明文 token 仅展示一次, 关闭后无法再次查看。</div>
          </div>
        </div>
      )}
    </div>
  );
}
