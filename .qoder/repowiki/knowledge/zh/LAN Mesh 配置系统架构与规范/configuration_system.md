## 1. 核心系统与工具

LAN Mesh 采用 **Pydantic + YAML** 的强类型配置方案，结合 **环境变量覆盖** 机制。
- **核心库**: `pydantic` (数据校验与模型定义), `pyyaml` (YAML 解析)。
- **配置加载器**: `lan_mesh/config.py` 是配置系统的核心入口，负责从多个候选路径加载并校验配置。
- **环境集成**: 支持通过 `LAN_MESH_CONFIG` 和 `LAN_MESH_MODEL_POOL` 环境变量指定配置文件路径。

## 2. 关键文件与目录

| 文件路径 | 作用 |
| :--- | :--- |
| `lan_mesh/config.py` | 配置加载逻辑、Pydantic 模型定义 (`AppConfig`, `ModelPoolConfig`)。 |
| `config.yaml` | 项目根目录下的默认应用配置模板（发现端口、节点 API 端口、共享文件夹等）。 |
| `lan_mesh/model_pool.yaml` | 模型池配置，定义了各 LLM 厂商的接入参数、成本及降级策略。 |
| `lan_mesh/preflight.py` | 启动前自检模块，负责检查配置文件的完整性并自动创建缺失的默认配置。 |
| `quicklan-main/src-tauri/tauri.conf.json` | QuickLAN 桌面应用的 Tauri 框架配置（窗口、打包、安全策略）。 |
| `quicklan-main/src-tauri/src/settings.rs` | QuickLAN 的用户偏好设置管理（昵称、下载目录），基于 JSON 持久化。 |

## 3. 架构设计与约定

### 3.1 分层加载策略
配置加载遵循严格的优先级顺序：
1. **显式参数**: 函数调用时传入的 `config_path`。
2. **环境变量**: `LAN_MESH_CONFIG` 指向的路径。
3. **用户级配置**: `~/.lan_mesh/config.yaml`。
4. **项目级配置**: 当前工作目录下的 `./config.yaml`。

### 3.2 强类型校验
所有配置项均通过 Pydantic `BaseModel` 进行定义。例如 `DiscoveryConfig` 强制要求 `port` 为整数，`WorkerConfig` 规定了 `shared_folder` 的默认值。这种设计确保了在程序启动早期即可捕获格式错误。

### 3.3 敏感信息管理
系统严禁在 YAML 文件中明文存储 API Key。配置文件中仅存储**环境变量名**（如 `api_key_env: DEEPSEEK_API_KEY`），运行时通过 `os.environ.get()` 动态获取实际密钥。

### 3.4 自动修复机制
`preflight.py` 在节点启动时会执行 `_check_config_file`。如果检测到所有候选路径下均不存在配置文件，系统会自动在当前目录生成一份包含默认值的 `config.yaml`，降低了新用户的上手门槛。

## 4. 开发者规范

- **新增配置项**: 必须在 `config.py` 中定义对应的 Pydantic 模型，并设置合理的默认值。
- **密钥处理**: 任何涉及第三方服务的密钥都必须通过 `api_key_env` 字段引用环境变量，禁止硬编码。
- **路径处理**: 配置中的路径字符串必须通过 `config.py` 提供的 `_expand()` 函数处理，以兼容 `~` 和环境变量引用。
- **QuickLAN 设置**: 若需扩展桌面端用户设置，应在 `settings.rs` 中更新 `AppSettings` 结构体，并确保向后兼容（通过 `normalize_settings` 处理旧配置）。
