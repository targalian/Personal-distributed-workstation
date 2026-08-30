#!/usr/bin/env python3
"""影子开发模式 — 让 PM/CLI Agent 在仓库副本上自主开发, 产出 diff 供人审。

背景 (iter-70): agent_runtime._handle_cli_agent 会以 --full-auto/--yes 拉起
claude/codex/aider, 其子进程不受父进程沙箱与 SHELL_BLOCKED_PATTERNS 约束。
若直接指向主仓库, Agent 可改写自身安全策略 (agent_runtime.py)、解除上库门禁
(.githooks/), 且 _dev_file_watcher 热重载会在写入瞬间重启承载它的进程 —
形成不可控自举。

影子模式的三道护栏:
  ① 隔离: git clone 出独立副本, CLI Agent 的 cwd 指向副本 (白名单放行),
     主仓库全程只读 — 即使 Agent 失控也炸不到本体;
  ② 门禁: 副本内强制跑 py_compile + pytest + sync_docs, 任一 FAIL 即判定
     失败并保留现场, 绝不上报成功; 另复核护栏/门禁文件未被改写;
  ③ 人闸: 只产出 diff 文件, 不 apply、不 commit、不 push — 落地仍走
     scripts/ship.ps1 由非 Agent 进程执行。

用法:
  python -m lan_mesh.shadow_dev --task "按 docs/reference/controller-split-plan.md 执行 Phase 1"
  python -m lan_mesh.shadow_dev --task "..." --backend claude --timeout 1800
  python -m lan_mesh.shadow_dev --list                 # 列出历史影子运行
  python -m lan_mesh.shadow_dev --show <run_id>        # 查看某次 diff 摘要
"""
from __future__ import annotations

import argparse
import io
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import threading
import uuid
from datetime import datetime
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
# 影子副本与产物统一放仓库外, 避免污染主仓库工作区与 .gitignore
# 默认落在系统临时目录 (家目录在受限沙箱下可能不可写); SHADOW_DEV_HOME 可覆盖
SHADOW_HOME = Path(os.environ.get(
    "SHADOW_DEV_HOME", str(Path(tempfile.gettempdir()) / "lan_mesh_shadow")))

# 门禁: 与 AGENTS.md 验证流程 / .githooks/pre-push 对齐
GATE_PY_FILES = [
    "lan_mesh/pm_agent.py", "lan_mesh/station_controller.py",
    "lan_mesh/station_api.py", "lan_mesh/chat_handler.py",
    "lan_mesh/bot_gateway.py", "lan_mesh/database.py",
    "lan_mesh/worker.py", "lan_mesh/api.py", "lan_mesh/orchestrator.py",
]


def log(msg: str) -> None:
    """带合法 AgentRuntime 前缀的日志。"""
    print(f"[AgentRuntime] {msg}", flush=True)


def run(cmd: list[str], cwd: Path, timeout: int = 600) -> tuple[int, str, str]:
    """执行命令, 返回 (returncode, stdout, stderr)。"""
    try:
        r = subprocess.run(cmd, cwd=str(cwd), capture_output=True, text=True,
                           encoding="utf-8", errors="replace", timeout=timeout)
        return r.returncode, r.stdout or "", r.stderr or ""
    except subprocess.TimeoutExpired:
        return -1, "", f"命令超时 ({timeout}s): {' '.join(str(c) for c in cmd)}"
    except Exception as exc:
        return -1, "", f"命令异常: {exc}"


# 影子副本不需要的重目录/临时产物 (拷贝时跳过, 显著加速且避免污染)
# 影子副本永不携带本地密钥/个人运行配置; 所有符号链接也跳过,
# 避免链接目标落在副本外破坏物理隔离
SHADOW_IGNORE = shutil.ignore_patterns(
    ".git", ".venv", "__pycache__", "*.pyc", "node_modules", ".pytest_cache",
    ".qoder", "temp_resault", "_x4*", "_x5*", "_x6*", "*.log", "*.sqlite3",
    ".env", ".env.*", "*.secret", "*.key", "*.pem", "config.yaml",
    "model_pool.yaml", "secrets.yaml", "resources.yaml",
    ".codex_last_message.txt",
)


