该仓库采用多语言混合的依赖管理策略，分别针对 Python 后端服务 (`lan_mesh`) 和 Tauri 桌面客户端 (`quicklan-main`) 使用不同的包管理器。

### 1. Python 后端 (lan_mesh)
- **管理工具**: 使用标准的 `pip` 配合 `requirements.txt` 进行依赖声明。
- **版本策略**: 采用最小版本约束（如 `fastapi>=0.104.0`），允许自动获取兼容的最新补丁或次版本，未提供 `requirements.lock` 或 `Pipfile.lock` 等锁定文件，这在开发阶段较为常见，但在生产部署中可能存在环境不一致风险。
- **核心依赖**: 主要包括 `fastapi` (Web 框架), `uvicorn` (ASGI 服务器), `pydantic` (数据校验), `websockets` (实时通信) 以及 `psutil` (系统信息采集)。

### 2. Tauri 桌面客户端 (quicklan-main)
该项目包含前端 (React/TypeScript) 和后端 (Rust) 两部分，分别由 npm 和 Cargo 管理。

#### 前端依赖 (npm)
- **管理工具**: 使用 `npm`，通过 `package.json` 声明依赖，并由 `package-lock.json` (lockfileVersion 3) 严格锁定版本。
- **核心依赖**: 
  - `@tauri-apps/api` 及相关插件 (`dialog`, `opener`)：用于与 Rust 后端交互。
  - `react` / `react-dom`: UI 框架。
  - `vite`: 构建工具。
- **私有性**: `package.json` 中标记 `"private": true`，表明该应用不作为 npm 包发布。

#### 后端依赖 (Cargo/Rust)
- **管理工具**: 使用 `cargo`，通过 `src-tauri/Cargo.toml` 声明依赖，并由 `src-tauri/Cargo.lock` 严格锁定所有传递依赖的版本和校验和。
- **核心依赖**:
  - `tauri` (v2): 核心框架。
  - `tokio`: 异步运行时，启用了 `fs`, `net`, `rt-multi-thread` 等特性以支持局域网发现和文件传输。
  - `rusqlite`: 嵌入式数据库，使用 `bundled` 特性以确保跨平台编译时的 SQLite 可用性。
  - `serde` / `serde_json`: 序列化支持。
- **构建依赖**: `tauri-build` 用于处理 Tauri 特定的构建流程。

### 3. 开发者规范
- **Python 环境**: 建议在使用前运行 `pip install -r requirements.txt`。由于缺乏锁定文件，建议在虚拟环境 (`.venv`) 中安装以避免冲突。
- **前端更新**: 修改 `package.json` 后需运行 `npm install` 以同步 `package-lock.json`。
- **Rust 更新**: 修改 `Cargo.toml` 后需运行 `cargo build` 或 `cargo update` 以同步 `Cargo.lock`。
- **一致性**: 在提交代码时，必须同时提交 `package-lock.json` 和 `Cargo.lock` 以确保团队成员和 CI/CD 环境构建的一致性。