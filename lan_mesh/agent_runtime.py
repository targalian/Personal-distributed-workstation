"""
Agent 运行时 — Worker 端任务执行引擎

职责:
1. 接收 Secretary 分发的子任务
2. 根据技能类型执行任务 (调用 LLM API / 运行工具 / 本地处理)
3. 返回执行结果

执行策略:
- code_generation: 调用外部 LLM API 生成代码
- code_review: 调用外部 LLM API 审查代码
- document_summary: 调用外部 LLM API 生成摘要
- rag_search: 本地知识库检索 (预留)
- shell_exec: 执行 Shell 命令
- file_ops: 文件读写
- monitoring: 采集系统资源

LLM API 调用通过环境变量配置 (OPENAI_API_KEY / DEEPSEEK_API_KEY 等)。
"""
import os
import shutil
import subprocess
import threading
import time
import json as _json
from pathlib import Path
from typing import Optional

import requests

from .logger import get_logger

logger = get_logger("agent_runtime")


def _build_react_rules_prompt(cwd: str) -> str:
    """构造 ReAct 循环的规则段 system prompt。

    BUG-038: 仅把 cwd 传给工具还不够 —— 模型看不到作业目录就仍会自由探索,
    M1 实测的「后期脱离目标项目 + 耗尽 30 轮」正是提示缺位所致。故显式声明
    相对路径基准、禁止跨盘漫游、禁止靠试探消耗轮次。

    Args:
        cwd: 子 Agent 作业目录

    Returns:
        追加到 system prompt 末尾的规则文本
    """
    return (
        "\n\n你是一个自主执行 Agent。你可以使用提供的工具来完成任务。\n"
        "规则:\n"
        "- 直接执行, 不要反问\n"
        "- 每次只调用一个工具, 观察结果后决定下一步\n"
        "- 任务完成后直接输出最终结果 (不再调用工具)\n"
        "- 如果工具执行失败, 尝试替代方案\n"
        f"- 工作目录 (相对路径均以此为基准): {cwd}\n"
        f"- 探索代码请从该目录开始 (如 dir_list 传 path='{cwd}'); "
        "任务描述中提到的项目路径即该目录, 不要去其他盘符或上级目录漫游\n"
        "- 若该目录内容与任务描述不符, 直接说明并结束, 不要靠反复试探消耗轮次\n"
        "- 禁止: 不要执行进度上报/curl/HTTP POST 到任何 progress-report 端点, "
        "框架会自动处理\n"
        "- 禁止: 不要浪费轮次做网络请求上报状态, 专注于任务本身\n"
        "- 工具返回 timed_out=true 时不要原样重试, 应缩小范围或直接说明超时原因\n"
        "- git_safe_directory_denied=true 时禁止修改 git config 或重试 Git 命令, "
        "改用 file_read/dir_list 只读分析\n"
    )


def resolve_cli_backend(requested: str) -> tuple:
    """解析本次调用应使用的 CLI Agent 后端 (iter-101, BUG-039 根因2)。

    原实现把「拼错的后端名」与「未指定」同等对待, 静默回退到首选后端并真的
    开跑 —— 实测 `backend="nonexistent_backend_zz"` 拉起 claude 跑了 180s,
    调用方拿不到任何提示。未知后端名一律显式失败。

    Args:
        requested: 调用方指定的后端名 (可为空表示自动选择)

    Returns:
        `(backend, error_result)`; `error_result` 非空时表示应直接返回它
    """
    backend = str(requested or "").lower().strip()
    if backend and backend not in CLI_AGENT_BACKENDS:
        return "", {
            "error": (f"未知的 CLI Agent 后端: '{backend}' "
                      f"(支持: {sorted(CLI_AGENT_BACKENDS)})"),
            "status": "failed",
            "backend": backend,
        }
    if backend:
        if not _which_with_fallback(CLI_AGENT_BACKENDS[backend]["detect"]):
            return "", {
                "error": (f"CLI Agent '{backend}' 未安装 "
                          f"(which {CLI_AGENT_BACKENDS[backend]['detect']} 未找到)"),
                "status": "failed",
                "backend": backend,
            }
        return backend, None
    backend = get_preferred_cli_agent()
    if not backend:
        hint = ("请安装: npm install -g @anthropic-ai/claude-code "
                "或 pip install aider-chat")
        return "", {
            "error": f"未检测到可用的 CLI Agent。{hint}",
            "hint": "支持: claude (Claude Code), codex (OpenAI), aider",
            "status": "failed",
        }
    return backend, None


