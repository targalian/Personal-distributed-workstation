该项目采用**双模配置架构**，分别针对 Python 后端（LAN Mesh）和 Rust/Tauri 桌面前端（QuickLAN）设计了独立的配置加载、校验与持久化机制。

### 1. LAN Mesh (Python) - 基于 Pydantic 的分层 YAML 配置

**核心逻辑：**
- **强类型校验**：使用 `pydantic` 定义配置模型（`AppConfig`, `DiscoveryConfig`, `WorkerConfig`, `SecretaryConfig`），确保配置项的类型安全与默认值管理。
- **多源加载策略**：`load_config()` 函数实现了严格的优先级查找顺序：
  1. 命令行参数 `--config` 指定的路径。
  2. 环境变量 `LAN_MESH_CONFIG`。
  3. 用户主目录 `~/.lan_mesh/config.yaml`。
  4. 项目根目录 `./config.yaml`。
  5. 若均不存在，则返回包含默认值的 `AppConfig` 实例。
- **命令行覆盖**：在 `main.py` 中，命令行参数（如 `--port`, `--name`, `--shared`）会在加载 YAML 后直接修改配置对象实例，实现运行时灵活覆盖。
- **敏感信息管理**：API Keys 等敏感信息不直接存入 YAML，而是通过 `model_pool.yaml` 中的 `api_key_env` 字段指定环境变量名（如 `DEEPSEEK_API_KEY`），由业务逻辑在运行时从环境中读取。
- **模型池配置**：独立的 `model_pool.yaml` 用于管理 LLM 模型元数据（ID、Provider、成本、能力评分、降级链等），通过 `load_model_pool()` 加载，支持包内默认配置与用户自定义配置合并。

**关键文件：**
- `lan_mesh/config.py`: 配置模型定义与加载逻辑。
- `config.yaml`: 项目根目录示例配置。
- `lan_mesh/model_pool.yaml`: 模型池元数据配置。
- `main.py`: 入口脚本，处理 CLI 参数与配置合并。

### 2. QuickLAN (Tauri/Rust) - 基于 JSON 的用户设置持久化

**核心逻辑：**
- **应用构建配置**：`tauri.conf.json` 管理 Tauri 应用的窗口属性、构建命令、安全策略及打包选项（NSIS 安装器配置）。
- **用户设置持久化**：`settings.rs` 实现了 `SettingsService`，负责管理用户级运行时设置（如昵称、下载目录）。
  - **存储位置**：使用 `dirs::config_dir()` 定位平台特定的配置目录（如 Windows 的 `%APPDATA%\QuickLAN`），存储为 `settings.json`。
  - **加载与容错**：启动时尝试读取并反序列化 JSON；若文件不存在或格式错误，则生成默认配置（默认昵称为主机名，默认下载目录为系统下载文件夹）。
  - **原子更新**：提供 `update_nickname` 和 `update_download_dir` 方法，修改后立即持久化到磁盘，确保状态一致性。
- **数据存储路径**：`storage.rs` 定义了应用数据目录（`dirs::data_dir()`），用于存放 SQLite 数据库、共享文件缓存等非配置类持久化数据。

**关键文件：**
- `quicklan-main/src-tauri/tauri.conf.json`: Tauri 框架配置。
- `quicklan-main/src-tauri/src/settings.rs`: 用户设置加载、校验与持久化逻辑。
- `quicklan-main/src-tauri/src/storage.rs`: 应用数据目录与文件存储路径管理。

### 3. 开发规范与建议

- **Python 端**：新增配置项时，必须在 `lan_mesh/config.py` 中更新对应的 Pydantic 模型，并设置合理的默认值。敏感信息严禁硬编码，应通过 `api_key_env` 指向环境变量。
- **Rust 端**：用户可修改的设置应纳入 `AppSettings` 结构体并通过 `SettingsService` 管理；不可变的系统路径或常量应放在 `storage.rs` 或编译期常量中。
- **配置隔离**：区分“应用配置”（YAML/JSON，描述系统行为）与“用户数据”（SQLite/文件系统，描述业务状态）。
- **路径处理**：Python 端使用 `os.path.expanduser` 处理 `~`；Rust 端使用 `dirs` crate 获取平台标准目录，避免硬编码绝对路径。