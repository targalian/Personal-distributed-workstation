## 系统概述

LAN Mesh 采用 **Pydantic 强类型模型 + YAML 文件 + 环境变量** 的三层配置体系，支持运行时命令行参数覆盖。Python 后端与 Tauri 前端各自维护独立的配置子系统。

## 核心架构

### Python 后端配置（`lan_mesh/config.py`）

- **强类型模型**：使用 Pydantic `BaseModel` 定义 `DiscoveryConfig`、`WorkerConfig`、`SecretaryConfig`、`BotConfig`、`ModelPoolConfig` 等结构，提供默认值与类型校验
- **分层加载顺序**：
  - `config.yaml`：应用级配置，查找优先级为 `显式路径 > LAN_MESH_CONFIG 环境变量 > ~/.lan_mesh/config.yaml > ./config.yaml`
  - `model_pool.yaml`：模型池配置，查找优先级为 `显式路径 > LAN_MESH_MODEL_POOL 环境变量 > lan_mesh/model_pool.yaml > ./model_pool.yaml`
- **路径展开**：`~` 和用户环境变量通过 `_expand()` 统一处理
- **运行时覆盖**：`main.py` 中 argparse 参数直接修改已加载的配置对象（`cfg.secretary.api_port = args.port` 等）

### 独立采集脚本（`lan_mesh/collect_config.py`）

自包含工具，不依赖框架包，用于采集主机硬件/网络信息并输出 JSON/TXT 报告，可分发到共享文件夹供任意主机运行。

### Tauri 前端配置（`quicklan-main/src-tauri/src/settings.rs`）

- **JSON 持久化**：用户设置保存在 `settings.json`（由 `storage::config_dir()` 定位）
- **SettingsService**：基于 `Arc<Mutex<AppSettings>>` 的线程安全读写服务
- **默认值归一化**：`normalize_settings()` 确保空昵称回退到 hostname，下载目录为空时使用默认值
- **Tauri 构建配置**：`tauri.conf.json` 管理窗口尺寸、打包目标、图标等资源

## 关键约定

1. **配置文件命名**：应用配置用 `config.yaml`，模型池用 `model_pool.yaml`，前端设置用 `settings.json`
2. **环境变量前缀**：`LAN_MESH_*` 命名空间（如 `LAN_MESH_CONFIG`、`LAN_MESH_MODEL_POOL`）
3. **敏感信息隔离**：API Key 通过 `api_key_env` 字段引用环境变量名，不在配置文件中明文存储
4. **向后兼容**：`secretary` 角色保留以兼容旧部署方式，推荐入口为 `station`
5. **配置即代码**：所有配置项在 Pydantic 模型中声明，新增配置需同步更新模型定义

## 开发者规则

- 新增配置项必须在对应 Pydantic 模型中添加字段并提供合理默认值
- 敏感凭据一律通过环境变量注入，禁止硬编码或写入配置文件
- 路径配置统一使用 `~/` 或绝对路径，避免相对路径歧义
- 前端设置变更必须通过 `SettingsService.update_*` 方法触发持久化