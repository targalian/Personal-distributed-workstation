## 1. 系统概述
LAN Mesh 采用基于 **YAML** 的声明式配置文件结合 **Pydantic** 强类型模型进行配置管理。系统支持多层级配置来源（文件、环境变量、命令行参数），并实现了自动回退机制。对于 Tauri 桌面端，则采用独立的 JSON 配置存储策略。

## 2. 核心架构与加载逻辑
### 2.1 Python 后端 (lan_mesh)
- **配置定义**：在 `lan_mesh/config.py` 中定义了 `DiscoveryConfig`、`WorkerConfig`、`MasterConfig` 等 Pydantic 模型，确保配置项的类型安全与默认值管理。
- **加载优先级**：`load_config()` 函数按以下顺序查找配置文件：
  1. 显式指定的路径 (`--config` 参数)。
  2. 环境变量 `LAN_MESH_CONFIG` 指向的路径。
  3. 用户主目录下的 `~/.lan_mesh/config.yaml`。
  4. 项目根目录下的 `./config.yaml`。
- **动态覆盖**：在 `main.py` 入口中，命令行参数（如 `--port`, `--name`, `--shared`）会直接覆盖已加载的配置对象实例，实现运行时灵活调整。
- **路径处理**：内置 `_expand()` 工具函数，自动处理路径中的 `~` (用户主目录) 和环境变量展开。

### 2.2 模型池配置 (Model Pool)
- **独立配置文件**：模型路由相关的配置存储在 `model_pool.yaml` 中，通过 `load_model_pool()` 独立加载。
- **敏感信息管理**：API Key 不直接写入 YAML，而是通过 `api_key_env` 字段指定环境变量名（如 `DEEPSEEK_API_KEY`），由业务逻辑在运行时从环境中读取。
- **查找逻辑**：优先查找 `LAN_MESH_MODEL_POOL` 环境变量，其次为包目录及当前工作目录。

### 2.3 Tauri 前端 (QuickLAN)
- **应用配置**：使用标准的 `tauri.conf.json` 管理窗口、构建及安全策略。
- **用户设置**：通过 `quicklan-main/src-tauri/src/settings.rs` 实现用户偏好（如昵称、下载目录）的持久化。配置以 JSON 格式存储在系统配置目录 (`storage::config_dir()`) 下的 `settings.json` 中，并具备默认值归一化处理。

## 3. 关键文件清单
| 文件路径 | 作用描述 |
| :--- | :--- |
| `config.yaml` | 全局默认配置模板，包含发现协议、端口及共享文件夹设置。 |
| `lan_mesh/config.py` | 配置加载核心逻辑，定义 Pydantic 模型及多源合并策略。 |
| `main.py` | 入口脚本，负责解析 CLI 参数并覆盖配置对象。 |
| `lan_mesh/model_pool.example.yaml` | 模型池配置示例，展示多厂商 LLM 的路由与成本配置。 |
| `quicklan-main/src-tauri/tauri.conf.json` | Tauri 桌面应用的静态构建与运行时配置。 |
| `quicklan-main/src-tauri/src/settings.rs` | 桌面端用户动态设置的读写与持久化逻辑。 |

## 4. 开发规范与最佳实践
1. **配置隔离**：严禁将包含真实 API Key 的 `model_pool.yaml` 提交至版本库（已在 `.gitignore` 中排除）。
2. **类型安全**：新增配置项时，必须在 `lan_mesh/config.py` 中更新对应的 Pydantic 模型，并设置合理的默认值。
3. **路径兼容性**：所有涉及文件路径的配置项，必须通过 `config._expand()` 处理，以确保跨平台（Windows/macOS/Linux）的路径解析正确性。
4. **环境变量命名**：遵循 `LAN_MESH_` 前缀约定（如 `LAN_MESH_CONFIG`），避免与其他应用冲突。