def infer_result_status(result) -> tuple:
    """从技能处理器返回值推断子任务成败 (iter-101, BUG-039 根因3)。

    `AgentRuntime.execute` 原先只要 handler 未抛异常就一律记 `completed`,
    而各 handler 表达失败的方式是**返回** `{"error": ...}` 或
    `{"status": "failed"|"timeout"}` —— 于是护栏拒绝、CLI 非零退出、执行
    超时全部被上报为成功: PM 侧 `handle_subagent_failure` 的重试与升级链
    永不触发, 交付物是错的却显示一切正常。

    Args:
        result: handler 返回值 (通常为 dict)

    Returns:
        `(status, error)` 二元组; status 取 `completed` / `failed`
    """
    if not isinstance(result, dict):
        return "completed", ""
    inner = str(result.get("status", "") or "").strip().lower()
    if inner in ("failed", "timeout", "error", "cancelled"):
        err = str(result.get("error", "") or result.get("stderr", "") or "")
        return ("cancelled" if inner == "cancelled" else "failed"), err
    err = str(result.get("error", "") or "")
    if err.strip():
        return "failed", err
    return "completed", ""


def _resolve_tool_path(cwd: str, raw_path: str) -> str:
    """BUG-038: 把 file_read/file_write 的相对路径锚定到子 Agent 作业目录。

    这两个工具不接受 cwd 参数, 相对路径原本按 Station 进程的启动目录解析
    (即本仓库), 既读不到目标项目的文件, 也有越界写入风险。绝对路径与
    ``~`` 展开后为绝对的路径原样返回; cwd 为空时不做改写。

    Args:
        cwd: 子 Agent 作业目录 (可为空)
        raw_path: 工具调用里给出的原始路径

    Returns:
        解析后的路径 (无需改写时原样返回)
    """
    path = str(raw_path or "")
    if not path or not cwd:
        return path
    if os.path.isabs(os.path.expanduser(path)):
        return path
    return os.path.join(cwd, path)


_env_lock = threading.Lock()
_env_loaded = False


def _ensure_env_loaded():
    """确保 API Key 环境变量已加载。

    尝试从常见路径加载 .env 文件补齐缺失的 Key (override=False 幂等);
    解决: 进程从非项目根目录启动时 load_dotenv() 找不到 .env 的问题。
    线程安全: 使用 Lock + 标志位防止多线程重复加载。
    """
    global _env_loaded
    if _env_loaded:
        return
    # iter-55: 加入 ARK_API_KEY; 不再因「部分 key 已有值」提前 return —
    # 否则仅有 aliyun key 的环境会跳过 .env 加载导致 ark key 缺失
    key_envs = ["ARK_API_KEY", "ALIYUN_TOKENPLAN_API_KEY", "DEEPSEEK_API_KEY",
                "OPENAI_API_KEY", "ANTHROPIC_API_KEY", "QWEN_API_KEY"]
    with _env_lock:
        if _env_loaded:
            return  # 另一线程已完成加载
        # 尝试从多个路径加载 .env
        candidates = [
            Path(__file__).parent.parent / ".env",   # 项目根目录
            Path.cwd() / ".env",                      # 当前工作目录
            Path.home() / ".lan_mesh" / ".env",       # 用户数据目录
        ]
        for p in candidates:
            if p.is_file():
                try:
                    from dotenv import load_dotenv
                    load_dotenv(p, override=False)
                    logger.debug("已从 %s 加载 .env 环境变量", p)
                except ImportError:
                    # 手动解析 .env (dotenv 依赖缺失时兜底)
                    for line in p.read_text(encoding="utf-8").splitlines():
                        line = line.strip()
                        if not line or line.startswith("#"):
                            continue
                        if "=" in line:
                            k, v = line.split("=", 1)
                            k, v = k.strip(), v.strip()
                            if k and v and not os.environ.get(k):
                                os.environ[k] = v
                except Exception as e:
                    logger.debug("加载 %s 失败: %s", p, e)
        _env_loaded = True  # 标记已尝试, 避免反复扫描文件系统
        missing = [k for k in key_envs if not os.environ.get(k)]
        if missing:
            logger.debug("API Key 环境变量缺失: %s", ", ".join(missing))


# 模块加载时立即检查
_ensure_env_loaded()