def _shadow_ignore(directory: str, names: list[str]) -> set[str]:
    """过滤敏感配置与符号链接的 copytree 回调。"""
    ignored = set(SHADOW_IGNORE(directory, names))
    ignored.update(
        name for name in names
        if os.path.islink(os.path.join(directory, name))
    )
    return ignored


def ensure_shadow_home_outside_repo() -> None:
    """拒绝把影子产物目录配置到主仓库内部。"""
    home = SHADOW_HOME.resolve()
    if home == REPO_ROOT.resolve() or home.is_relative_to(REPO_ROOT.resolve()):
        raise RuntimeError(f"SHADOW_DEV_HOME 必须位于主仓库外: {home}")


def create_shadow(run_id: str) -> Path:
    """建立影子副本: 拷贝工作树 + 全新 git init 基线。

    不用 git clone: 受限沙箱内 git-upload-pack 不可达 (clone 即便对本地路径
    也走 upload-pack 协议), 且 clone 只能取到 HEAD 已提交内容 —— 拷贝工作树
    还能带上当前未提交改动, 更贴近真实开发起点。

    副本内 git init 出独立基线, 使 `git diff` 天然等价于「Agent 改了什么」,
    且与主仓库 .git 完全无关联 (无 object 共享, 无 remote, 推不出去)。
    """
    shadow = SHADOW_HOME / run_id / "repo"
    shadow.parent.mkdir(parents=True, exist_ok=True)
    log(f"建立影子副本 → {shadow}")
    shutil.copytree(REPO_ROOT, shadow, ignore=_shadow_ignore, symlinks=True)

    # 独立 git 基线: 无 remote (推不出去), 无 hooks (不触发其它 Agent)
    run(["git", "init", "-q"], shadow)
    run(["git", "config", "core.hooksPath", str(shadow / ".git" / "_no_hooks")], shadow)
    run(["git", "config", "user.email", "shadow@localhost"], shadow)
    run(["git", "config", "user.name", "Shadow Dev"], shadow)
    run(["git", "add", "-A"], shadow, 300)
    code, _, err = run(["git", "commit", "-q", "-m", "shadow baseline"], shadow, 300)
    if code != 0:
        raise RuntimeError(f"影子基线提交失败: {err[:300]}")
    log("影子副本就绪 (独立 git 基线, 无 remote/hooks, 与主仓库物理隔离)")
    return shadow


def invoke_cli_agent(shadow: Path, task: str, backend: str, timeout: int) -> dict:
    """在影子副本内调用 CLI Agent 自主开发。

    通过 CLI_AGENT_ALLOWED_ROOTS 白名单放行副本目录; 主仓库仍被
    validate_cli_agent_cwd 拒绝 (CLI_AGENT_ALLOW_SELF_REPO 未开)。
    """
    sys.path.insert(0, str(REPO_ROOT))
    os.environ["CLI_AGENT_ALLOWED_ROOTS"] = str(SHADOW_HOME)
    os.environ.pop("CLI_AGENT_ALLOW_SELF_REPO", None)

    import importlib
    ar = importlib.import_module("lan_mesh.agent_runtime")
    importlib.reload(ar)  # 让白名单环境变量生效

    reject = ar.validate_cli_agent_cwd(str(shadow))
    if reject:
        return {"error": f"影子副本未通过 cwd 校验: {reject}"}

    runtime = ar.AgentRuntime(agent_id="shadow-dev", shared_folder_path=str(shadow))
    prompt = (
        f"{task}\n\n"
        "【硬约束 — 必须遵守】\n"
        "1. 严格遵循仓库根 AGENTS.md 的编码规范 (类型标注、模块 docstring、"
        "日志前缀、函数不超 80 行、Pydantic v2)。\n"
        "2. 禁止修改 agent_runtime.py 的安全策略、.githooks/、scripts/sync_push.ps1、"
        "scripts/ship.ps1、影子模式与协作锁文件 - 以 SELF_MOD_FORBIDDEN 清单为准。\n"
        "3. 不得删除已有 API 端点, 不得改 DB 主键结构, 不得硬编码密钥。\n"
        "4. 完成后必须自行运行: python -m pytest -q 确认全绿。\n"
        "5. 不要执行 git commit / git push, 只改文件。\n"
    )
    log(f"拉起 CLI Agent (backend={backend or 'auto'}, timeout={timeout}s)…")
    log("注意: 该步骤由 Agent 自主迭代, 期间无输出属正常")
    started = time.time()
    # 直接调 cli_agent 处理器: 绕过 required_skill 路由, 避免任务描述被
    # 关键词分类误判到 code_generation 等单轮处理器
    result = runtime._handle_cli_agent({
        "requirement": prompt,
        "cwd": str(shadow),
        "backend": backend,
        "timeout": timeout,
    })
    result["_elapsed"] = round(time.time() - started, 1)
    return result


