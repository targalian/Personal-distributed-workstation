"""Boss 通道 — Codex 对接秘书 (Secretary) 与 PM Agent 的命令行客户端。

背景: Codex CLI 运行在沙箱内, 既要以 Boss 身份向秘书交接需求/确认蓝图, 又要
监管 PM 执行、排查 bug。此前只能手写 Invoke-RestMethod, token/编码/错误处理
每次重来。本模块把 Station HTTP API 封成稳定的子命令, 供 Codex 与人共用。

信任根: ~/.lan_mesh/mesh_token (mesh token 持有人 = boss), 亦可用
LAN_MESH_TOKEN 环境变量或 --token 覆盖。

用法:
  python scripts/boss_channel.py health
  python scripts/boss_channel.py projects
  python scripts/boss_channel.py blueprint <project_id>
  python scripts/boss_channel.py blueprint <project_id> --set-file bp.json
  python scripts/boss_channel.py chat "消息内容" [--conv <conv_id>]
  python scripts/boss_channel.py tasks [--status running]
  python scripts/boss_channel.py task <task_id>
  python scripts/boss_channel.py graph <task_id> [--put-file g.json]
  python scripts/boss_channel.py pm [<pm_id>]
  python scripts/boss_channel.py progress <pm_id>
  python scripts/boss_channel.py reply <pm_id> "回复内容"
  python scripts/boss_channel.py watch [--interval 10]
  python scripts/boss_channel.py diag
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Optional

DEFAULT_BASE = os.environ.get("LAN_MESH_STATION", "http://127.0.0.1:45470")
TOKEN_FILE = Path.home() / ".lan_mesh" / "mesh_token"
TIMEOUT = 10.0


def load_token(explicit: str = "") -> str:
    """按 --token > LAN_MESH_TOKEN > ~/.lan_mesh/mesh_token 解析 boss 凭证。"""
    if explicit:
        return explicit.strip()
    env = os.environ.get("LAN_MESH_TOKEN", "").strip()
    if env:
        return env
    try:
        return TOKEN_FILE.read_text(encoding="utf-8").strip()
    except OSError:
        return ""


def request(method: str, path: str, base: str, token: str,
            payload: Optional[dict] = None,
            timeout: float = TIMEOUT) -> tuple[int, Any]:
    """发一次 Station API 请求, 返回 (status_code, 解析后的 body)。

    所有网络异常均收敛为 (0, {"error": ...}) / (status, body), 不抛出 —
    调用方按状态码分支, 契合 AGENTS.md 的错误隔离约定。
    """
    url = base.rstrip("/") + path
    data = None
    headers = {"Accept": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    if payload is not None:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8", "replace")
            return resp.status, _parse(raw)
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8", "replace")
        return exc.code, _parse(raw)
    except Exception as exc:
        return 0, {"error": str(exc)}


def _parse(raw: str) -> Any:
    try:
        return json.loads(raw) if raw else {}
    except json.JSONDecodeError:
        return {"raw": raw[:2000]}


def dump(obj: Any) -> None:
    """统一以 UTF-8 JSON 输出, 便于 Codex 直接解析。"""
    print(json.dumps(obj, ensure_ascii=False, indent=2))


def _fail(status: int, body: Any, hint: str = "") -> int:
    detail = body.get("detail") if isinstance(body, dict) else None
    print(f"[Station] 请求失败 status={status} detail={detail or body}",
          file=sys.stderr)
    if status == 401:
        print("[Station] 401: mesh token 无效, 检查 ~/.lan_mesh/mesh_token",
              file=sys.stderr)
    if status == 503:
        print("[Station] 503: Secretary 未激活, 先在面板启动秘书", file=sys.stderr)
    if hint:
        print(f"[Station] {hint}", file=sys.stderr)
    return 2


# ── 子命令 ───────────────────────────────────────────────────────────

def cmd_health(args: argparse.Namespace) -> int:
    """探活 + 组件状态 (白名单端点, 无需 token)。"""
    status, body = request("GET", "/health", args.base, "")
    if status != 200:
        return _fail(status, body)
    dump(body)
    return 0


def cmd_projects(args: argparse.Namespace) -> int:
    """列出项目 (名称/预算/状态, 折叠冗长字段)。"""
    status, body = request("GET", "/api/projects", args.base, args.token)
    if status != 200:
        return _fail(status, body)
    rows = []
    for p in (body.get("projects") or []):
        rows.append({
            "project_id": p.get("project_id", ""),
            "name": p.get("name", ""),
            "status": p.get("status", ""),
            "budget": f"{p.get('budget_used_usd', 0)}/{p.get('budget_limit_usd', 0)}",
            "has_charter": bool(p.get("charter")),
            "roadmap_phases": len(p.get("roadmap") or []),
        })
    dump({"projects": rows, "total": body.get("total", len(rows))})
    return 0


def cmd_blueprint(args: argparse.Namespace) -> int:
    """读取或整体写入项目蓝图 (charter/roadmap/decisions)。"""
    path = f"/api/projects/{args.project_id}/blueprint"
    if args.set_file:
        try:
            payload = json.loads(
                Path(args.set_file).read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            print(f"[Station] 蓝图文件读取失败: {exc}", file=sys.stderr)
            return 2
        status, body = request("PUT", path, args.base, args.token, payload)
        if status != 200:
            return _fail(status, body, "蓝图需含 charter(dict)/roadmap(list)/decisions(list)")
        print(f"[Station] 项目蓝图已写入: {args.project_id}")
        dump(body)
        return 0
    status, body = request("GET", path, args.base, args.token)
    if status != 200:
        return _fail(status, body)
    dump(body)
    return 0


def cmd_chat(args: argparse.Namespace) -> int:
    """以 Boss 身份向秘书发消息 (交接需求/确认蓝图/下达指令)。"""
    payload: dict = {"message": args.message}
    if args.conv:
        payload["conv_id"] = args.conv
    status, body = request("POST", "/api/secretary/chat", args.base,
                           args.token, payload, timeout=args.timeout)
    if status != 200:
        return _fail(status, body)
    dump(body)
    return 0


def cmd_tasks(args: argparse.Namespace) -> int:
    """列出任务 (可按状态过滤), 只显示监管需要的字段。"""
    status, body = request("GET", "/api/tasks", args.base, args.token)
    if status != 200:
        return _fail(status, body)
    rows = []
    for t in (body.get("tasks") or []):
        if args.status and t.get("status") != args.status:
            continue
        rows.append({
            "task_id": t.get("task_id", ""),
            "name": (t.get("name") or "")[:60],
            "status": t.get("status", ""),
            "pm_agent_id": t.get("pm_agent_id", ""),
            "project_id": (t.get("project_id") or "")[:8],
            "progress": t.get("progress", 0),
        })
    dump({"tasks": rows, "total": len(rows)})
    return 0


def cmd_task(args: argparse.Namespace) -> int:
    """单任务详情 (含交付物与 iter-90 验收自检结论)。"""
    status, body = request("GET", f"/api/tasks/{args.task_id}",
                           args.base, args.token)
    if status != 200:
        return _fail(status, body)
    delivery = ((body.get("output_data") or {}).get("_delivery") or {})
    dump({
        "task_id": body.get("task_id", ""),
        "name": body.get("name", ""),
        "status": body.get("status", ""),
        "pm_agent_id": body.get("pm_agent_id", ""),
        "error": body.get("error", ""),
        "delivery_summary": (delivery.get("summary") or "")[:400],
        "acceptance_review": delivery.get("acceptance_review") or {},
    })
    return 0


def cmd_graph(args: argparse.Namespace) -> int:
    """读取或回写任务 DAG 图 (任务图落地载体)。"""
    path = f"/api/tasks/{args.task_id}/graph"
    if args.put_file:
        try:
            payload = json.loads(
                Path(args.put_file).read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            print(f"[Station] 图文件读取失败: {exc}", file=sys.stderr)
            return 2
        status, body = request("PUT", path, args.base, args.token, payload)
        if status != 200:
            return _fail(status, body, "图需含 nodes/edges 且任务为 pending, 且无环")
        print(f"[Station] 任务图已写入: {args.task_id}")
        dump(body)
        return 0
    status, body = request("GET", path, args.base, args.token)
    if status != 200:
        return _fail(status, body)
    dump(body)
    return 0


def cmd_pm(args: argparse.Namespace) -> int:
    """PM 列表或单个 PM 详情 (含团队结构)。"""
    if args.pm_id:
        status, body = request("GET", f"/api/pm/{args.pm_id}",
                               args.base, args.token)
        if status != 200:
            return _fail(status, body)
        dump(body)
        return 0
    status, body = request("GET", "/api/pm", args.base, args.token)
    if status != 200:
        return _fail(status, body)
    rows = [{
        "pm_id": p.get("pm_id", ""),
        "task_id": p.get("task_id", ""),
        "status": p.get("status", ""),
        "device_name": p.get("device_name", ""),
    } for p in (body.get("pm_agents") or [])]
    dump({"pm_agents": rows, "total": len(rows)})
    return 0


def cmd_progress(args: argparse.Namespace) -> int:
    """PM 进度报告流水 (监管执行细节)。"""
    status, body = request(
        "GET", f"/api/pm/{args.pm_id}/progress?limit={args.limit}",
        args.base, args.token)
    if status != 200:
        return _fail(status, body)
    dump(body)
    return 0


def cmd_reply(args: argparse.Namespace) -> int:
    """向 awaiting_input 的 PM 注入 Boss 回复 (反向沟通通道)。"""
    status, body = request("POST", f"/api/pm/{args.pm_id}/inject-input",
                           args.base, args.token, {"content": args.content})
    if status != 200:
        return _fail(status, body, "PM 不在等待输入态时会 409")
    print(f"[Station] 已向 PM 注入回复: {args.pm_id}")
    dump(body)
    return 0


def cmd_watch(args: argparse.Namespace) -> int:
    """轮询任务/PM 状态, 变化时打印一行 (长任务监管)。"""
    seen: dict = {}
    deadline = time.time() + args.duration if args.duration else None
    while True:
        status, body = request("GET", "/api/tasks", args.base, args.token)
        if status != 200:
            print(f"[Station] watch 拉取失败 status={status}", file=sys.stderr)
        else:
            for t in (body.get("tasks") or []):
                tid = t.get("task_id", "")
                cur = (t.get("status", ""), t.get("progress", 0))
                if seen.get(tid) != cur:
                    seen[tid] = cur
                    stamp = time.strftime("%H:%M:%S")
                    print(f"[Station] {stamp} {tid} {cur[0]} "
                          f"progress={cur[1]} {(t.get('name') or '')[:40]}",
                          flush=True)
        if deadline and time.time() >= deadline:
            return 0
        time.sleep(max(2, args.interval))


def cmd_diag(args: argparse.Namespace) -> int:
    """一次性体检: 探活 + 秘书态 + 任务/PM 计数 + 停滞告警 + 近期错误。"""
    report: dict = {}
    _, health = request("GET", "/health", args.base, "")
    report["health"] = health if isinstance(health, dict) else {}
    for key, path in (("tasks", "/api/tasks"),
                      ("pm", "/api/pm"),
                      ("stall_alerts", "/api/runtime/task-stall-alerts"),
                      ("errors", "/api/errors/recent?limit=5")):
        status, body = request("GET", path, args.base, args.token)
        if status != 200:
            report[key] = {"_status": status,
                           "_detail": body.get("detail") if isinstance(body, dict) else None}
            continue
        if key == "tasks":
            tasks = body.get("tasks") or []
            counts: dict = {}
            for t in tasks:
                counts[t.get("status", "?")] = counts.get(t.get("status", "?"), 0) + 1
            report[key] = {"total": len(tasks), "by_status": counts}
        elif key == "pm":
            report[key] = {"total": body.get("total", 0)}
        elif key == "stall_alerts":
            alerts = body.get("alerts") or []
            report[key] = {
                "total": len(alerts),
                "watching": body.get("watching"),
                "stall_minutes": body.get("stall_minutes"),
                "top": alerts[:5],
            }
        else:
            errs = body.get("errors") or []
            report[key] = {"total": len(errs), "recent": errs[:5]}
    dump(report)
    return 0


def build_parser() -> argparse.ArgumentParser:
    """构建 CLI 参数解析器。"""
    parser = argparse.ArgumentParser(
        prog="boss_channel",
        description="Codex ↔ 秘书/PM 通道 (Station HTTP API 客户端)")
    parser.add_argument("--base", default=DEFAULT_BASE,
                        help=f"Station 基址 (默认 {DEFAULT_BASE})")
    parser.add_argument("--token", default="", help="覆盖 mesh token")
    sub = parser.add_subparsers(dest="cmd", required=True)

    sub.add_parser("health", help="探活与组件状态").set_defaults(func=cmd_health)
    sub.add_parser("projects", help="项目列表").set_defaults(func=cmd_projects)
    sub.add_parser("diag", help="一次性体检").set_defaults(func=cmd_diag)

    p_bp = sub.add_parser("blueprint", help="读/写项目蓝图")
    p_bp.add_argument("project_id")
    p_bp.add_argument("--set-file", default="",
                      help="从 JSON 文件整体写入 charter/roadmap/decisions")
    p_bp.set_defaults(func=cmd_blueprint)

    p_chat = sub.add_parser("chat", help="向秘书发消息")
    p_chat.add_argument("message")
    p_chat.add_argument("--conv", default="", help="指定对话 ID")
    p_chat.add_argument("--timeout", type=float, default=180.0,
                        help="LLM 回复可能较慢, 默认 180s")
    p_chat.set_defaults(func=cmd_chat)

    p_tasks = sub.add_parser("tasks", help="任务列表")
    p_tasks.add_argument("--status", default="", help="按状态过滤")
    p_tasks.set_defaults(func=cmd_tasks)

    p_task = sub.add_parser("task", help="任务详情与验收自检")
    p_task.add_argument("task_id")
    p_task.set_defaults(func=cmd_task)

    p_graph = sub.add_parser("graph", help="读/写任务 DAG 图")
    p_graph.add_argument("task_id")
    p_graph.add_argument("--put-file", default="", help="从 JSON 文件回写图")
    p_graph.set_defaults(func=cmd_graph)

    p_pm = sub.add_parser("pm", help="PM 列表或详情")
    p_pm.add_argument("pm_id", nargs="?", default="")
    p_pm.set_defaults(func=cmd_pm)

    p_prog = sub.add_parser("progress", help="PM 进度报告")
    p_prog.add_argument("pm_id")
    p_prog.add_argument("--limit", type=int, default=50)
    p_prog.set_defaults(func=cmd_progress)

    p_reply = sub.add_parser("reply", help="向 PM 注入 Boss 回复")
    p_reply.add_argument("pm_id")
    p_reply.add_argument("content")
    p_reply.set_defaults(func=cmd_reply)

    p_watch = sub.add_parser("watch", help="轮询任务状态变化")
    p_watch.add_argument("--interval", type=int, default=10)
    p_watch.add_argument("--duration", type=int, default=0,
                         help="秒; 0 表示持续运行")
    p_watch.set_defaults(func=cmd_watch)
    return parser


def main(argv: Optional[list] = None) -> int:
    """入口: 解析参数并分派子命令。"""
    parser = build_parser()
    args = parser.parse_args(argv)
    args.token = load_token(args.token)
    if not args.token and args.cmd not in ("health",):
        print("[Station] 未找到 mesh token (~/.lan_mesh/mesh_token / "
              "LAN_MESH_TOKEN / --token), 认证端点将返回 401", file=sys.stderr)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
