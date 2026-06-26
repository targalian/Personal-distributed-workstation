## 1. 核心系统与工具

LAN Mesh 采用**基于 Pydantic 的强类型配置管理**（Python 后端）与**本地 JSON 持久化**（Tauri 前端）相结合的混合配置架构。

- **Python 后端 (`lan_mesh`)**: 使用 `pydantic` 进行数据模型定义与校验，结合 `yaml` 库解析配置文件。支持环境变量注入与命令行参数覆盖。
- **Tauri 前端 (`quicklan-main`)**: 使用 Rust 的 `serde` 进行序列化/反序列化，将用户设置持久化为本地 JSON 文件，并通过 `dirs` crate 定位系统配置目录。

## 2. 关键文件与职责

| 文件路径 | 职责描述 |
| :--- | :--- |
| `lan_mesh/config.py` | 核心配置逻辑。定义了 `AppConfig`、`DiscoveryConfig` 等 Pydantic 模型，并实现了多级查找的 `load_config` 函数。 |
| `config.yaml` | 项目根目录下的默认配置模板，包含 UDP 发现端口、Worker/Secretary 节点的基础设置。 |
| `lan_mesh/model_pool.example.yaml` | 模型池配置示例。定义了 LLM 模型的 ID、Provider、成本、能力评分及降级链，需复制为 `model_pool.yaml` 使用。 |
| `main.py` | 统一入口。负责解析 CLI 参数并将其合并到配置对象中，随后启动对应角色的控制器。 |
| `quicklan-main/src-tauri/src/settings.rs` | Tauri 应用的设置服务。负责加载、校验和保存 `settings.json`（包含昵称、下载目录等）。 |
| `quicklan-main/src-tauri/src/storage.rs` | 存储路径管理。定义了应用数据目录、配置目录及共享存储区的物理路径规则。 |

## 3. 架构设计与加载逻辑

### 3.1 Python 后端配置加载顺序
`load_config()` 函数遵循严格的优先级顺序查找配置文件：
1. **显式指定**: 通过 `--config` 或 `-c` 命令行参数指定的路径。
2. **环境变量**: `LAN_MESH_CONFIG` 指向的路径。
3. **用户全局配置**: `~/.lan_mesh/config.yaml`。
4. **项目本地配置**: 当前工作目录下的 `./config.yaml`。

若所有路径均不存在，系统将返回带有默认值的 `AppConfig` 实例。

### 3.2 配置覆盖机制
系统支持三层配置覆盖，优先级从高到低为：
1. **命令行参数 (CLI)**: 在 `main.py` 中，`--port`, `--name`, `--shared` 等参数会直接修改内存中的配置对象。
2. **环境变量**: 部分敏感信息（如 API Key）通过 `api_key_env` 字段间接引用环境变量。
3. **YAML 文件**: 基础运行时参数（端口、路径、超时时间等）。

### 3.3 模型池独立配置
为了隔离业务配置与 AI 模型元数据，系统引入了独立的 `load_model_pool()` 逻辑：
- 查找顺序类似主配置，但优先检查包目录 `lan_mesh/model_pool.yaml`。
- 使用 `ModelEntryConfig` 对每个模型进行强类型校验，确保路由算法能获取准确的 `quality_score` 和 `cost` 信息。

### 3.4 Tauri 前端配置持久化
- **路径选择**: 使用 `dirs::config_dir()` 确保配置文件位于操作系统的标准配置目录下（如 Windows 的 `%APPDATA%`）。
- **自动修复**: `normalize_settings` 函数会在加载时校验字段有效性（如昵称非空），若不合法则回退到默认值（如主机名）。

## 4. 开发者规范

1. **新增配置项**: 必须在 `lan_mesh/config.py` 中对应的 Pydantic `BaseModel` 子类中添加字段，并设置合理的默认值。
2. **敏感信息管理**: 严禁在 `config.yaml` 中明文存储 API Key。应使用 `api_key_env` 字段指定环境变量名（如 `DEEPSEEK_API_KEY`），并在代码中通过 `os.environ` 读取。
3. **路径处理**: 所有涉及文件系统的路径配置（如 `shared_folder`, `db_path`）必须通过 `config.py` 中的 `_expand()` 函数处理，以支持 `~` 符号和环境变量展开。
4. **配置文件安全**: `model_pool.yaml` 已加入 `.gitignore`。开发者应从 `model_pool.example.yaml` 复制并修改，避免误提交密钥信息。