# ── 安全沙箱配置 ────────────────────────────────────────────

# 禁止执行的危险命令前缀/关键词
SHELL_BLOCKED_PATTERNS = [
    "rm -rf /", "rm -rf ~", "rm -rf .",
    "mkfs", "dd if=", "fdisk", "format",
    "shutdown", "reboot", "halt", "poweroff",
    "chmod 777 /", "chown",
    ":(){ :|:& };:",  # fork bomb
    "wget", "curl",  # 禁止网络下载
    "sudo", "su ",
    "> /dev/", "> /etc/", "> /usr/",
    "iptables", "systemctl", "service ",
]

# ── 自举护栏 (iter-70) ──────────────────────────────────────
# 背景: _handle_cli_agent 会拉起 claude/codex/aider 的全自动模式, 其子进程
# 不受本模块 SHELL_BLOCKED_PATTERNS / file_ops 路径校验约束 — 等于沙箱逃逸口。
# 若放任 cwd 指向本仓库, Agent 可改写自身安全策略或门禁, 形成不可控自举。

# 禁止 CLI Agent 直接改写的文件 (自身安全策略 / 门禁 / 推送通道)
SELF_MOD_FORBIDDEN = [
    "lan_mesh/agent_runtime.py",   # 本模块: 黑名单与护栏自身
    "lan_mesh/shadow_dev.py",      # 影子模式隔离与门禁
    "tests/test_shadow_dev.py",    # 护栏回归测试
    "AGENTS.md",                   # Agent 硬约束
    "AGENT_LOCKS.md",              # 多 Agent 协作锁
    ".githooks/",                  # 9 项上库门禁
    "scripts/dev_status.py",       # 协作态势与归属检查
    "scripts/sync_docs.py",        # 文档清单门禁
    "scripts/sync_push.ps1",       # 推送唯一入口
    "scripts/ship.ps1",            # 发货通道
    ".git/",                       # 版本库内部
]

# 允许 CLI Agent 工作的根目录白名单 (环境变量可追加, os.pathsep 分隔)
# 默认只允许 shared_folder 与影子副本目录; 主仓库需显式放行。
CLI_AGENT_ALLOWED_ROOTS = [
    r for r in os.environ.get("CLI_AGENT_ALLOWED_ROOTS", "").split(os.pathsep) if r
]

# 显式停用的 CLI 后端 (逗号分隔; iter-101, BUG-039 根因4)
# detect_cli_agents 只检查可执行文件是否存在, 无法判断「装了但未登录/未配置」
# —— 实测本机 claude 与 aider 均可 --version 成功 (rc=0) 却在真实调用时
# 跑满 180s 后 rc=1。凭据探测既慢又不可靠, 故改为显式声明不可用的后端。
CLI_AGENT_DISABLED_BACKENDS = [
    name.strip().lower()
    for name in os.environ.get("CLI_AGENT_DISABLED_BACKENDS", "").split(",")
    if name.strip()
]

# 显式开关: 置 1 才允许 CLI Agent 在主仓库内直接作业 (默认禁止, 走影子模式)
CLI_AGENT_ALLOW_SELF_REPO = os.environ.get("CLI_AGENT_ALLOW_SELF_REPO", "") == "1"

# 需要额外透传给 CLI 子进程的环境变量 (逗号分隔; 默认不透传业务密钥)
CLI_AGENT_PASSTHROUGH_ENV = [
    key.strip()
    for key in os.environ.get("CLI_AGENT_PASSTHROUGH_ENV", "").split(",")
    if key.strip()
]


def _repo_root() -> str:
    """本项目仓库根目录绝对路径 (agent_runtime.py 的上一级)。"""
    return os.path.realpath(os.path.join(os.path.dirname(__file__), ".."))


