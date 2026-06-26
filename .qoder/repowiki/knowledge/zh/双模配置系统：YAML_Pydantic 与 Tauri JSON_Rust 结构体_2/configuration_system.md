LAN Mesh 项目采用**双模配置架构**，分别针对后端分布式网格（Python）和桌面客户端（Tauri/Rust）设计了不同的配置加载、校验与管理机制。

### 1. Python 后端：基于 Pydantic 的分层 YAML 配置

**核心逻辑**位于 `lan_mesh/config.py`。系统利用 `pydantic` 提供强类型校验，通过 `yaml.safe_load` 解析配置文件。

*   **分层加载策略**：
    `load_config()` 函数实现了严格的优先级查找顺序：
    1.  显式指定的路径参数。
    2.  环境变量 `LAN_MESH_CONFIG`。
    3.  用户主目录下的 `~/.lan_mesh/config.yaml`。
    4.  当前工作目录下的 `./config.yaml`。
    5.  若均不存在，则返回包含默认值的 `AppConfig` 实例。

*   **模型池独立配置**：
    针对 AI 模型路由功能，提供了独立的 `load_model_pool()` 逻辑，支持通过 `LAN_MESH_MODEL_POOL` 环境变量或包目录下的 `model_pool.yaml` 进行配置。该配置定义了模型的厂商、API Key 环境变量名、成本基准及降级链（fallback）。

*   **安全与路径处理**：
    *   **敏感信息隔离**：API Key 等敏感数据不直接存储在 YAML 中，而是通过 `api_key_env` 字段指定环境变量名，运行时从环境中读取。
    *   **路径展开**：内置 `_expand()` 工具函数，自动处理路径中的 `~`（用户主目录）和环境变量引用。

### 2. Tauri 桌面端：基于 JSON 的持久化设置

**核心逻辑**位于 `quicklan-main/src-tauri/src/settings.rs`。客户端配置侧重于用户个性化设置（如昵称、下载目录），采用 JSON 格式存储。

*   **存储位置**：
    配置文件 `settings.json` 存储在操作系统标准的配置目录下（通过 `dirs::config_dir()` 获取，如 Windows 的 `%APPDATA%\QuickLAN`）。

*   **加载与容错机制**：
    `SettingsService::load()` 在启动时尝试读取并反序列化 JSON。若文件不存在或格式错误，系统会自动回退到默认配置（`default_settings()`），并确保将默认配置写回磁盘以初始化环境。

*   **动态更新与同步**：
    配置对象被包裹在 `Arc<Mutex<AppSettings>>` 中，支持多线程安全访问。当用户通过 UI 修改昵称或下载路径时，`update_*` 方法会同步更新内存状态并持久化到 JSON 文件。

### 3. 开发规范与约定

*   **禁止提交敏感配置**：`model_pool.yaml` 等包含实际密钥引用的文件已被列入 `.gitignore`，开发者应基于 `model_pool.example.yaml` 创建本地配置。
*   **类型安全优先**：Python 端严禁直接使用字典访问配置，必须通过 `AppConfig` 及其子模型（如 `DiscoveryConfig`）的属性访问，以确保在应用启动阶段即可发现配置错误。
*   **路径标准化**：在处理文件共享或数据库路径时，必须调用 `get_shared_folder()` 或 `get_db_path()` 等辅助函数，以确保路径在不同操作系统下正确展开。