本项目采用多语言混合架构（Python、Rust/Tauri、TypeScript/React），针对不同技术栈实施了独立且标准的依赖管理方案，各模块间无统一的 Monorepo 依赖协调工具。

### 1. Python 后端 (`lan_mesh`)
- **管理工具**：使用 `pip` 配合根目录下的 `requirements.txt`。
- **版本策略**：采用最小版本约束（如 `fastapi>=0.104.0`），未提供 `requirements.lock` 或 `Pipfile.lock`。这种策略在开发阶段提供了灵活性，但在生产环境复现时可能存在细微的版本漂移风险。
- **核心依赖**：包括 `fastapi`, `uvicorn`, `pydantic`, `psutil`, `requests` 等，主要用于构建 HTTP API、系统信息采集和网络通信。
- **配置隔离**：敏感配置（如 LLM API Key）通过 `model_pool.example.yaml` 提供模板，实际配置文件 `model_pool.yaml` 被 `.gitignore` 排除，遵循了配置与代码分离的原则。

### 2. Rust/Tauri 桌面端 (`quicklan-main/src-tauri`)
- **管理工具**：使用 `Cargo` 作为包管理器和构建工具。
- **锁定机制**：通过 `Cargo.toml` 声明直接依赖，并自动生成 `Cargo.lock` 以确保构建的确定性。该文件已纳入版本控制，记录了所有 crate 的精确版本和校验和。
- **关键库**：深度集成 `tauri` (v2), `tokio` (异步运行时), `serde` (序列化), `rusqlite` (本地存储) 等。
- **特性管理**：通过 `features` 字段精细控制依赖编译选项（如 `tokio` 的 `fs`, `net` 等）。

### 3. TypeScript/React 前端 (`quicklan-main`)
- **管理工具**：使用 `npm` 配合 `package.json`。
- **锁定机制**：使用 `package-lock.json` (lockfileVersion 3) 锁定依赖树，确保团队成员和 CI 环境安装完全一致的包版本。
- **构建生态**：基于 `Vite` 构建，集成 `@tauri-apps/cli` 进行桌面应用打包。
- **核心依赖**：`react`, `@tauri-apps/api`, `lucide-react` 以及各类开发依赖。

### 4. 开发者规范与约束
- **禁止提交敏感信息**：严禁将包含真实 API Key 的 `model_pool.yaml` 或其他 `.env` 文件提交至版本库。
- **锁文件维护**：前端和 Rust 层的 lock 文件应随依赖更新同步提交，以维持团队间的环境一致性。严禁手动编辑 `Cargo.lock`。
- **虚拟环境**：Python 开发建议在 `.venv` 或类似隔离环境中安装依赖，避免污染全局 Python 环境。
- **隔离性**：各子模块依赖完全隔离，无跨语言共享依赖机制，开发者需分别进入对应目录执行安装命令。