def validate_cli_agent_cwd(
        cwd: str,
        allowed_roots: Optional[list[str]] = None) -> Optional[str]:
    """校验 CLI Agent 工作目录是否安全。

    返回 None 表示放行; 返回字符串表示拒绝原因。

    规则:
      1. 目录必须存在;
      2. 落在主仓库内 → 默认拒绝 (需 CLI_AGENT_ALLOW_SELF_REPO=1 显式放行),
         避免 Agent 改写自身安全策略造成不可控自举;
      3. 必须落在显式传入根目录或 CLI_AGENT_ALLOWED_ROOTS 白名单内。
    """
    if not cwd:
        return "未指定工作目录 (cwd)"
    resolved = os.path.realpath(os.path.expanduser(cwd))
    if not os.path.isdir(resolved):
        return f"工作目录不存在: {cwd}"

    repo = _repo_root()
    in_repo = resolved == repo or resolved.startswith(repo + os.sep)
    if in_repo and not CLI_AGENT_ALLOW_SELF_REPO:
        return (f"[自举护栏] 拒绝在主仓库内运行 CLI Agent: {resolved}。"
                f" 请使用影子模式 (lan_mesh/shadow_dev.py) 在副本上作业,"
                f" 或显式设置 CLI_AGENT_ALLOW_SELF_REPO=1 承担风险")

    if in_repo and CLI_AGENT_ALLOW_SELF_REPO:
        return None

    roots = allowed_roots if allowed_roots is not None else CLI_AGENT_ALLOWED_ROOTS
    resolved_roots = [
        os.path.realpath(os.path.expanduser(root))
        for root in roots if root
    ]
    if not resolved_roots:
        return "[自举护栏] 未配置 CLI Agent 允许根目录"
    ok = any(
        resolved == root or resolved.startswith(root + os.sep)
        for root in resolved_roots
    )
    if not ok:
        # iter-101: 原提示只列白名单, 未告知如何合法放行 —— iter-100 起 PM
        # 会把业务项目目录注入 cwd, 运维看到拒绝却不知该配哪个变量。
        return (f"[自举护栏] 工作目录不在白名单内: {resolved}"
                f" (允许: {resolved_roots})。"
                f" 如需放行业务项目目录, 请在 .env 配置"
                f" CLI_AGENT_ALLOWED_ROOTS (os.pathsep 分隔的根目录清单)")
    return None


def check_self_mod_violations(changed_files: list[str]) -> list[str]:
    """检查 CLI Agent 变更列表是否触碰护栏/门禁文件。

    用于影子模式产出 diff 后的复核: 即使在副本中, 也不接受 Agent 改写
    自身安全策略、协作锁或上库门禁。
    """
    violations: list[str] = []
    for changed in changed_files:
        rel = changed.replace("\\", "/").strip()
        if rel.startswith("./"):
            rel = rel[2:]
        for forbidden in SELF_MOD_FORBIDDEN:
            prefix = forbidden.rstrip("/")
            if rel == prefix or (forbidden.endswith("/") and rel.startswith(prefix + "/")):
                if forbidden not in violations:
                    violations.append(forbidden)
    return violations


# 文件操作允许的最大文件大小 (10MB)
MAX_FILE_SIZE = 10 * 1024 * 1024

# Shell 输出最大长度 (100KB)
MAX_OUTPUT_LENGTH = 100 * 1024


# ── Provider 默认配置 (provider → base_url) ──────────────────────

PROVIDER_CONFIG = {
    # iter-55: 火山方舟放首位 — 订阅制 Coding Plan 优先消耗, 且是 default_model 所在 provider
    "volcengine-ark":    {"base_url": "https://ark.cn-beijing.volces.com/api/coding/v3", "api_key_env": "ARK_API_KEY"},
    "deepseek":          {"base_url": "https://api.deepseek.com/v1", "api_key_env": "DEEPSEEK_API_KEY"},
    "openai":            {"base_url": "https://api.openai.com/v1", "api_key_env": "OPENAI_API_KEY"},
    "anthropic":         {"base_url": "https://api.anthropic.com/v1", "api_key_env": "ANTHROPIC_API_KEY"},
    "qwen":              {"base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1", "api_key_env": "QWEN_API_KEY"},
    "aliyun-tokenplan":  {"base_url": "https://token-plan.cn-beijing.maas.aliyuncs.com/compatible-mode/v1", "api_key_env": "ALIYUN_TOKENPLAN_API_KEY"},
}


def _load_model_pool_entries():
    """惰性加载 model_pool.yaml, 返回 {model_id: ModelEntryConfig} 字典。"""
    try:
        from .config import load_model_pool
        pool = load_model_pool()
        return {e.id: e for e in pool.models}
    except Exception:
        return {}


# ── CLI Agent 配置 ────────────────────────────────────────────

# 阿里 Token Plan 端点 (同时支持 OpenAI 和 Anthropic 协议)
TOKEN_PLAN_OPENAI_BASE = "https://token-plan.cn-beijing.maas.aliyuncs.com/compatible-mode/v1"
TOKEN_PLAN_ANTHROPIC_BASE = "https://token-plan.cn-beijing.maas.aliyuncs.com/apps/anthropic"

