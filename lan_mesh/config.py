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


# ── 模型池配置 (Phase 2: 模型路由器) ─────────────────────────────


class ModelEntryConfig(BaseModel):
    """模型池条目配置 — 对应 model_pool.yaml 中每个模型定义。"""
    id: str                           # 模型 ID (e.g. deepseek-chat)
    provider: str                     # 厂商 (openai / deepseek / anthropic / qwen)
    api_key_env: str                  # API Key 环境变量名
    base_url: str = ""                # API Base URL
    cost_input_per_1k: float = 0.0   # 输入成本 (美元 / 1K tokens)
    cost_output_per_1k: float = 0.0  # 输出成本 (美元 / 1K tokens)
    capabilities: list = Field(default_factory=list)  # 能力标签 [reasoning, coding, ...]
    quality_score: float = 0.5        # 能力基准分 (0~1)
    speed_score: float = 0.5          # 响应速度分 (0~1)
    rate_limit_rpm: int = 100         # 请求速率限制 (RPM)
    max_context_tokens: int = 32768   # 最大上下文窗口
    fallback: list = Field(default_factory=list)  # 降级链 [model_id, ...]


class ModelPoolConfig(BaseModel):
    """模型池配置集合。"""
    models: list = Field(default_factory=list)  # List[ModelEntryConfig]


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


def load_model_pool(config_path: Optional[str] = None) -> ModelPoolConfig:
    """加载模型池配置 (model_pool.yaml)。

    查找顺序:
    1. 显式指定的 config_path
    2. 环境变量 LAN_MESH_MODEL_POOL
    3. lan_mesh/ 包目录下的 model_pool.yaml
    4. ./model_pool.yaml
    5. 返回空配置 (无模型)
    """
    candidates = []
    if config_path:
        candidates.append(Path(config_path))
    env_path = os.environ.get("LAN_MESH_MODEL_POOL")
    if env_path:
        candidates.append(Path(env_path))
    # 包目录下 (lan_mesh/model_pool.yaml)
    pkg_dir = Path(__file__).parent
    candidates.append(pkg_dir / "model_pool.yaml")
    candidates.append(Path("model_pool.yaml"))

    for p in candidates:
        if p.is_file():
            with open(p, "r", encoding="utf-8") as f:
                raw = yaml.safe_load(f) or {}
            models = [ModelEntryConfig(**m) for m in raw.get("models", [])]
            return ModelPoolConfig(models=models)

    return ModelPoolConfig()
