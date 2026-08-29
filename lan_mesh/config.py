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


class SecretaryConfig(BaseModel):
    """Secretary 节点配置。"""
    api_port: int = 45470
    shared_folder: str = "~/lan_mesh_shared"
    device_name: str = ""
    db_path: str = "~/.lan_mesh/secretary.sqlite3"


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
    default_model: str = ""  # 全局默认模型 (留空则走自动路由)


# ── Bot 通道配置 (Phase: 手机交互) ─────────────────────────────


class BotChannelConfig(BaseModel):
    """单个 Bot 通道配置。"""
    channel_type: str = "wechat_webhook"   # wechat_webhook | telegram
    enabled: bool = False
    webhook_url: str = ""                 # 企业微信群机器人 webhook URL
    bot_token: str = ""                   # Telegram bot token
    chat_id: str = ""                     # Telegram chat_id
    webhook_url_base: str = ""           # Telegram webhook 公网回调地址 (可选)
    min_priority: str = "normal"          # low | normal | high


# ── 云存储配置 (跨主机共享文件夹同步) ────────────────────────────


class CloudStorageConfig(BaseModel):
    """云存储配置 — S3 兼容服务 (阿里云 OSS / MinIO / AWS S3)。

    密钥优先从环境变量读取:
      CLOUD_STORAGE_ACCESS_KEY  /  CLOUD_STORAGE_SECRET_KEY
    """
    enabled: bool = False
    endpoint: str = ""                 # S3 端点 (如 oss-cn-hangzhou.aliyuncs.com)
    bucket: str = "lan-mesh"           # 存储桶名称
    prefix: str = "shared/"            # 桶内路径前缀
    access_key: str = ""               # 访问密钥
    secret_key: str = ""               # 密钥
    region: str = ""                   # 区域 (如 us-east-1)
    secure: bool = True                # 是否使用 HTTPS
    auto_sync: bool = True             # 启动时自动同步
    sync_interval: int = 300           # 自动同步间隔 (秒), 0 = 仅启动时同步一次


class QuietHoursConfig(BaseModel):
    """免打扰时段配置。"""
    enabled: bool = False
    start: str = "23:00"              # 开始时间 (HH:MM)
    end: str = "08:00"                # 结束时间 (HH:MM)
    override_priority: str = "high"   # 此级别及以上可穿透免打扰


class BotConfig(BaseModel):
    """Bot 通道总配置。"""
    channels: list = Field(default_factory=list)  # List[BotChannelConfig]
    aggregate_window: int = 30        # 消息聚合窗口 (秒), 0=禁用聚合
    quiet_hours: QuietHoursConfig = Field(default_factory=QuietHoursConfig)
    max_retry: int = 3                # 发送失败最大重试次数
    retry_backoff: float = 2.0        # 重试退避基数 (秒)


class ObservabilityConfig(BaseModel):
    """可观测性配置 (iter-43: 任务停滞检测参数配置化; iter-50: 自动自愈守护; iter-54: 日志容量修剪; iter-57: API 限流双桶; iter-60: 自愈写动作配额)。"""
    stall_check_interval: float = 60.0   # 停滞检查周期 (秒, 实际最小 10)
    stall_minutes: float = 30.0          # 停滞判定阈值 (分钟), ≤0 禁用检测与告警
    auto_heal_enabled: bool = False      # 自动自愈守护开关 (默认关, 需手动开启)
    auto_heal_interval: float = 300.0    # 自动自愈扫描周期 (秒, 实际最小 30)
    auto_heal_cooldown: float = 600.0    # 同类别动作冷却期 (秒), 防执行风暴
    auto_heal_daily_limit: int = 3       # iter-60: 写动作 (rotate_key/switch_pool) 每类别每日自动执行上限, 0=禁止写动作自动执行
    log_retention_days: float = 30.0     # 日志表保留天数 (llm_call_log/chat_history 等), ≤0 禁用修剪
    log_prune_interval_hours: float = 24.0  # 日志修剪周期 (小时, 实际最小 1), ≤0 禁用
    log_vacuum: bool = True              # 修剪后执行 VACUUM 回收磁盘空间
    api_rate_limit: int = 120            # 严格桶限流 (req/min, 未认证请求), ≤0 禁用
    api_rate_limit_trusted: int = 1000   # 信任桶限流 (req/min, 合法 mesh token), ≤0 禁用


