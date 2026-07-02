LAN Mesh 项目采用多语言混合架构，针对不同技术栈（Python、Rust、TypeScript）实施了独立的依赖管理方案。核心特征如下：

### 1. Python 后端 (lan_mesh)
- **管理工具**：使用标准的 `pip` 和 `requirements.txt`。
- **版本策略**：采用最小版本约束（如 `fastapi>=0.104.0`），允许向后兼容的自动升级，但未提供 `requirements.lock` 或 `Pipfile.lock`，表明在开发阶段更倾向于灵活性而非严格的确定性构建。
- **核心依赖**：`fastapi`, `uvicorn`, `pydantic`, `psutil`, `requests` 等，主要用于构建 HTTP API、系统信息采集和网络通信。

### 2. Rust/Tauri 桌面端 (quicklan-main/src-tauri)
- **管理工具**：使用 `Cargo` 作为包管理器和构建工具。
- **锁定机制**：通过 `Cargo.toml` 声明直接依赖，并自动生成 `Cargo.lock` 以确保构建的确定性。所有依赖均指向 `crates.io` 官方源。
- **关键库**：`tauri` (v2), `tokio` (异步运行时), `serde` (序列化), `rusqlite` (本地存储)。

### 3. TypeScript/React 前端 (quicklan-main)
- **管理工具**：使用 `npm` 配合 `package.json`。
- **锁定机制**：使用 `package-lock.json` (lockfileVersion 3) 锁定依赖树，确保团队成员和 CI 环境安装完全一致的包版本。
- **构建生态**：基于 `Vite` 构建，集成 `@tauri-apps/cli` 进行桌面应用打包。

### 4. 缺乏统一的 Monorepo 依赖协调
- 项目根目录与各子模块（`lan_mesh`, `quicklan-main`）之间没有发现统一的依赖编排工具（如 `pnpm workspace`, `Nx`, 或 `Justfile`）。各子模块独立维护其依赖清单，开发者需分别进入对应目录执行安装命令。

### 5. 私有源与 Vendoring
- 未发现配置私有注册表（如 `.npmrc` 指向私有源，或 `Cargo` 的 `config.toml` 替换源）。
- 未发现第三方库的代码 vendoring（即直接将库代码提交到 repo）行为，所有依赖均通过标准包管理器从公共源获取。