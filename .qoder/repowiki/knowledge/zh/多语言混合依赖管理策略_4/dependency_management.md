该仓库采用多语言混合架构，针对 Python 后端、Rust 系统层和 TypeScript 前端分别使用独立的包管理器进行依赖管理。

### 1. Python 后端 (`lan_mesh`)
- **管理工具**: 使用标准的 `pip` 配合 `requirements.txt`。
- **版本策略**: 采用最小版本约束（如 `fastapi>=0.104.0`），允许自动获取兼容的次版本更新，但未提供 `requirements.lock` 或 `Pipfile.lock`，导致构建环境可能存在细微差异。
- **核心依赖**: `fastapi`, `uvicorn`, `pydantic`, `websockets` 等，主要用于构建异步 API 服务和 WebSocket 通信。

### 2. Rust 系统层 (`quicklan-main/src-tauri`)
- **管理工具**: 使用 `Cargo` (Rust 官方包管理器)。
- **锁定机制**: 包含 `Cargo.lock` 文件，确保了依赖树中所有 crate 版本的确定性，保证了跨机器构建的一致性。
- **依赖来源**: 主要依赖 `crates.io` 官方源，并深度集成 `tauri` 生态（如 `tauri-plugin-dialog`）以构建跨平台桌面应用。

### 3. TypeScript 前端 (`quicklan-main`)
- **管理工具**: 使用 `npm`。
- **锁定机制**: 包含 `package-lock.json` (lockfileVersion 3)，锁定了精确的依赖版本和完整性哈希，确保前端构建的可复现性。
- **构建工具链**: 基于 `Vite` 和 `@tauri-apps/cli`，依赖包括 `react`, `lucide-react` 等。

### 开发约定
- **隔离性**: 各语言模块的依赖相互独立，通过 `quicklan-main` 目录物理隔离前端与 Rust 代码，`lan_mesh` 目录存放 Python 逻辑。
- **启动脚本**: 根目录 `scripts/` 提供了跨平台的启动脚本（`.sh`, `.ps1`, `.bat`），隐含了环境初始化的顺序，但未在代码层面实现统一的依赖安装自动化（如 Makefile 或 Justfile）。