# 支持的 CLI Agent 后端 (name → 检测命令 + 调用模板)
CLI_AGENT_BACKENDS = {
    "claude": {
        "detect": "claude",
        "build_cmd": lambda prompt, cwd: [
            "claude", "--print", "--output-format", "json",
            "--model", "qwen3.8-max",
            "--max-turns", "30", prompt,
        ],
        "timeout": 600,
        "description": "Claude Code CLI — 自主编码 Agent (Token Plan qwen3.8-max)",
    },
    "aider": {
        "detect": "aider",
        "build_cmd": lambda prompt, cwd: [
            "aider", "--yes", "--no-auto-commits",
            "--model", "openai/qwen3.8-max",
            "--architect-model", "openai/qwen3.6-flash",
            "--message", prompt,
        ],
        "timeout": 600,
        "description": "Aider — Git-aware 双模型编码 Agent (Token Plan 零成本)",
    },
    "codex": {
        "detect": "codex",
        # codex exec: 非交互模式 (旧 CLI 的 --quiet/--full-auto 已废弃)
        # -s workspace-write: 沙箱限制写入范围
        # -C cwd: 显式指定工作目录 (subprocess cwd 之外的双重保险)
        # --output-last-message: 单文件输出最后消息, 便于机器读取
        "build_cmd": lambda prompt, cwd: [
            "codex", "exec",
            "-s", "workspace-write",
            "-C", cwd,
            "--output-last-message", os.path.join(cwd, ".codex_last_message.txt"),
            prompt,
        ],
        "timeout": 600,
        "description": "OpenAI Codex CLI - 沙箱编码 Agent (exec 非交互模式)",
    },
}

# CLI Agent 默认超时 (s), 可通过环境变量覆盖
CLI_AGENT_TIMEOUT = int(os.environ.get("CLI_AGENT_TIMEOUT", "600"))
# CLI Agent 输出截断限制
CLI_AGENT_MAX_OUTPUT = 200 * 1024  # 200KB


def _build_cli_env(backend: str) -> dict:
    """构建 CLI Agent 最小执行环境, 自动注入所需后端凭据。

    策略:
    - 只透传进程启动/网络/CLI 认证所需变量, 避免 CLI 子进程拿到
      全部业务密钥、Git 凭据与无关配置;
    - claude: 注入 ANTHROPIC_API_KEY + ANTHROPIC_BASE_URL;
    - aider: 注入 OPENAI_API_KEY + OPENAI_API_BASE;
    - 额外变量必须通过 CLI_AGENT_PASSTHROUGH_ENV 显式声明;
    - 禁用 Git 全局/系统配置与凭据助手, 降低自主 Agent 误推送风险。
    """
    base_keys = {
        # Windows/Unix 进程与动态库基础
        "PATH", "PATHEXT", "SYSTEMROOT", "SYSTEMDRIVE", "COMSPEC",
        "TEMP", "TMP", "USERPROFILE", "HOME", "APPDATA", "LOCALAPPDATA",
        "PROGRAMFILES", "PROGRAMDATA", "LANG", "LC_ALL", "TERM",
        # 网络代理与证书
        "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "NO_PROXY",
        "http_proxy", "https_proxy", "all_proxy", "no_proxy",
        "SSL_CERT_FILE", "REQUESTS_CA_BUNDLE",
    }
    backend_keys = {
        "claude": {
            "ANTHROPIC_API_KEY", "ANTHROPIC_BASE_URL", "ANTHROPIC_AUTH_TOKEN",
            "CLAUDE_CODE_USE_BEDROCK", "CLAUDE_CODE_USE_VERTEX",
        },
        "codex": {"OPENAI_API_KEY", "OPENAI_BASE_URL", "CODEX_HOME"},
        "aider": {"OPENAI_API_KEY", "OPENAI_API_BASE", "OPENAI_BASE_URL"},
    }
    allowed_keys = base_keys | backend_keys.get(backend, set()) | set(CLI_AGENT_PASSTHROUGH_ENV)
    env = {
        key: value for key, value in os.environ.items()
        if key in allowed_keys
    }
    env["NO_COLOR"] = "1"
    # Windows CLI (codex/claude) 需要 HOME; 沙箱进程可能未继承 -> 用 USERPROFILE 补
    if not env.get("HOME") and env.get("USERPROFILE"):
        env["HOME"] = env["USERPROFILE"]
    # iter-101: codex 定位配置目录 (auth.json) 用的是 CODEX_HOME, 不认 HOME。
    # 环境净化把它滤掉后, 子进程报「Error finding codex home」直接 rc=1 ——
    # 未登录与找不到配置目录是两种失败, 此处显式兜底为 ~/.codex。
    if backend == "codex" and not env.get("CODEX_HOME"):
        codex_home = os.path.join(
            env.get("USERPROFILE") or env.get("HOME") or "", ".codex")
        if os.path.isdir(codex_home):
            env["CODEX_HOME"] = codex_home
    _harden_git_env(env)
    token_plan_key = os.environ.get("ALIYUN_TOKENPLAN_API_KEY", "")

    if not token_plan_key:
        return env

    if backend == "claude":
        # 仅在用户未配置原生 Anthropic Key 时注入 Token Plan
        if not env.get("ANTHROPIC_API_KEY"):
            env["ANTHROPIC_API_KEY"] = token_plan_key
            env["ANTHROPIC_BASE_URL"] = TOKEN_PLAN_ANTHROPIC_BASE
            logger.debug("[CLI Agent] claude 使用 Token Plan Anthropic 端点")

    elif backend == "aider":
        # 仅在用户未配置原生 OpenAI Key 时注入 Token Plan
        if not env.get("OPENAI_API_KEY"):
            env["OPENAI_API_KEY"] = token_plan_key
            env["OPENAI_API_BASE"] = TOKEN_PLAN_OPENAI_BASE
            logger.debug("[CLI Agent] aider 使用 Token Plan OpenAI 端点")

    return env