def run_gates(shadow: Path) -> dict:
    """在副本内跑门禁: 编译 + 导入 + 测试 + 文档清单 + 护栏复核。"""
    gates = {}

    log("门禁 1/6: 编译检查")
    code_str = ("import py_compile;"
                f"[py_compile.compile(f, doraise=True) for f in {GATE_PY_FILES!r}];"
                "print('ok')")
    code, _, err = run([sys.executable, "-c", code_str], shadow, 180)
    gates["compile"] = {"pass": code == 0, "detail": err[-400:] if code else "PASS"}

    log("门禁 2/6: 导入检查")
    code, _, err = run([sys.executable, "-c",
                        "from lan_mesh.station_controller import StationController; print('ok')"],
                       shadow, 180)
    gates["import"] = {"pass": code == 0, "detail": err[-400:] if code else "PASS"}

    log("门禁 3/6: 全量回归 (约 2 分钟)")
    code, out, err = run([sys.executable, "-m", "pytest", "-q"], shadow, 900)
    tail = (out or err).strip().splitlines()
    gates["pytest"] = {"pass": code == 0,
                       "detail": tail[-1] if tail else "no output"}

    log("门禁 4/6: 文档清单一致性")
    code, out, err = run([sys.executable, "scripts/sync_docs.py"], shadow, 180)
    gates["sync_docs"] = {"pass": code == 0,
                          "detail": "PASS" if code == 0 else "清单漂移, 需 --write"}

    log("门禁 5/6: 护栏文件复核")
    changed = list_changed_files(shadow)
    from .agent_runtime import check_self_mod_violations
    hits = check_self_mod_violations(changed)
    gates["self_mod"] = {"pass": not hits,
                         "detail": "PASS" if not hits else f"改写了护栏文件: {hits}"}
    return gates


def list_changed_files(shadow: Path) -> list[str]:
    """列出副本内已修改与未忽略新增的文件。"""
    _, modified, _ = run(["git", "diff", "--name-only", "HEAD"], shadow, 60)
    _, untracked, _ = run(
        ["git", "ls-files", "--others", "--exclude-standard"], shadow, 60)
    return [line.strip() for line in (modified + "\n" + untracked).splitlines()
            if line.strip()]


SECRET_PATTERNS = [
    re.compile(
        r"(?i)(?:api[_-]?key|access[_-]?key|secret|token|password)\s*[:=]\s*"
        r"[\"']?[A-Za-z0-9_\-]{20,}"),
    re.compile(r"(?i)\b(?:sk|ak|rk)-[A-Za-z0-9_\-]{20,}\b"),
    re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._\-]{20,}\b"),
]


def scan_added_lines_for_secrets(patch: str) -> list[str]:
    """扫描 diff 新增行中的常见明文密钥形态。"""
    findings: list[str] = []
    for line in patch.splitlines():
        if not line.startswith("+") or line.startswith("+++"):
            continue
        if any(pattern.search(line) for pattern in SECRET_PATTERNS):
            findings.append(line[:240])
    return findings


