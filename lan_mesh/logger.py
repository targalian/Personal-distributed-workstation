"""
LAN Mesh 结构化日志系统

职责:
1. 统一日志格式: [时间] [级别] [模块] 消息
2. 支持控制台 + 文件双输出
3. 文件日志自动轮转 (默认 5MB × 3 个备份)
4. 兼容已有 print 输出 (渐进式迁移)

用法:
    from .logger import get_logger
    logger = get_logger("station")
    logger.info("任务已创建: %s", task_id)
    logger.error("聚合失败", exc_info=True)

配置:
    环境变量 LAN_MESH_LOG_LEVEL: 日志级别 (DEBUG/INFO/WARNING/ERROR)
    环境变量 LAN_MESH_LOG_FILE: 日志文件路径 (默认 ~/.lan_mesh/lan_mesh.log)
    环境变量 LAN_MESH_LOG_CONSOLE: 是否输出到控制台 (1/0, 默认 1)
"""
import logging
import os
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path


# ── 默认配置 ──────────────────────────────────────────────────

DEFAULT_LOG_LEVEL = "INFO"
DEFAULT_LOG_FILE = ""  # 空表示使用 ~/.lan_mesh/lan_mesh.log
DEFAULT_MAX_BYTES = 5 * 1024 * 1024  # 5MB
DEFAULT_BACKUP_COUNT = 3
DEFAULT_FORMAT = "[%(asctime)s] [%(levelname)s] [%(name)s] %(message)s"
DEFAULT_DATE_FORMAT = "%Y-%m-%d %H:%M:%S"

# 模块名 → 日志前缀映射 (兼容已有 print 风格)
_MODULE_PREFIXES = {
    "station": "Station",
    "pm": "PM",
    "worker": "Worker",
    "secretary": "Secretary",
    "bot": "BotGateway",
    "chat": "ChatHandler",
    "db": "Database",
    "api": "API",
    "discovery": "Discovery",
    "orchestrator": "Orchestrator",
    "model": "ModelRouter",
    "mcp": "MCP",
    "sync": "CloudSync",
}

_initialized = False


def _ensure_initialized():
    """确保日志系统已初始化 (仅执行一次)。"""
    global _initialized
    if _initialized:
        return
    _initialized = True

    # 根 logger 配置
    root = logging.getLogger("lan_mesh")
    root.setLevel(_get_level())

    # 避免重复添加 handler
    if root.handlers:
        return

    formatter = logging.Formatter(DEFAULT_FORMAT, datefmt=DEFAULT_DATE_FORMAT)

    # 控制台 handler
    if os.environ.get("LAN_MESH_LOG_CONSOLE", "1") != "0":
        console_handler = logging.StreamHandler(sys.stdout)
        console_handler.setLevel(_get_level())
        console_handler.setFormatter(formatter)
        root.addHandler(console_handler)

    # 文件 handler
    log_file = _get_log_file()
    if log_file:
        try:
            log_file.parent.mkdir(parents=True, exist_ok=True)
            file_handler = RotatingFileHandler(
                str(log_file),
                maxBytes=DEFAULT_MAX_BYTES,
                backupCount=DEFAULT_BACKUP_COUNT,
                encoding="utf-8",
            )
            file_handler.setLevel(logging.DEBUG)  # 文件记录所有级别
            file_handler.setFormatter(formatter)
            root.addHandler(file_handler)
        except Exception as e:
            print(f"[Logger] 无法创建日志文件 {log_file}: {e}")


def _get_level() -> int:
    """从环境变量获取日志级别。"""
    level_str = os.environ.get("LAN_MESH_LOG_LEVEL", DEFAULT_LOG_LEVEL).upper()
    return getattr(logging, level_str, logging.INFO)


def _get_log_file() -> Path:
    """获取日志文件路径。"""
    env_path = os.environ.get("LAN_MESH_LOG_FILE", DEFAULT_LOG_FILE)
    if env_path:
        return Path(env_path).expanduser()
    return Path.home() / ".lan_mesh" / "lan_mesh.log"


def get_logger(module_name: str) -> logging.Logger:
    """获取模块专用 logger。

    Args:
        module_name: 模块简称 (如 "station", "pm", "bot")

    Returns:
        配置好的 Logger 实例

    用法:
        logger = get_logger("station")
        logger.info("任务已创建: %s", task_id)
    """
    _ensure_initialized()
    # 使用 lan_mesh.模块名 作为 logger 名称
    return logging.getLogger(f"lan_mesh.{module_name}")


# ── 便捷函数 (兼容 print 风格) ────────────────────────────────

def log_info(module: str, message: str, *args):
    """便捷 INFO 日志 (兼容 print 迁移)。"""
    get_logger(module).info(message, *args)


def log_error(module: str, message: str, *args, exc_info=False):
    """便捷 ERROR 日志。"""
    get_logger(module).error(message, *args, exc_info=exc_info)


def log_warning(module: str, message: str, *args):
    """便捷 WARNING 日志。"""
    get_logger(module).warning(message, *args)


def log_debug(module: str, message: str, *args):
    """便捷 DEBUG 日志。"""
    get_logger(module).debug(message, *args)
