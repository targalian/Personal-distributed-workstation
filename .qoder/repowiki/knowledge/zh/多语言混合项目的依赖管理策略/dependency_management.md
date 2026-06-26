该项目采用多语言混合架构，针对不同模块使用了独立的依赖管理系统：

### 1. Python 后端 (`lan_mesh`)
- **管理工具**: `pip`
- **声明文件**: 根目录下的 `requirements.txt`。
- **版本策略**: 使用最小版本约束（如 `fastapi>=0.104.0`），未提供 `requirements.lock` 或 `pip-tools` 生成的锁定文件，依赖解析具有不确定性。
- **核心依赖**: `fastapi`, `uvicorn`, `pydantic`, `psutil`, `requests` 等。
- **运行时依赖**: 部分功能（如 LLM 调用）依赖环境变量（`DEEPSEEK_API_KEY`）而非代码库中的配置文件。

### 2. Rust/Tauri 桌面端 (`quicklan-main/src-tauri`)
- **管理工具**: `Cargo`
- **声明文件**: `src-tauri/Cargo.toml`。
- **锁定机制**: 使用 `src-tauri/Cargo.lock` 确保构建的可复现性。该文件已纳入版本控制，记录了所有传递性依赖的精确版本和校验和。
- **核心依赖**: `tauri` (v2), `tokio`, `serde`, `rusqlite` (bundled), `windows-sys`。
- **特性管理**: 通过 `features` 字段精细控制依赖编译选项（如 `tokio` 的 `fs`, `net` 等）。

### 3. React/Vite 前端 (`quicklan-main`)
- **管理工具**: `npm`
- **声明文件**: `package.json`。
- **锁定机制**: 使用 `package-lock.json` (lockfileVersion 3) 锁定依赖树，确保团队间和环境间的一致性。
- **核心依赖**: `react`, `@tauri-apps/api`, `lucide-react`。
- **构建工具**: `vite` 及其插件 `@vitejs/plugin-react` 作为开发依赖管理。

### 开发者规范
- **Python**: 新增依赖需手动更新 `requirements.txt`，建议在生产环境部署前生成并验证锁定文件。
- **Rust**: 严禁手动编辑 `Cargo.lock`，应通过 `cargo update` 或 `cargo add` 管理依赖。
- **Frontend**: 使用 `npm install <pkg>` 添加依赖以自动更新 `package.json` 和 `package-lock.json`。
- **隔离性**: 各子模块依赖完全隔离，无跨语言共享依赖机制。