def collect_diff(shadow: Path, out_dir: Path) -> dict:
    """收集副本相对 HEAD 的全部改动为 diff 文件 (含新增文件)。"""
    artifact = shadow / ".codex_last_message.txt"
    if artifact.is_file():
        artifact.unlink()
    run(["git", "add", "-A"], shadow, 120)  # 让新增文件进入 diff
    code, out, err = run(["git", "diff", "--cached", "--stat"], shadow, 120)
    stat = out.strip()
    code, patch, err = run(["git", "diff", "--cached"], shadow, 300)

    diff_path = out_dir / "changes.patch"
    io.open(diff_path, "w", encoding="utf-8", newline="\n").write(patch)
    code, files_out, _ = run(["git", "diff", "--cached", "--name-status"], shadow, 120)
    return {
        "diff_file": str(diff_path),
        "stat": stat,
        "files": [ln.strip() for ln in files_out.splitlines() if ln.strip()],
        "bytes": len(patch),
        "secret_findings": scan_added_lines_for_secrets(patch),
    }


def simulate_agent_change(shadow: Path) -> None:
    '''Self-test mode: append a marker to the shadow README only.'''
    readme = shadow / 'README.md'
    text = io.open(readme, encoding='utf-8').read()
    marker = '\n[shadow-dev simulate]\n'
    if marker.strip() not in text:
        io.open(readme, 'w', encoding='utf-8',
                newline='\n').write(text + marker)


def new_run_id() -> str:
    """生成同秒内也不冲突的影子运行 ID。"""
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    return f"{stamp}-{uuid.uuid4().hex[:6]}"


def execute_run(run_id: str, task: str, backend: str, timeout: int,
                keep: bool, simulate: bool = False) -> dict:
    """执行一次完整影子开发流程并返回报告。"""
    ensure_shadow_home_outside_repo()
    out_dir = SHADOW_HOME / run_id
    out_dir.mkdir(parents=True, exist_ok=True)

    log("=" * 56)
    log(f"影子开发运行 {run_id}")
    log(f"任务: {task[:120]}")
    log("=" * 56)

    report = {"run_id": run_id, "task": task,
              "started_at": datetime.now().isoformat(),
              "backend": backend or "auto"}
    try:
        report['backend'] = 'simulate' if simulate else (backend or 'auto')
        shadow = create_shadow(run_id)

        if simulate:
            simulate_agent_change(shadow)
            agent_result = {'backend': 'simulate', 'returncode': 0, '_elapsed': 0.0}
        else:
            agent_result = invoke_cli_agent(shadow, task, backend, timeout)
            if agent_result.get('returncode') not in (0, None):
                agent_result['error'] = (
                    'CLI Agent exit code ' + str(agent_result['returncode']))
        report["agent"] = {k: v for k, v in agent_result.items()
                           if k in ("backend", "elapsed_secs", "returncode",
                                    "error", "_elapsed")}
        if agent_result.get("error"):
            log(f"CLI Agent 失败: {agent_result['error'][:300]}")
            report["verdict"] = "AGENT_FAILED"
        else:
            log(f"CLI Agent 完成 ({agent_result.get('_elapsed')}s)")
            gates = run_gates(shadow)
            report["diff"] = collect_diff(shadow, out_dir)
            findings = report["diff"]["secret_findings"]
            gates["secrets"] = {
                "pass": not findings,
                "detail": "PASS" if not findings else f"疑似明文密钥: {len(findings)} 处",
            }
            report["gates"] = gates
            all_pass = all(g["pass"] for g in gates.values())
            for name, g in gates.items():
                log(f"  {name:<10}: {'PASS' if g['pass'] else 'FAIL'} - {g['detail'][:120]}")
            report["verdict"] = "READY_FOR_REVIEW" if all_pass else "GATES_FAILED"

        report["shadow_path"] = str(shadow)
        report["finished_at"] = datetime.now().isoformat()
    except Exception as exc:
        report["verdict"] = "ERROR"
        report["error"] = str(exc)
        log(f"运行异常: {exc}")

    rp = out_dir / "report.json"
    io.open(rp, "w", encoding="utf-8", newline="\n").write(
        json.dumps(report, ensure_ascii=False, indent=2))

    log("")
    log(f"结论: {report.get('verdict')}")
    if report.get("diff"):
        log(f"diff: {report['diff']['diff_file']} ({report['diff']['bytes']} 字节)")
        for line in report["diff"]["stat"].splitlines()[-6:]:
            log(f"  {line}")
    log(f"报告: {rp}")
    log("")
    log("主仓库未被改动 (影子模式)。审核通过后手工应用:")
    log(f"  git apply {report.get('diff', {}).get('diff_file', '<diff>')}")
    log("  然后走 powershell -File scripts/ship.ps1 提交推送")
    if not keep and report.get("verdict") == "AGENT_FAILED":
        log("(失败运行的副本已保留供排查, 可手工删除)")
    return report