def _harden_git_env(env: dict) -> None:
    """移除 Git 凭据入口并禁用全局配置, 降低误推送风险。"""
    for key in (
            "GIT_ASKPASS", "SSH_ASKPASS", "GIT_SSH_COMMAND", "SSH_AUTH_SOCK",
            "GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE", "GIT_OBJECT_DIRECTORY",
    ):
        env.pop(key, None)
    env["GIT_TERMINAL_PROMPT"] = "0"
    env["GIT_CONFIG_NOSYSTEM"] = "1"
    env["GIT_CONFIG_GLOBAL"] = os.devnull


def _which_with_fallback(cmd: str) -> Optional[str]:
    """shutil.which 增强: PATH 找不到时回退常见安装目录。"""
    found = shutil.which(cmd)
    if found:
        return found
    # Windows 常见全局安装路径
    extra_dirs = [
        os.path.join(os.environ.get("APPDATA", ""), "npm"),
        r"C:\Program Files\nodejs",
        os.path.join(os.environ.get("LOCALAPPDATA", ""), "Programs", "nodejs"),
    ]
    for d in extra_dirs:
        if not d or not os.path.isdir(d):
            continue
        candidate = os.path.join(d, f"{cmd}.cmd" if os.name == "nt" else cmd)
        if os.path.isfile(candidate):
            return candidate
    return None


def detect_cli_agents() -> list[str]:
    """检测系统上可用的 CLI Agent 后端。

    Returns:
        可用的 CLI Agent 名称列表, 如 ["claude", "aider"]
    """
    available = []
    for name, cfg in CLI_AGENT_BACKENDS.items():
        if name in CLI_AGENT_DISABLED_BACKENDS:
            continue
        if _which_with_fallback(cfg["detect"]):
            available.append(name)
    return available


def get_preferred_cli_agent() -> Optional[str]:
    """获取首选 CLI Agent (环境变量 > 自动检测)。

    环境变量 CLI_AGENT_BACKEND 可指定: claude / aider / codex
    未设置时按优先级自动选择: claude > aider > codex
    (claude 和 aider 均可通过 Token Plan 零成本使用)
    """
    preferred = os.environ.get("CLI_AGENT_BACKEND", "").lower().strip()
    if preferred and preferred in CLI_AGENT_BACKENDS:
        if preferred in CLI_AGENT_DISABLED_BACKENDS:
            logger.warning("指定的 CLI Agent '%s' 已被 "
                           "CLI_AGENT_DISABLED_BACKENDS 停用, 回退自动检测",
                           preferred)
        elif _which_with_fallback(CLI_AGENT_BACKENDS[preferred]["detect"]):
            return preferred
        else:
            logger.warning("指定的 CLI Agent '%s' 未安装, 回退自动检测",
                           preferred)

    # 自动检测优先级: codex > claude > aider。codex 置首位是安全取向 ——
    # 它是唯一同时带沙箱 (-s workspace-write) 与显式工作目录 (-C cwd) 的
    # 后端, 契合自举护栏的意图。
    for name in ["codex", "claude", "aider"]:
        if name in CLI_AGENT_DISABLED_BACKENDS:
            continue
        if _which_with_fallback(CLI_AGENT_BACKENDS[name]["detect"]):
            return name
    return None


