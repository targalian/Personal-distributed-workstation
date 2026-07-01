该项目采用多语言混合架构（Python 后端、Rust/Tauri 桌面端、React 前端），针对不同技术栈实施了标准化的依赖管理策略，并严格区分了代码依赖与运行时敏感配置。

### 1. Python 后端 (lan_mesh)
- **包管理器**: 使用 `pip` 配合 `requirements.txt` 进行依赖声明。
- **依赖范围**: 核心依赖包括 `fastapi`, `uvicorn`, `pydantic`, `websockets` 等，版本约束采用最小版本兼容模式（如 `>=0.104.0`）。
- **环境隔离**: 通过 `.venv/` 目录实现虚拟环境隔离，并在 `.gitignore` 中排除，确保开发环境的一致性且不污染版本库。
- **配置与密钥隔离**: 
  - `model_pool.yaml` 被明确列入 `.gitignore`，禁止提交到版本库。该文件包含模型 API Key 的环境变量名映射及详细的模型元数据（成本、能力评分等）。
  - 提供 `model_pool.example.yaml` 作为模板，指导开发者如何配置本地环境。
  - 这种模式实现了“代码即配置”的解耦，确保敏感信息（API Keys）不随代码分发。

### 2. Rust/Tauri 桌面端 (quicklan-main/src-tauri)
- **包管理器**: 使用 `Cargo` 作为构建系统和包管理器。
- **依赖锁定**: 通过 `Cargo.toml` 声明直接依赖（如 `tauri`, `tokio`, `rusqlite`），并通过 `Cargo.lock` 锁定整个依赖树的精确版本，确保跨平台构建的可复现性。
- **特性管理**: 在 `Cargo.toml` 中精细控制依赖特性（features），例如 `rusqlite` 启用 `bundled` 特性以简化部署，`tokio` 仅启用必要的异步运行时组件。

### 3. React 前端 (quicklan-main)
- **包管理器**: 使用 `npm` (由 `package-lock.json` 确认) 管理前端依赖。
- **依赖锁定**: 通过 `package-lock.json` 锁定依赖树，确保 `vite`, `react`, `@tauri-apps/api` 等库的版本一致性。
- **构建工具链**: 依赖 `vite` 和 `@tauri-apps/cli` 进行开发与生产构建，并通过 `tsconfig.json` 管理 TypeScript 类型依赖。

### 4. 开发者规范
- **严禁提交敏感文件**: 任何包含真实 API Key 或私有凭证的 `model_pool.yaml`、`.env` 文件均被 Git 忽略。
- **依赖更新**: 
  - Python: 修改 `requirements.txt` 后需重新安装环境。
  - Rust: 运行 `cargo update` 需谨慎，应优先信任 `Cargo.lock` 以保证稳定性。
  - Frontend: 使用 `npm install` 同步 `package-lock.json` 中的依赖状态。
- **环境初始化**: 项目提供了 `scripts/setup_env.*` 脚本，用于自动化处理不同操作系统下的环境依赖安装与配置模板复制。