def do_run(task: str, backend: str, timeout: int, keep: bool,
           simulate: bool = False) -> int:
    """执行一次完整影子开发流程 (CLI 兼容入口)。"""
    report = execute_run(new_run_id(), task, backend, timeout, keep, simulate)
    return 0 if report.get("verdict") == "READY_FOR_REVIEW" else 1


class ShadowDevManager:
    """常驻影子开发守护: 串行队列 + 内存状态 + 历史报告聚合。"""

    def __init__(self) -> None:
        self._condition = threading.Condition()
        self._queue: list[dict] = []
        self._runs: dict[str, dict] = {}
        self._thread: threading.Thread | None = None
        self._stopping = False

    def start_guardian(self) -> dict:
        """启动常驻守护线程 (幂等)。"""
        with self._condition:
            if self._thread and self._thread.is_alive():
                return {"running": True, "queued": len(self._queue)}
            self._stopping = False
            self._thread = threading.Thread(
                target=self._guardian_loop, name="shadow-dev-guardian", daemon=True)
            self._thread.start()
            return {"running": True, "queued": len(self._queue)}

    def stop_guardian(self) -> dict:
        """停止守护线程; 已进入 CLI 的任务无法强制中断。"""
        with self._condition:
            self._stopping = True
            for run in self._queue:
                self._runs[run["run_id"]]["status"] = "cancelled"
            self._queue.clear()
            self._condition.notify_all()
        if self._thread and self._thread is not threading.current_thread():
            self._thread.join(timeout=2)
        return {"running": self._thread is not None and self._thread.is_alive(),
                "cancelled": True}

    def submit(self, task: str, backend: str = "", timeout: int = 1800,
               simulate: bool = False) -> dict:
        """提交影子任务; 同一时间只执行一个, 其余排队。"""
        task = task.strip()
        if not task:
            raise ValueError("task 不能为空")
        run_id = new_run_id()
        record = {
            "run_id": run_id,
            "task": task,
            "backend": "simulate" if simulate else (backend or "auto"),
            "timeout": max(30, min(timeout, 1800)),
            "simulate": simulate,
            "status": "queued",
            "created_at": datetime.now().isoformat(),
        }
        with self._condition:
            if self._stopping:
                raise RuntimeError("影子开发守护已停止")
            self._runs[run_id] = record
            self._queue.append(record)
            self._condition.notify_all()
        self.start_guardian()
        return record

    def status(self) -> dict:
        """返回守护状态与队列摘要。"""
        with self._condition:
            queued = len(self._queue)
            running = any(run["status"] == "running" for run in self._runs.values())
            thread_alive = bool(self._thread and self._thread.is_alive())
            return {"running": thread_alive, "busy": running,
                    "queued": queued, "stopping": self._stopping}

    def list_runs(self) -> list[dict]:
        """合并内存队列/运行状态与历史报告。"""
        with self._condition:
            records = [dict(run) for run in self._runs.values()]
        known = {run["run_id"] for run in records}
        records.extend(self._load_history(known))
        return sorted(records, key=lambda run: run.get("run_id", ""), reverse=True)

    def get_run(self, run_id: str) -> dict | None:
        """获取单次运行详情。"""
        with self._condition:
            if run_id in self._runs:
                return dict(self._runs[run_id])
        return self._read_report(run_id)

    def _guardian_loop(self) -> None:
        """串行消费影子任务队列。"""
        while True:
            with self._condition:
                while not self._queue and not self._stopping:
                    self._condition.wait()
                if self._stopping:
                    return
                request = self._queue.pop(0)
                run_id = request["run_id"]
                self._runs[run_id]["status"] = "running"
                self._runs[run_id]["started_at"] = datetime.now().isoformat()
            try:
                report = execute_run(
                    run_id, request["task"], request["backend"],
                    request["timeout"], True, request["simulate"])
                with self._condition:
                    self._runs[run_id].update(report)
                    self._runs[run_id]["status"] = report.get("verdict", "ERROR")
            except Exception as exc:
                log(f"守护执行异常 {run_id}: {exc}")
                with self._condition:
                    self._runs[run_id]["status"] = "ERROR"
                    self._runs[run_id]["error"] = str(exc)
                    self._runs[run_id]["finished_at"] = datetime.now().isoformat()

    def _read_report(self, run_id: str) -> dict | None:
        """读取历史 report.json。"""
        report_path = SHADOW_HOME / run_id / "report.json"
        if not report_path.is_file():
            return None
        try:
            data = json.loads(report_path.read_text(encoding="utf-8"))
            data["status"] = data.get("verdict", "unknown")
            return data
        except Exception:
            return {"run_id": run_id, "status": "REPORT_ERROR"}

    def _load_history(self, known_ids: set[str]) -> list[dict]:
        """加载不在内存中的历史运行。"""
        if not SHADOW_HOME.is_dir():
            return []
        records = []
        for directory in SHADOW_HOME.iterdir():
            if not directory.name.startswith("20") or directory.name in known_ids:
                continue
            report = self._read_report(directory.name)
            if report:
                records.append(report)
        return records


