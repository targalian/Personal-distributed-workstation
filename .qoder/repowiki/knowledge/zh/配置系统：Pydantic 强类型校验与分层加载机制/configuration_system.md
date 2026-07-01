## 1. 核心系统与工具

LAN Mesh 采用 **Pydantic** 作为核心配置管理框架，结合 **YAML** 文件存储与 **环境变量** 注入，实现了强类型、可校验且支持多层覆盖的配置系统。

- **配置解析**：使用 `pydantic.BaseModel` 定义结构化配置模型（如 `AppConfig`, `DiscoveryConfig`），确保配置项的类型安全与默认值管理。
- **文件存储**：主要配置存储在 `config.yaml`（应用级）和 `model_pool.yaml`（模型路由级）中。
- **环境集成**：敏感信息（如 API Key）通过环境变量名引用（`api_key_env`），而非直接硬编码在配置文件中。

## 2. 关键文件与逻辑

### Python 后端 (lan_mesh)
- **`lan_mesh/config.py`**：配置系统的核心入口。定义了所有 Pydantic 模型，并提供了 `load_config()` 和 `load_model_pool()` 两个主要加载函数。
- **`config.yaml`**：根目录下的示例配置文件，包含 UDP 发现、Worker 和 Secretary 的基础参数。
- **`lan_mesh/model_pool.yaml`**：定义 LLM 模型池的详细参数（Provider, Cost, Capabilities, Fallback 链等）。
- **`main.py`**：统一启动入口，负责解析命令行参数并将其覆盖到已加载的配置对象中。

### Rust 前端/客户端 (quicklan-main)
- **`quicklan-main/src-tauri/src/settings.rs`**：基于 `serde` 的本地设置管理，处理用户昵称、下载目录等持久化配置，存储为 JSON 格式。
- **`quicklan-main/src-tauri/tauri.conf.json`**：Tauri 框架的应用构建与运行时配置。

## 3. 架构设计与加载约定

### 3.1 分层加载优先级 (Python)
`load_config()` 遵循严格的查找顺序，一旦找到即停止：
1. **显式路径**：通过 `--config` 或 `-c` 命令行参数指定的路径。
2. **环境变量**：`LAN_MESH_CONFIG` 指向的路径。
3. **用户全局配置**：`~/.lan_mesh/config.yaml`。
4. **项目本地配置**：当前工作目录下的 `./config.yaml`。
5. **兜底策略**：若以上均不存在，则返回包含所有默认值的 `AppConfig` 实例。

*注：`model_pool.yaml` 也有类似的加载逻辑，支持 `LAN_MESH_MODEL_POOL` 环境变量。*

### 3.2 命令行覆盖机制
在 `main.py` 中，配置加载后会根据命令行参数进行动态覆盖：
- `--name`：覆盖 `device_name`。
- `--port`：覆盖 `api_port`。
- `--shared`：覆盖 `shared_folder`。
这种设计允许在不修改 YAML 文件的情况下快速调整节点行为。

### 3.3 路径自动展开
配置中的路径字段（如 `~/lan_mesh_shared`）会通过 `_expand()` 函数自动处理 `~`（用户主目录）和环境变量展开，确保跨平台兼容性。

## 4. 开发者规范

1. **新增配置项**：必须在 `lan_mesh/config.py` 中对应的 `BaseModel` 子类中添加字段，并设定合理的 `default` 值。
2. **敏感信息管理**：严禁在 `config.yaml` 中明文存储 API Key。应使用 `api_key_env` 字段指定环境变量名（如 `DEEPSEEK_API_KEY`），并在运行时由代码从 `os.environ` 读取。
3. **配置文件位置**：推荐将个人化的 `config.yaml` 放置在 `~/.lan_mesh/` 目录下，以避免污染项目根目录或误提交至版本控制系统。
4. **模型池维护**：`model_pool.yaml` 已被 `.gitignore` 排除。开发者应复制 `model_pool.example.yaml` 并进行个性化配置，确保不泄露私有模型端点或密钥信息。