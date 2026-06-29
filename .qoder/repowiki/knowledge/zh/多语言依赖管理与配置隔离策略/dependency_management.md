该仓库采用混合技术栈（Python, Rust, TypeScript），针对不同语言生态实施了标准化的依赖管理方案，并特别强调了敏感配置与依赖清单的隔离。

### 1. Python 后端 (`lan_mesh`)
- **管理工具**: 使用标准的 `pip` 配合 `requirements.txt` 进行依赖声明。
- **核心依赖**: 包括 `fastapi`, `uvicorn`, `pydantic`, `websockets` 等，版本约束采用最小版本兼容模式（如 `>=0.104.0`）。
- **配置隔离**: 项目通过 `model_pool.example.yaml` 提供配置模板，而实际的模型池配置 `model_pool.yaml` 被明确排除在版本控制之外（`.gitignore`），以防止 API Key 等敏感信息泄露。这种模式将“代码依赖”与“运行时配置/密钥”进行了物理隔离。

### 2. Rust 桌面端 (`quicklan-main/src-tauri`)
- **管理工具**: 使用 `Cargo` 作为包管理器和构建工具。
- **锁定机制**: 通过 `Cargo.toml` 声明直接依赖（如 `tauri`, `tokio`, `rusqlite`），并利用 `Cargo.lock` 确保构建的可重现性。依赖主要来源于 `crates.io` 官方注册表。
- **特性管理**: 在 `Cargo.toml` 中精细控制了 crate 的 features（如 `tokio` 的异步运行时特性），以优化二进制体积和性能。

### 3. TypeScript 前端 (`quicklan-main`)
- **管理工具**: 使用 `npm` 进行包管理，`Vite` 作为构建工具。
- **锁定机制**: 通过 `package.json` 定义项目元数据及依赖树，利用 `package-lock.json` (lockfileVersion 3) 锁定完整的依赖拓扑结构，确保团队开发环境的一致性。
- **生态集成**: 深度集成 `@tauri-apps` 系列插件，实现了前端与 Rust 后端的桥接。

### 4. 开发者规范
- **敏感信息管理**: 严禁将包含真实 API Key 的配置文件（如 `model_pool.yaml`）提交至 Git。新增配置项时应同步更新 `.example` 模板文件。
- **版本一致性**: 在前端和 Rust 开发中，必须提交并同步更新 Lock 文件（`package-lock.json` 和 `Cargo.lock`），以避免“在我机器上是好的”这类环境问题。
- **环境初始化**: 根目录 `scripts/` 提供了跨平台的环境设置脚本（`.sh`, `.ps1`, `.bat`），用于统一初始化 Python 虚拟环境及安装基础依赖。