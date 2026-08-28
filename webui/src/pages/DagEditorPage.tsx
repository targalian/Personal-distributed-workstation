import { useCallback, useEffect, useMemo, useState } from "react";
import {
  ReactFlow,
  Background,
  Controls,
  MiniMap,
  applyNodeChanges,
  applyEdgeChanges,
  addEdge,
  Node,
  Edge,
  NodeChange,
  EdgeChange,
  Connection,
  Handle,
  Position,
} from "@xyflow/react";
import "@xyflow/react/dist/style.css";
import { apiFetch, TaskGraph } from "../api";

// iter-56 补强#4 F5.1: DAG 可视化编辑器 (GET/PUT /api/tasks/{id}/graph)
// - 节点按状态着色, 拖拽改布局, 增删节点/依赖连线, 条件边标注
// - 保存时 PUT 回写 (服务端校验仅 pending 可编辑 + 环检测)

const STATUS_COLOR: Record<string, string> = {
  pending: "#9e9e9e",
  running: "#4caf50",
  monitoring: "#9c27b0",
  completed: "#8bc34a",
  failed: "#f44336",
  paused: "#ff9800",
};

interface DagNodeData extends Record<string, unknown> {
  label: string;
  status: string;
  skill?: string;
}

/** 任务图节点 (服务端 GraphNode 形态)。 */
interface GNode {
  id: string;
  name: string;
  status: string;
  skill?: string;
  description?: string;
  x?: number;
  y?: number;
}

function toFlowNodes(nodes: GNode[]): Node<DagNodeData>[] {
  return nodes.map((n) => ({
    id: n.id,
    position: { x: n.x ?? 0, y: n.y ?? 0 },
    data: { label: n.name, status: n.status, skill: n.skill },
  }));
}

function toFlowEdges(edges: { source: string; target: string; condition?: string }[]): Edge[] {
  return edges.map((e, i) => ({
    id: `e${i}-${e.source}-${e.target}`,
    source: e.source,
    target: e.target,
    label: e.condition || undefined,
    animated: true,
  }));
}

function DagNode({ data }: { data: DagNodeData }) {
  const color = STATUS_COLOR[data.status] ?? "#9e9e9e";
  return (
    <div className="dag-node" style={{ borderColor: color }}>
      <Handle type="target" position={Position.Top} />
      <div className="dag-node-head">
        <span className="dag-dot" style={{ background: color }} />
        <span className="dag-title">{data.label}</span>
      </div>
      {data.skill && <div className="dag-skill">{data.skill}</div>}
      <Handle type="source" position={Position.Bottom} />
    </div>
  );
}

const nodeTypes = { dag: DagNode };

export default function DagEditorPage({ taskId }: { taskId: string }) {
  const [tid, setTid] = useState(taskId);
  const [nodes, setNodes] = useState<Node<DagNodeData>[]>([]);
  const [edges, setEdges] = useState<Edge[]>([]);
  const [taskName, setTaskName] = useState("");
  const [taskStatus, setTaskStatus] = useState("");
  const [message, setMessage] = useState("");
  const [error, setError] = useState("");
  const [saving, setSaving] = useState(false);

  const load = useCallback(async (id: string) => {
    if (!id) return;
    try {
      setError("");
      const g = await apiFetch<TaskGraph>(`/api/tasks/${id}/graph`);
      setNodes(toFlowNodes(g.nodes));
      setEdges(toFlowEdges(g.edges));
      setTaskName("");
      setTaskStatus("");
      // 顺带拉任务元信息 (名称/状态展示)
      try {
        const t = await apiFetch<{ name: string; status: string }>(
          `/api/tasks/${id}`,
        );
        setTaskName(t.name);
        setTaskStatus(t.status);
      } catch {
        /* 元信息拉取失败不影响图编辑 */
      }
    } catch (e) {
      setError(`加载图失败: ${e}`);
      setNodes([]);
      setEdges([]);
    }
  }, []);

  useEffect(() => {
    load(taskId);
  }, [taskId, load]);

  const onNodesChange = useCallback(
    (changes: NodeChange<Node<DagNodeData>>[]) =>
      setNodes((nds) => applyNodeChanges(changes, nds)),
    [],
  );
  const onEdgesChange = useCallback(
    (changes: EdgeChange[]) => setEdges((eds) => applyEdgeChanges(changes, eds)),
    [],
  );
  const onConnect = useCallback(
    (conn: Connection) => setEdges((eds) => addEdge({ ...conn, animated: true }, eds)),
    [],
  );

  const addNode = () => {
    const n = nodes.length + 1;
    setNodes((nds) => [
      ...nds,
      {
        id: `node-${n}`,
        position: { x: 80 + (n % 4) * 220, y: 80 + Math.floor(n / 4) * 140 },
        data: { label: `子任务 ${n}`, status: "pending" },
        type: "dag",
      },
    ]);
  };

  const save = async () => {
    if (!tid) {
      setError("请输入任务 ID");
      return;
    }
    setSaving(true);
    try {
      const gNodes = nodes.map((n) => ({
        id: n.id,
        name: String(n.data.label ?? n.id),
        status: n.data.status ?? "pending",
        skill: n.data.skill ?? "",
        x: Math.round(n.position.x),
        y: Math.round(n.position.y),
      }));
      const gEdges = edges.map((e) => ({
        source: e.source,
        target: e.target,
        condition: typeof e.label === "string" ? e.label : "",
      }));
      const resp = await apiFetch<{ ok: boolean; message: string }>(
        `/api/tasks/${tid}/graph`,
        { method: "PUT", body: JSON.stringify({ nodes: gNodes, edges: gEdges }) },
      );
      setMessage(resp.message);
      setError("");
    } catch (e) {
      setError(`保存失败: ${e}`);
    } finally {
      setSaving(false);
    }
  };

  const flowNodes = useMemo(
    () => nodes.map((n) => ({ ...n, type: "dag" as const })),
    [nodes],
  );

  return (
    <div className="page dag-page">
      <div className="page-head">
        <h2>DAG 可视化编辑器</h2>
        <div className="toolbar">
          <input
            className="input"
            placeholder="任务 ID"
            value={tid}
            onChange={(e) => setTid(e.target.value)}
          />
          <button className="btn" onClick={() => load(tid)}>
            加载
          </button>
          <button className="btn" onClick={addNode}>
            + 节点
          </button>
          <button className="btn primary" onClick={save} disabled={saving}>
            {saving ? "保存中..." : "保存图"}
          </button>
        </div>
      </div>
      {taskName && (
        <div className="hint">
          任务: {taskName} <span className={`v st-${taskStatus}`}>{taskStatus}</span>
          (服务端校验: 仅 pending 任务可保存图结构)
        </div>
      )}
      {error && <div className="error-banner">{error}</div>}
      {message && <div className="ok-banner">{message}</div>}
      <div className="dag-canvas">
        <ReactFlow
          nodes={flowNodes}
          edges={edges}
          onNodesChange={onNodesChange}
          onEdgesChange={onEdgesChange}
          onConnect={onConnect}
          nodeTypes={nodeTypes}
          fitView
          proOptions={{ hideAttribution: true }}
        >
          <Background />
          <Controls />
          <MiniMap pannable zoomable />
        </ReactFlow>
      </div>
      <div className="hint">
        拖动节点调整布局; 节点间拖线建立依赖; 双击连线可加条件表达式;
        保存前服务端会做环检测。
      </div>
    </div>
  );
}