def do_list() -> int:
    """列出历史影子运行。"""
    if not SHADOW_HOME.is_dir():
        print("尚无影子运行记录。")
        return 0
    rows = []
    for d in sorted(SHADOW_HOME.iterdir(), reverse=True):
        rp = d / "report.json"
        if rp.is_file():
            try:
                r = json.loads(rp.read_text(encoding="utf-8"))
                rows.append((d.name, r.get("verdict", "?"), r.get("task", "")[:60]))
            except Exception:
                continue
    if not rows:
        print("尚无完成的影子运行。")
        return 0
    print(f"{'RUN_ID':<18} {'VERDICT':<18} TASK")
    for rid, v, t in rows:
        print(f"{rid:<18} {v:<18} {t}")
    return 0


def do_show(run_id: str) -> int:
    """展示某次运行的报告摘要。"""
    rp = SHADOW_HOME / run_id / "report.json"
    if not rp.is_file():
        print(f"未找到运行记录: {run_id}")
        return 1
    r = json.loads(rp.read_text(encoding="utf-8"))
    print(json.dumps(r, ensure_ascii=False, indent=2))
    return 0


def main() -> int:
    """影子开发入口。"""
    ap = argparse.ArgumentParser(description="影子开发模式 (副本内自主开发 + diff 上报)")
    ap.add_argument("--task", help="任务描述 (自然语言)")
    ap.add_argument("--backend", default="", help="CLI Agent 后端: claude/codex/aider")
    ap.add_argument("--timeout", type=int, default=1800, help="Agent 超时秒数 (最大 1800)")
    ap.add_argument("--keep", action="store_true", help="保留副本 (默认保留)")
    ap.add_argument("--list", action="store_true", help="列出历史运行")
    ap.add_argument("--show", metavar="RUN_ID", help="查看某次运行报告")
    ap.add_argument('--simulate', action='store_true',
                    help='self-test without calling a CLI Agent')
    args = ap.parse_args()

    if args.list:
        return do_list()
    if args.show:
        return do_show(args.show)
    if not args.task:
        ap.print_help()
        return 2
    return do_run(args.task, args.backend, min(args.timeout, 1800),
                  args.keep, args.simulate)


if __name__ == "__main__":
    raise SystemExit(main())
