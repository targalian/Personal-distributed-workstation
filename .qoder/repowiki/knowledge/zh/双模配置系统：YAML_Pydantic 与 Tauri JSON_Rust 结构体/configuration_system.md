该项目包含两个独立的子系统（Python 后端 `lan_mesh` 和 Tauri 桌面应用 `quicklan-main`），各自采用了符合其技术栈的配置管理方案。

### 1. LAN Mesh (Python) 配置系统
**核心机制**：基于 `Pydantic` 的强类型校验与 `YAML` 文件加载。
- **配置源优先级**：
  1. 命令行参数 `--config` 指定的路径。
  2. 环境变量 `LAN_MESH_CONFIG` 指定的路径。
  3. 用户主目录下的 `~/.lan_mesh/config.yaml`。
  4. 项目根目录下的 `./config.yaml`。
- **数据结构**：使用 `AppConfig` 模型，内部嵌套 `DiscoveryConfig`（发现协议）、`WorkerConfig`（工作节点）和 `MasterConfig`（主控节点）。
- **动态覆盖**：在 `main.py` 入口中，命令行参数（如 `--port`, `--name`, `--shared`）会直接覆盖从 YAML 加载的配置对象属性，实现灵活的运行时定制。
- **路径处理**：内置 `_expand` 函数，自动处理路径中的 `~`（用户主目录）和环境变量展开。

### 2. QuickLAN (Tauri/Rust) 配置系统
**核心机制**：分层配置，区分“应用构建配置”与“用户运行时设置”。
- **应用构建配置 (`tauri.conf.json`)**：
  - 定义窗口尺寸、安全策略（CSP）、打包目标（NSIS）及开发/构建命令。
  - 遵循 Tauri 2.0 标准 schema，静态且随应用分发。
- **用户运行时设置 (`settings.rs`)**：
  - **存储格式**：JSON 文件 (`settings.json`)。
  - **存储位置**：通过 `dirs::config_dir()` 获取系统标准配置目录（如 Windows 的 `AppData/Roaming/QuickLAN`）。
  - **加载逻辑**：应用启动时尝试读取 JSON，若失败或字段缺失则回退到默认值（如使用主机名作为昵称，使用系统下载目录作为保存路径）。
  - **持久化**：提供 `SettingsService`，支持线程安全地更新昵称和下载目录，并自动同步到磁盘。
- **数据存储路径 (`storage.rs`)**：
  - 独立于用户设置，定义了应用数据目录（`QuickLANData`），用于存放 SQLite 数据库和共享文件缓存（`shared_store`）。

### 开发规范与建议
- **Python 端**：新增配置项需在 `lan_mesh/config.py` 中定义对应的 Pydantic 模型字段，并更新 `config.yaml` 示例。避免硬编码路径，统一使用 `get_shared_folder` 等辅助函数。
- **Rust 端**：用户可修改的设置应纳入 `AppSettings` 结构体并通过 `SettingsService` 管理；不可变的系统路径或常量应放在 `storage.rs` 或 `protocol.rs` 中。
- **一致性**：两个子系统均优先使用操作系统标准的配置/数据目录，确保跨平台兼容性。