class FederationPeer(BaseModel):
    """F3.4 (iter-64): 跨网段联邦对端 — 静态配置的远端可达节点。

    UDP 发现无法跨越广播域, 联邦通过 HTTP 通道连接对端 Secretary/Station。
    port 为对端 API 端口 (0 = 使用本机 api_port)。
    """
    name: str = ""    # 联邦名 (如 office-a, 用于主机来源标记)
    host: str = ""    # 可达地址 (IP 或域名)
    port: int = 0


class FederationConfig(BaseModel):
    """F3.4 (iter-64): 跨网段多 Secretary 联邦 (静态 peer 联邦)。

    enabled=False 时不启动联邦轮询线程 (向后兼容); 联邦对端不参与
    本网段 Secretary 仲裁 (各网段保留自己的 Secretary, 联邦共存)。
    """
    enabled: bool = False
    interval: int = 15        # 轮询间隔 (秒)
    offline_after: int = 3    # 连续失败 N 次标记对端离线
    peers: list[FederationPeer] = []


class AppConfig(BaseModel):
    """应用顶层配置 (iter-61: 技能市场目录与包大小限制)。"""
    discovery: DiscoveryConfig = Field(default_factory=DiscoveryConfig)
    worker: WorkerConfig = Field(default_factory=WorkerConfig)
    secretary: SecretaryConfig = Field(default_factory=SecretaryConfig)
    bot: BotConfig = Field(default_factory=BotConfig)
    cloud_storage: CloudStorageConfig = Field(default_factory=CloudStorageConfig)
    security: "SecurityConfig" = Field(default_factory=lambda: SecurityConfig())
    observability: ObservabilityConfig = Field(default_factory=ObservabilityConfig)
    federation: FederationConfig = Field(default_factory=FederationConfig)
    auto_upgrade: bool = True  # F1: 版本落后时自动 git pull 对齐 (工作区脏则跳过)
    skill_market_dir: str = "skills_market"  # iter-61: 第三方技能市场目录 (相对项目根, 每个子目录为一个可安装包)
    skill_max_size_kb: int = 200  # iter-61: 单个技能包最大体积 (KB), 防止第三方内容注入超大文本


class UserAccount(BaseModel):
    """F5.2 (iter-58): 用户账号 — 多用户权限的最小配置单元。

    token 为用户个人凭证 (与节点级 mesh_token 独立), 持 token 的
    请求按 role 分级授权; 非法 role 值由 load 时归一到 viewer。
    """
    name: str = ""
    role: str = "viewer"   # boss | operator | viewer
    token: str = ""


class SecurityConfig(BaseModel):
    """安全配置。"""
    auth_enabled: bool = True    # 是否启用节点间 Token 认证 (P2 #5: 默认启用)
    mesh_token: str = ""        # 显式指定全网共享 token (留空则自动生成/持久化)
    # token 来源优先级: security.mesh_token > 环境变量 LAN_MESH_TOKEN > ~/.lan_mesh/mesh_token
    users: list[UserAccount] = []  # F5.2 (iter-58): 空列表 = 关闭多用户权限
    # (所有人持 mesh token 即 boss, 向后兼容); 非空时按 role 分级授权


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
    folder = cfg.secretary.shared_folder if role == "secretary" else cfg.worker.shared_folder
    return Path(_expand(folder))


def get_db_path(cfg: AppConfig) -> Path:
    """获取 Secretary SQLite 数据库路径。"""
    return Path(_expand(cfg.secretary.db_path))


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
            default_model = raw.get("default_model", "")
            return ModelPoolConfig(models=models, default_model=default_model)

    return ModelPoolConfig()
