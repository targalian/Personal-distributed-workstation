"""
启动前自检模块 - 在程序启动时检查所有前置条件

检查项:
1. Python 版本 >= 3.10
2. 核心依赖是否安装 (psutil, fastapi, uvicorn, requests, yaml, pydantic)
3. 配置文件是否存在 (不存在则自动创建默认配置)
4. 数据目录是否可写
5. 共享文件夹路径是否有效且可写
6. 网络接口是否可用
7. UDP 发现端口是否可绑定
8. HTTP API 端口是否可用
9. Secretary 专属: 数据库路径目录是否可写
10. Secretary 专属: Web UI 模板是否存在

自检流程:
  run_preflight(role, cfg) → 打印检查报告 → 返回是否全部通过
"""
import importlib
import os
import platform
import shutil
import socket
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

import yaml


def _safe_print(msg=""):
    """安全打印: 遇到编码错误时回退到 ASCII。"""
    try:
        print(msg)
    except UnicodeEncodeError:
        # 替换 emoji/框线字符为 ASCII 等价物
        safe = msg
        safe = safe.replace("✅", "[OK]").replace("❌", "[X]").replace("⚠️", "[!]")
        safe = safe.replace("┌", "+").replace("┬", "+").replace("┐", "+")
        safe = safe.replace("├", "+").replace("┼", "+").replace("┤", "+")
        safe = safe.replace("└", "+").replace("┴", "+").replace("┘", "+")
        safe = safe.replace("─", "-").replace("│", "|")
        print(safe.encode("ascii", "replace").decode())

from .config import AppConfig, get_db_path, get_shared_folder
from .protocol import DISCOVERY_PORT


@dataclass
class CheckResult:
    """单项检查结果。"""
    name: str
    passed: bool
    detail: str
    critical: bool = True     # critical 失败则终止启动
    auto_fixed: bool = False  # 是否已自动修复


# ── 检查项实现 ──────────────────────────────────────────────────

def _check_python() -> CheckResult:
    """检查 Python 版本 >= 3.10。"""
    v = sys.version_info
    version_str = f"{v.major}.{v.minor}.{v.micro}"
    if v >= (3, 10):
        return CheckResult("Python 版本", True, f"{version_str} (>= 3.10)")
    return CheckResult("Python 版本", False, f"{version_str} — 需要 3.10+", critical=True)


def _check_dependencies() -> CheckResult:
    """检查核心依赖包是否安装。"""
    deps = ["psutil", "fastapi", "uvicorn", "requests", "yaml", "pydantic", "multipart"]
    missing = []
    for dep in deps:
        try:
            importlib.import_module(dep)
        except ImportError:
            missing.append(dep)

    if not missing:
        return CheckResult("核心依赖", True, f"{len(deps)} 个包已安装")
    return CheckResult(
        "核心依赖", False,
        f"缺失: {', '.join(missing)} — 运行: pip install -r requirements.txt",
        critical=True,
    )


def _check_config_file(config_path: Optional[str] = None) -> CheckResult:
    """检查配置文件是否存在，不存在则自动创建默认配置。"""
    candidates = []
    if config_path:
        candidates.append(Path(config_path))
    env_path = os.environ.get("LAN_MESH_CONFIG")
    if env_path:
        candidates.append(Path(env_path))
    candidates.append(Path.home() / ".lan_mesh" / "config.yaml")
    candidates.append(Path("config.yaml"))

    for p in candidates:
        if p.is_file():
            return CheckResult("配置文件", True, f"{p}", auto_fixed=False)

    # 自动创建默认配置
    target = Path("config.yaml")
    try:
        _write_default_config(target)
        return CheckResult("配置文件", True, f"已自动创建: {target}", auto_fixed=True)
    except OSError as e:
        return CheckResult("配置文件", False, f"不存在且无法创建: {e}", critical=True)