def _build_blueprint_prompt(blueprint_hint) -> str:
    """iter-89: 把 PM 下发的项目蓝图约束渲染为 system prompt 尾缀。

    Args:
        blueprint_hint: PM 注入的蓝图提示文本, 非字符串或空值一律忽略。

    Returns:
        可直接拼接到 system prompt 尾部的片段; 无蓝图时返回空串。
    """
    if not isinstance(blueprint_hint, str):
        return ""
    hint = blueprint_hint.strip()
    if not hint:
        return ""
    return (
        "\n\n## 项目蓝图约束 (来自项目蓝图工作台, 必须遵守)\n"
        f"{hint[:1200]}\n"
        "产出必须服务于上述目标与验收标准, 不得展开被列为非目标的方向。"
    )


from typing import Any

class AgentRuntime:
    """Worker 端 Agent 运行时。

    接收子任务,根据 required_skill 路由到对应的执行处理器。
    """

    def __init__(self, agent_id: str, shared_folder_path: str,
                 custom_system_prompt: str = "", resource_manager: object = None):
        self.agent_id = agent_id
        self._resource_manager = resource_manager
        self.shared_folder = shared_folder_path
        self._custom_system_prompt = custom_system_prompt  # PM 注入的定制 prompt
        self._cancel_event = threading.Event()
        # 优化3: 当前执行的技能类型 (供选择性加载)
        # 用线程局部变量: 并行分发场景下多个线程共享同一 runtime, 实例属性会互相覆盖
        self._local = threading.local()
        self._skills_cache_dir = Path.home() / ".lan_mesh" / "skills_cache"
        self._handlers = {
            "code_generation": self._handle_code_generation,
            "code_review": self._handle_code_review,
            "document_summary": self._handle_document_summary,
            "rag_search": self._handle_rag_search,
            "shell_exec": self._handle_shell_exec,
            "file_ops": self._handle_file_ops,
            "monitoring": self._handle_monitoring,
            "react_agent": self._handle_react_agent,  # F2.1: 工具循环 Agent
            "cli_agent": self._handle_cli_agent,      # CLI Agent (Claude Code/Codex/Aider)
        }

    def cancel(self) -> None:
        """Request cooperative cancellation for the current runtime task."""
        self._cancel_event.set()

    def _record_usage(self, model_id: str, input_tokens: int, output_tokens: int) -> dict:
        manager = getattr(self, '_resource_manager', None)
        if manager is not None:
            return manager.record_usage(model_id, input_tokens, output_tokens)
        from .model_resources import record_usage_global
        return record_usage_global(model_id, input_tokens, output_tokens)

    def reset_cancellation(self) -> None:
        """Clear cancellation state before starting a new runtime task."""
        self._cancel_event.clear()

    def is_cancelled(self) -> bool:
        """Return whether cancellation has been requested."""
        return self._cancel_event.is_set()

    def execute(self, subtask: dict) -> dict:
        return call_component(self, RuntimeExecutionService, 'execute', subtask=subtask)

    def _finalize_subtask(self, result, usage: dict, skill: str, trace_id, task_id: str, elapsed_ms: float) -> dict:
        return call_component(self, RuntimeExecutionService, '_finalize_subtask', result=result, usage=usage, skill=skill, trace_id=trace_id, task_id=task_id, elapsed_ms=elapsed_ms)

    # ── 技能处理器 ──────────────────────────────────────────────

    def _handle_code_generation(self, input_data: dict) -> dict:
        return call_component(self, RuntimeSkillService, '_handle_code_generation', input_data=input_data)

    def _handle_code_review(self, input_data: dict) -> dict:
        return call_component(self, RuntimeSkillService, '_handle_code_review', input_data=input_data)

    def _handle_document_summary(self, input_data: dict) -> dict:
        return call_component(self, RuntimeSkillService, '_handle_document_summary', input_data=input_data)

    def _handle_rag_search(self, input_data: dict) -> dict:
        return call_component(self, RuntimeSkillService, '_handle_rag_search', input_data=input_data)

    def _handle_shell_exec(self, input_data: dict) -> dict:
        return call_component(self, RuntimeSkillService, '_handle_shell_exec', input_data=input_data)

    def _handle_file_ops(self, input_data: dict) -> dict:
        return call_component(self, RuntimeSkillService, '_handle_file_ops', input_data=input_data)

    def _handle_monitoring(self, input_data: dict) -> dict:
        return call_component(self, RuntimeSkillService, '_handle_monitoring', input_data=input_data)

    # ── F2.1: ReAct 工具循环 Agent ─────────────────────────────

    def _handle_react_agent(self, input_data: dict) -> dict:
        return call_component(self, RuntimeReactService, '_handle_react_agent', input_data=input_data)

    def _build_openai_tools_schema(self, registry) -> list[dict]:
        return call_component(self, RuntimeReactService, '_build_openai_tools_schema', registry=registry)

    # ── CLI Agent 执行器 (Claude Code / Codex / Aider) ─────────────

    def _handle_cli_agent(self, input_data: dict) -> dict:
        return call_component(self, RuntimeCliService, '_handle_cli_agent', input_data=input_data)

    def _parse_cli_output(self, backend: str, stdout: str, stderr: str, returncode: int) -> dict:
        return call_component(self, RuntimeCliService, '_parse_cli_output', backend=backend, stdout=stdout, stderr=stderr, returncode=returncode)

    def list_cli_agents(self) -> dict:
        return call_component(self, RuntimeCliService, 'list_cli_agents')

    def _call_llm_messages_with_tools(self, messages: list[dict], tools: list[dict], input_data: dict) -> dict:
        return call_component(self, RuntimeLlmService, '_call_llm_messages_with_tools', messages=messages, tools=tools, input_data=input_data)

    def _call_openai_with_tools(self, messages: list[dict], tools: list[dict], model_id: str, base_url: str, api_key: str) -> dict:
        return call_component(self, RuntimeLlmService, '_call_openai_with_tools', messages=messages, tools=tools, model_id=model_id, base_url=base_url, api_key=api_key)

    # ── LLM API 调用 (支持多 Provider + 降级链重试) ──────────────────

    def set_custom_prompt(self, prompt: str) -> Any:
        return call_component(self, RuntimePromptService, 'set_custom_prompt', prompt=prompt)

    def _build_system_prompt(self, task_context: str='') -> str:
        return call_component(self, RuntimePromptService, '_build_system_prompt', task_context=task_context)

    def _call_llm_with_routing(self, prompt: str, input_data: dict) -> dict:
        return call_component(self, RuntimeLlmService, '_call_llm_with_routing', prompt=prompt, input_data=input_data)

    def _resolve_provider(self, model_id: str) -> Optional[dict]:
        return call_component(self, RuntimeLlmService, '_resolve_provider', model_id=model_id)

    def _call_openai_compatible(self, prompt: str, model_id: str, base_url: str, api_key: str, system_prompt: str='') -> dict:
        return call_component(self, RuntimeLlmService, '_call_openai_compatible', prompt=prompt, model_id=model_id, base_url=base_url, api_key=api_key, system_prompt=system_prompt)

    def _call_llm_full(self, prompt: str, system_prompt: str='') -> dict:
        return call_component(self, RuntimeLlmService, '_call_llm_full', prompt=prompt, system_prompt=system_prompt)

    def _get_provider_models(self, provider: str) -> list:
        return call_component(self, RuntimeLlmService, '_get_provider_models', provider=provider)

    def _get_default_model(self, provider: str) -> str:
        return call_component(self, RuntimeLlmService, '_get_default_model', provider=provider)

    def _call_llm(self, prompt: str) -> str:
        return call_component(self, RuntimeLlmService, '_call_llm', prompt=prompt)


from typing import Any
from .component_service import call_component
from .runtime_execution_service import RuntimeExecutionService
from .runtime_skill_service import RuntimeSkillService
from .runtime_react_service import RuntimeReactService
from .runtime_cli_service import RuntimeCliService
from .runtime_llm_service import RuntimeLlmService
from .runtime_prompt_service import RuntimePromptService
