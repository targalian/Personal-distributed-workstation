"""
配置管理 - 基于 Pydantic 的强类型配置校验

读取 config.yaml 与环境变量,提供全局配置访问。
"""
import os
from pathlib import Path
from typing import Optional

import yaml
from pydantic import BaseModel, Field


class DiscoveryConfig(BaseModel):
    """UDP 发现配置。"""
    port: int = 45454
    presence_interval: int = 3
    device_ttl: int = 12


class WorkerConfig(BaseModel):
    """Worker 节点配置。"""
    api_port: int = 45460
    shared_folder: str = "~/lan_mesh_shared"
    device_name: str = ""           # 留空则自动使用 hostname


class MasterConfig(BaseModel):
    """Master 节点配置。"""
    api_port: int = 45470
    shared_folder: str = "~/lan_mesh_shared"
    device_name: str = ""
    db_path: str = "~/.lan_mesh/master.sqlite3"


class AppConfig(BaseModel):
    """应用顶层配置。"""
    discovery: DiscoveryConfig = Field(default_factory=DiscoveryConfig)
    worker: WorkerConfig = Field(default_factory=WorkerConfig)
    master: MasterConfig = Field(default_factory=MasterConfig)


def _expand(path_str: str) -> str:
    """展开路径中的 ~ 和环境变量。"""
    return os.path.expanduser(os.path.expandvars(path_str))


def load_config(config_path: Optional[str] = None) -> AppConfig:
    """加载 YAML 配置文件，不存在则返回默认配置。

    查找顺序:
    1. 显式指定的 config_path
    2. 环境变量 LAN_MESH_CONFIG
    3. ~/.lan_mesh/config.yaml
    4. ./config.yaml
    """
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
            with open(p, "r", encoding="utf-8") as f:
                raw = yaml.safe_load(f) or {}
            return AppConfig(**raw)

    return AppConfig()


def get_shared_folder(cfg: AppConfig, role: str) -> Path:
    """根据角色获取共享文件夹路径（展开 ~）。"""
    folder = cfg.master.shared_folder if role == "master" else cfg.worker.shared_folder
    return Path(_expand(folder))


def get_db_path(cfg: AppConfig) -> Path:
    """获取 Master SQLite 数据库路径。"""
    return Path(_expand(cfg.master.db_path))