def _write_default_config(path: Path):
    """写入默认配置文件。"""
    default = {
        "discovery": {
            "port": 45454,
            "presence_interval": 3,
            "device_ttl": 12,
        },
        "worker": {
            "api_port": 45460,
            "shared_folder": "~/lan_mesh_shared",
            "device_name": "",
        },
        "secretary": {
            "api_port": 45470,
            "shared_folder": "~/lan_mesh_shared",
            "device_name": "",
            "db_path": "~/.lan_mesh/secretary.sqlite3",
        },
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        yaml.dump(default, f, default_flow_style=False, allow_unicode=True, sort_keys=False)


def _check_data_dir() -> CheckResult:
    """检查数据目录是否可写。"""
    data_dir = Path.home() / ".lan_mesh"
    try:
        data_dir.mkdir(parents=True, exist_ok=True)
        test_file = data_dir / ".write_test"
        test_file.write_text("ok", encoding="utf-8")
        test_file.unlink()
        return CheckResult("数据目录", True, f"{data_dir}")
    except OSError as e:
        return CheckResult("数据目录", False, f"{data_dir} 不可写: {e}", critical=True)


def _check_shared_folder(cfg: AppConfig, role: str) -> CheckResult:
    """检查共享文件夹路径是否有效且可写。"""
    folder = get_shared_folder(cfg, role)
    try:
        folder.mkdir(parents=True, exist_ok=True)
        # 测试写入
        test_file = folder / ".write_test"
        test_file.write_text("ok", encoding="utf-8")
        test_file.unlink()
        return CheckResult("共享文件夹", True, f"{folder}")
    except OSError as e:
        return CheckResult("共享文件夹", False, f"{folder} 不可写: {e}", critical=True)


def _check_network() -> CheckResult:
    """检查网络接口是否可用 (至少一个非回环 IPv4)。"""
    import psutil
    ips = []
    for name, addrs in psutil.net_if_addrs().items():
        if name.startswith(("lo",)):
            continue
        for addr in addrs:
            if addr.family == socket.AF_INET and not addr.address.startswith("127."):
                ips.append(f"{name}({addr.address})")

    if ips:
        return CheckResult("网络接口", True, f"{len(ips)} 个: {' '.join(ips[:3])}")
    return CheckResult("网络接口", False, "未找到可用的非回环网络接口", critical=True)


def _check_udp_port(port: int) -> CheckResult:
    """检查 UDP 发现端口是否可绑定。"""
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        sock.bind(("", port))
        sock.close()
        return CheckResult("UDP 发现端口", True, f":{port} 可用")
    except OSError as e:
        return CheckResult(
            "UDP 发现端口", False,
            f":{port} 被占用或无权限: {e}\n"
            f"  提示: 可能已有另一个 LAN Mesh 实例在运行",
            critical=True,
        )


def _check_api_port(port: int) -> CheckResult:
    """检查 HTTP API 端口是否可用 (允许被占用,因为会自动找下一个)。"""
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.bind(("", port))
        return CheckResult("HTTP API 端口", True, f":{port} 可用")
    except OSError:
        # 端口被占用,但程序会自动递增尝试 — 仅警告
        return CheckResult(
            "HTTP API 端口", True,
            f":{port} 已被占用,将自动尝试后续端口",
            critical=False,
        )


def _check_db_path(cfg: AppConfig) -> Optional[CheckResult]:
    """Secretary 专属: 检查数据库路径目录是否可写。"""
    db_path = get_db_path(cfg)
    db_dir = db_path.parent
    try:
        db_dir.mkdir(parents=True, exist_ok=True)
        test_file = db_dir / ".write_test"
        test_file.write_text("ok", encoding="utf-8")
        test_file.unlink()
        return CheckResult("数据库目录", True, f"{db_dir}")
    except OSError as e:
        return CheckResult("数据库目录", False, f"{db_dir} 不可写: {e}", critical=True)


def _check_web_template() -> Optional[CheckResult]:
    """Secretary 专属: 检查 Web UI 模板是否存在。"""
    template = Path(__file__).parent / "web" / "templates" / "dashboard.html"
    if template.is_file():
        size = template.stat().st_size
        return CheckResult("Web UI 模板", True, f"{template} ({size} bytes)")
    return CheckResult("Web UI 模板", False, f"未找到: {template}", critical=False)


# ── 主入口 ──────────────────────────────────────────────────────

def run_preflight(
    role: str,
    cfg: AppConfig,
    config_path: Optional[str] = None,
) -> bool:
    """执行启动前自检。

    打印格式化的检查报告,返回是否所有 critical 检查通过。
    如果检测到配置文件缺失,会自动创建默认配置。
    """
    checks = [
        _check_python(),
        _check_dependencies(),
        _check_config_file(config_path),
        _check_data_dir(),
        _check_shared_folder(cfg, role),
        _check_network(),
        _check_udp_port(cfg.discovery.port),
    ]

    api_port = cfg.secretary.api_port if role == "secretary" else cfg.worker.api_port
    checks.append(_check_api_port(api_port))

    if role == "secretary":
        checks.append(_check_db_path(cfg))
        checks.append(_check_web_template())

    # 打印报告
    role_label = "Secretary" if role == "secretary" else "Worker"
    _safe_print()
    _safe_print("┌─────────────────────────────────────────────┐")
    _safe_print(f"│  LAN Mesh {role_label} 启动自检")
    _safe_print("├─────────────────────────────────────────────┤")

    all_passed = True
    for c in checks:
        if c is None:
            continue
        if not c.passed:
            all_passed = False

        icon = "✅" if c.passed else ("⚠️" if not c.critical else "❌")
        suffix = " [已自动修复]" if c.auto_fixed else ""
        suffix += " [非致命]" if not c.passed and not c.critical else ""
        _safe_print(f"│  {icon} {c.name}")
        _safe_print(f"│     {c.detail}{suffix}")
        _safe_print("│")

    # 总结
    critical_failed = [c for c in checks if c and not c.passed and c.critical]
    if critical_failed:
        _safe_print(f"│  ❌ 自检未通过: {len(critical_failed)} 项致命检查失败")
        _safe_print("└─────────────────────────────────────────────┘")
        _safe_print()
        return False
    elif all_passed:
        _safe_print(f"│  ✅ 全部 {len([c for c in checks if c])} 项检查通过")
    else:
        warnings = [c for c in checks if c and not c.passed]
        _safe_print(f"│  ✅ 自检通过 ({len(warnings)} 项非致命警告)")
    _safe_print("└─────────────────────────────────────────────┘")
    _safe_print()

    return True
