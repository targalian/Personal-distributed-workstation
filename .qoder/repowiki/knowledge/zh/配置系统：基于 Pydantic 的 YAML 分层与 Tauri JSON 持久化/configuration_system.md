该仓库采用双轨配置架构，分别服务于 Python 后端网格（LAN Mesh）和 Rust/Tauri 前端桌面应用（QuickLAN）。

### 1. Python 后端 (LAN Mesh)
**核心机制**：
- **强类型校验**：使用 `pydantic` 定义配置模型（`AppConfig`, `DiscoveryConfig`, `WorkerConfig` 等），确保运行时配置的合法性。
- **YAML 驱动**：主配置文件为 `config.yaml`，支持嵌套结构（如 `discovery.port`, `worker.api_port`）。
- **多级加载策略**：`lan_mesh/config.py` 中的 `load_config()` 按以下优先级查找配置：
  1. 命令行参数 `--config`
  2. 环境变量 `LAN_MESH_CONFIG`
  3. 用户目录 `~/.lan_mesh/config.yaml`
  4. 项目根目录 `./config.yaml`
  5. 内存默认值（Pydantic Field defaults）

**敏感信息管理**：
- **环境变量引用**：在 `model_pool.yaml` 中，API Key 不直接存储，而是通过 `api_key_env` 字段指定环境变量名（如 `DEEPSEEK_API_KEY`）。系统在运行时从环境中读取实际密钥。
- **Git 忽略**：`model_pool.yaml` 被明确加入 `.gitignore`，防止密钥泄露。

**动态覆盖**：
- `main.py` 入口脚本允许通过 CLI 参数（`--port`, `--name`, `--shared`）动态覆盖 YAML 中的特定字段，实现“配置即代码”与“启动时微调”的结合。

### 2. Rust/Tauri 前端 (QuickLAN)
**核心机制**：
- **JSON 持久化**：使用 `serde_json` 将 `AppSettings`（昵称、下载目录）序列化到本地文件系统。
- **路径规范**：配置文件位于 `storage::config_dir()/settings.json`，遵循操作系统标准配置目录规范。
- **自动修复与默认值**：`SettingsService::load()` 在读取失败或字段缺失时，会自动回退到默认值（如通过 `hostname::get()` 获取默认昵称），并立即保存规范化后的配置。

### 3. 开发约定
- **路径展开**：Python 配置中所有涉及路径的字段（如 `shared_folder`, `db_path`）均通过 `os.path.expanduser` 自动展开 `~`。
- **角色差异化**：配置结构根据节点角色（Station/Secretary/Worker）进行逻辑隔离，但共享同一套发现协议配置（`discovery`）。
- **模型池独立**：模型路由配置独立于主配置，通过 `load_model_pool()` 单独加载，支持更频繁的迭代而不影响基础网络配置。