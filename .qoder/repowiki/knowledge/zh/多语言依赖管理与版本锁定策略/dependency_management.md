本项目采用混合技术栈（Python, TypeScript/React, Rust/Tauri），针对不同语言生态实施了标准的依赖管理方案。

### 1. Python 后端 (`lan_mesh`)
- **管理工具**: 使用 `pip` 和 `requirements.txt` 进行依赖声明。
- **核心依赖**: 包括 `fastapi`, `uvicorn`, `pydantic`, `psutil`, `requests` 等。
- **版本策略**: 采用最小版本约束（如 `fastapi>=0.104.0`），未提供 `requirements.lock` 或 `poetry.lock`，在严格的生产环境复现中可能存在细微的版本漂移风险。
- **配置隔离**: 敏感配置（如模型 API Key）通过 `model_pool.example.yaml` 提供模板，实际配置文件 `model_pool.yaml` 被 `.gitignore` 排除，遵循了配置与代码分离的原则。

### 2. 前端应用 (`quicklan-main`)
- **管理工具**: 使用 `npm` (Node.js) 管理依赖。
- **核心依赖**: 基于 `React`, `Vite`, `TypeScript` 以及 `@tauri-apps/api` 系列插件。
- **版本锁定**: 提供了 `package-lock.json` (lockfileVersion 3)，确保了开发环境与构建环境依赖树的一致性。所有依赖均指向公共 npm registry。

### 3. 桌面端原生层 (`quicklan-main/src-tauri`)
- **管理工具**: 使用 `Cargo` (Rust) 管理原生依赖。
- **核心依赖**: 深度集成 `tauri` (v2), `tokio` (异步运行时), `serde` (序列化), `rusqlite` (本地数据库) 等。
- **版本锁定**: 提供了详尽的 `Cargo.lock` 文件，记录了所有 crate 的精确版本和校验和（checksum），确保了原生二进制构建的高度可复现性。
- **构建系统**: 通过 `tauri-build` 和 `build.rs` 处理原生资源的编译与链接。

### 4. 开发者规范
- **禁止提交敏感信息**: 严禁将包含真实 API Key 的 `model_pool.yaml` 或其他 `.env` 文件提交至版本库。
- **锁文件维护**: 前端和 Rust 层的 lock 文件应随依赖更新同步提交，以维持团队间的环境一致性。
- **虚拟环境**: Python 开发建议在 `.venv` 或类似隔离环境中安装依赖，避免污染全局 Python 环境。