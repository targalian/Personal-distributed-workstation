该仓库采用分层、多源且强类型的配置管理策略，主要服务于 Python 后端（LAN Mesh）和 Rust 前端（QuickLAN）两个子系统。

### 1. 核心系统与模式
- **Python (LAN Mesh)**: 采用 **Pydantic** 进行强类型数据校验与建模。配置加载遵循“约定优于配置”原则，支持 YAML 文件与环境变量的混合读取。系统通过 `load_config` 函数实现多级回退机制，确保在不同部署环境下均能获取有效配置。
- **Rust (QuickLAN)**: 采用 **Serde** 进行 JSON 序列化/反序列化。用户偏好设置（如昵称、下载路径）存储在本地持久化文件中，并通过 `SettingsService` 提供线程安全的读写访问。

### 2. 关键配置文件与逻辑
- **入口配置**: `config.yaml` 位于项目根目录，定义了 UDP 发现端口、Worker/Secretary 节点的 API 端口及共享文件夹路径等基础设施参数。
- **模型池配置**: `lan_mesh/model_pool.yaml` 管理多智能体协作所需的 LLM 模型元数据（包括 Provider、API Key 环境变量名、成本、能力评分及降级链）。
- **加载逻辑**: `lan_mesh/config.py` 是配置系统的核心，定义了 `AppConfig`、`DiscoveryConfig` 等 Pydantic 模型，并实现了从显式路径、环境变量 (`LAN_MESH_CONFIG`)、用户主目录到当前目录的查找顺序。
- **运行时覆盖**: `main.py` 作为统一入口，允许通过 CLI 参数（如 `--port`, `--name`）动态覆盖 YAML 中的默认值，实现了配置的灵活注入。

### 3. 架构约定与设计决策
- **路径展开**: 所有涉及文件系统的路径配置（如 `shared_folder`, `db_path`）均通过 `_expand` 函数自动处理 `~` 和环境变量，增强了跨平台兼容性。
- **敏感信息管理**: API Key 等敏感信息不直接写入 YAML，而是通过 `api_key_env` 字段指定环境变量名（如 `DEEPSEEK_API_KEY`），在运行时由应用层从环境中提取，符合安全最佳实践。
- **自包含采集**: `lan_mesh/collect_config.py` 作为一个独立脚本，不依赖框架包即可采集主机硬件与网络配置，体现了系统在分布式节点初始化阶段的解耦设计。

### 4. 开发者规范
- **新增配置项**: 必须在 `lan_mesh/config.py` 中定义对应的 Pydantic 模型字段，并提供合理的默认值。
- **配置文件位置**: 优先将 `config.yaml` 放置在 `~/.lan_mesh/` 目录下以实现全局生效，或在项目根目录放置用于开发调试。
- **模型扩展**: 新增 LLM 模型时，需在 `model_pool.yaml` 中按规范添加条目，并确保 `fallback` 链中的模型 ID 存在，以防止路由失败。