该项目采用**双模构建体系**，分别针对 Python 后端服务（LAN Mesh）和 Rust/Tauri 桌面应用（QuickLAN）设计了独立的开发与发布流程。

### 1. LAN Mesh (Python 后端)
- **依赖管理**：使用 `requirements.txt` 锁定核心依赖（FastAPI, Pydantic, uvicorn 等）。通过 `.venv` 虚拟环境隔离运行环境。
- **启动与编排**：采用**脚本化入口**模式。根目录下的 `main.py` 作为统一逻辑入口，支持通过参数切换角色（`station`, `secretary`, `worker`）。
- **环境初始化**：提供跨平台的初始化脚本（`scripts/setup_env.sh/ps1/bat`），自动完成虚拟环境创建、依赖安装及配置文件模板（如 `model_pool.yaml`）的生成。
- **运行规范**：开发者应优先使用 `scripts/` 下的封装脚本启动服务，以确保环境变量和路径的正确加载。

### 2. QuickLAN (Tauri 桌面应用)
- **技术栈**：基于 Tauri 2.0，前端采用 React + Vite，后端采用 Rust。
- **前端构建**：通过 `npm run build` 调用 Vite 进行静态资源编译，产物输出至 `dist/` 目录。
- **原生打包**：利用 `tauri build` 命令将 Rust 二进制与前端资源合并。配置位于 `src-tauri/tauri.conf.json`，目前主要针对 Windows 平台生成 NSIS 安装包（`targets: ["nsis"]`）。
- **版本同步**：版本号在 `package.json`、`Cargo.toml` 和 `tauri.conf.json` 中保持同步（当前为 `0.1.1`）。

### 3. 关键约定
- **无集中式 CI/CD**：目前未发现 GitHub Actions 或 Jenkins 等自动化流水线配置，构建主要依赖本地脚本执行。
- **配置即代码**：Python 端的配置通过 `config.yaml` 和 Pydantic 模型强类型校验；Tauri 端通过 JSON 结构体定义应用行为。
- **跨平台兼容**：Python 端提供了 `.sh` (Linux/Mac) 和 `.ps1/.bat` (Windows) 两套脚本以适配不同操作系统。